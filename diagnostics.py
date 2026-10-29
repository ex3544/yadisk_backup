"""Диагностический отчёт: собрать, показать, отправить.

Отчёт нужен, чтобы по одному ZIP-файлу понять состояние установки:
система, пути, настройки (без секретов), базы, статусы задач, хвосты логов.

Жёсткие правила
---------------
* В отчёт никогда не попадают токен, содержимое cookies.json, сами базы и
  полные логи. Только маски, счётчики, размеры и последние строки.
* Любая строка перед записью проходит через `redact_text()`. Это страховка:
  даже если токен случайно оказался в логе, в отчёт он не уйдёт.
* Большие логи читаются с конца (seek), а не целиком. `unlim_backup.log`
  может быть в сотни мегабайт.
"""

from __future__ import annotations

import json
import os
import platform
import re
import secrets
import shutil
import socket
import sqlite3
import ssl
import sys
import threading
import traceback
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

import paths
import utils
import version

try:  # python-dotenv есть в зависимостях, но отчёт не должен падать без него
    from dotenv import dotenv_values
except Exception:  # pragma: no cover - зависит от окружения
    dotenv_values = None

DEFAULT_REPORT_URL = "https://yadisk-backup-reports.ex3544.workers.dev"
MAX_UPLOAD_BYTES = 45 * 1024 * 1024
TAIL_BYTES = 64 * 1024
TAIL_LINES = 80
MAX_ERROR_LINES = 40
# Сколько каждого журнала кладём в ZIP. Полный unlim_backup.log бывает
# в сотни мегабайт — его не трогаем, читаем только хвост копии.
LOG_FILE_BYTES = 2 * 1024 * 1024

ERROR_RE = re.compile(
    r"(?i)(\berror\b|\bexception\b|traceback|\bcritical\b|\bfailed\b|"
    r"ошибк|не удалось|провал|критич)"
)

# Порядок важен: более конкретные шаблоны идут раньше общих.
_SECRET_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(?i)(YA_DISK_TOKEN\s*[=:]\s*)\S+"), r"\1***"),
    (re.compile(r"(?i)(X-Report-Key\s*[=:]\s*)\S+"), r"\1***"),
    (re.compile(r"(?i)(Session_id\s*[=:]\s*)[^;\s\"']+"), r"\1***"),
    (re.compile(r"(?i)(sessionid2\s*[=:]\s*)[^;\s\"']+"), r"\1***"),
    (
        re.compile(r"(?i)((?:access_|refresh_)?token[\"']?\s*[=:]\s*)"
                   r"([A-Za-z0-9._~+/=-]{16,})"),
        r"\1***",
    ),
    (re.compile(r"y0_[A-Za-z0-9_-]{10,}"), "y0_***"),
    (re.compile(r"AQAA[A-Za-z0-9_-]{10,}"), "AQAA***"),
]


def redact_text(value: Any) -> str:
    """Убирает из строки известные секреты."""
    text = "" if value is None else str(value)
    for pattern, repl in _SECRET_PATTERNS:
        text = pattern.sub(repl, text)
    return text


def _redact_obj(value: Any) -> Any:
    """Рекурсивно прогоняет все строки через redact_text()."""
    if isinstance(value, dict):
        return {str(k): _redact_obj(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_obj(v) for v in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def mask_token(token: str) -> str:
    """Короткая безопасная подпись токена для отчёта."""
    token = (token or "").strip()
    if not token:
        return ""
    if len(token) <= 12:
        return "***"
    return f"{token[:6]}…{token[-4:]} ({len(token)} симв.)"


def install_id() -> str:
    """Стабильный идентификатор этой установки (не привязан к железу)."""
    marker = paths.INSTALL_ID_FILE
    try:
        if marker.exists():
            value = marker.read_text(encoding="utf-8").strip()
            if re.fullmatch(r"[0-9a-f]{12,32}", value):
                return value
        value = secrets.token_hex(8)
        marker.write_text(value, encoding="utf-8")
        return value
    except OSError:
        return secrets.token_hex(8)


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _size_h(path: Path) -> int:
    try:
        return path.stat().st_size if path.exists() else 0
    except OSError:
        return 0


def _mb(n: int | float | None) -> float:
    return round((n or 0) / (1024 * 1024), 2)


def _gb(n: int | float | None) -> float:
    return round((n or 0) / (1024 ** 3), 2)


def read_tail(path: Path, max_bytes: int = TAIL_BYTES,
              max_lines: int = TAIL_LINES) -> dict:
    """Последние строки файла без чтения всего файла."""
    out = {"exists": path.exists(), "size": _size_h(path),
           "truncated": False, "lines": []}
    if not out["exists"]:
        return out
    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            start = max(0, size - max(1, max_bytes))
            f.seek(start)
            data = f.read()
    except OSError as e:
        out["error"] = f"{type(e).__name__}: {e}"
        return out
    out["truncated"] = start > 0
    text = data.decode("utf-8", errors="replace")
    lines = text.splitlines()
    if out["truncated"] and lines:
        lines = lines[1:]  # первая строка могла обрезаться посередине
    out["lines"] = [redact_text(ln) for ln in lines[-max_lines:]]
    return out


def _disk_usage(path_value: str) -> dict:
    """Свободное место для папки; для несуществующей — ближайшего родителя."""
    if not path_value:
        return {"path": "", "exists": False}
    p = Path(path_value).expanduser()
    original = str(p)
    while not p.exists() and p != p.parent:
        p = p.parent
    try:
        usage = shutil.disk_usage(str(p))
    except OSError as e:
        return {"path": original, "exists": False,
                "error": f"{type(e).__name__}: {e}"}
    return {
        "path": original,
        "exists": Path(path_value).expanduser().exists(),
        "total_gb": _gb(usage.total),
        "free_gb": _gb(usage.free),
        "used_gb": _gb(usage.used),
    }


def system_info() -> dict:
    info = {
        "os": platform.system(),
        "os_release": platform.release(),
        "os_version": platform.version(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "hostname": socket.gethostname(),
        "python": sys.version.split()[0],
        "python_implementation": platform.python_implementation(),
        "python_bits": 64 if sys.maxsize > 2 ** 32 else 32,
        "sqlite": sqlite3.sqlite_version,
        "openssl": ssl.OPENSSL_VERSION,
        "filesystem_encoding": sys.getfilesystemencoding(),
        "executable": sys.executable,
        "frozen": paths.IS_FROZEN,
        "pid": os.getpid(),
        "cwd": os.getcwd(),
        "timezone": datetime.now().astimezone().tzname(),
        "now": _now_iso(),
    }
    try:
        info["ram"] = utils.ram_info()
    except Exception as e:
        info["ram"] = {"error": f"{type(e).__name__}: {e}"}
    try:
        if os.name == "nt":
            import ctypes
            info["is_admin"] = bool(ctypes.windll.shell32.IsUserAnAdmin())
        else:
            info["is_admin"] = (os.geteuid() == 0)  # type: ignore[attr-defined]
    except Exception:
        info["is_admin"] = None
    return info


def _env_values() -> dict:
    if dotenv_values is None or not paths.ENV_FILE.exists():
        return {}
    try:
        return {str(k): str(v) for k, v in dotenv_values(paths.ENV_FILE).items()
                if v is not None}
    except Exception:
        return {}


def _cookie_summary() -> dict:
    out = {"exists": paths.COOKIES_FILE.exists(),
           "size": _size_h(paths.COOKIES_FILE), "count": 0,
           "important_present": False}
    if not out["exists"]:
        return out
    try:
        raw = json.loads(paths.COOKIES_FILE.read_text(encoding="utf-8"))
        if isinstance(raw, list):
            out["count"] = len(raw)
            names = {c.get("name") for c in raw if isinstance(c, dict)}
            out["important_present"] = bool(
                {"Session_id", "yandexuid", "sessionid2"} & names
            )
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
    return out


def config_info() -> dict:
    """Настройки без секретов."""
    env = _env_values()
    token = (env.get("YA_DISK_TOKEN") or os.getenv("YA_DISK_TOKEN", "") or "").strip()
    disk_dir = (env.get("DISK_DIR") or env.get("DOWNLOAD_DIR")
                or os.getenv("DISK_DIR") or os.getenv("DOWNLOAD_DIR")
                or str(paths.DEFAULT_DISK_DIR))
    unlim_dir = (env.get("UNLIM_DIR") or os.getenv("UNLIM_DIR")
                 or str(paths.DEFAULT_UNLIM_DIR))
    report_key = _read_report_key()
    return {
        "env_file_exists": paths.ENV_FILE.exists(),
        "env_file_size": _size_h(paths.ENV_FILE),
        "token_configured": bool(token),
        "token_masked": mask_token(token),
        "disk_dir": disk_dir,
        "unlim_dir": unlim_dir,
        "max_workers": env.get("MAX_WORKERS") or os.getenv("MAX_WORKERS", ""),
        "log_level": env.get("LOG_LEVEL") or os.getenv("LOG_LEVEL", ""),
        "webui_host": env.get("WEBUI_HOST") or os.getenv("WEBUI_HOST", ""),
        "webui_port": env.get("WEBUI_PORT") or os.getenv("WEBUI_PORT", ""),
        "data_dir_override": bool((os.getenv("YADISK_DATA_DIR") or "").strip()),
        "cookies": _cookie_summary(),
        "report_key_configured": bool(report_key),
        "report_url": _report_url(),
    }


def _ro_uri(path: Path) -> str:
    value = os.path.abspath(path)
    if os.name == "nt":
        value = value.replace("\\", "/")
    return f"file:{value}?mode=ro"


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {str(r[1]) for r in conn.execute(f'PRAGMA table_info("{table}")')}
    except sqlite3.Error:
        return set()


def database_info(path: Path, status_table: str | None = None) -> dict:
    out = {"exists": path.exists(), "size_mb": _mb(_size_h(path)),
           "wal_mb": _mb(_size_h(path.with_name(path.name + "-wal"))),
           "schema_version": None, "tables": {}, "status": {},
           "total": 0, "error": None}
    if not out["exists"]:
        return out
    conn = None
    try:
        conn = sqlite3.connect(_ro_uri(path), uri=True, timeout=15)
        conn.row_factory = sqlite3.Row
        out["schema_version"] = conn.execute("PRAGMA user_version").fetchone()[0]
        tables = [
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        for table in tables[:10]:
            try:
                out["tables"][table] = conn.execute(
                    f'SELECT COUNT(*) FROM "{table}"'
                ).fetchone()[0]
            except sqlite3.Error:
                out["tables"][table] = None
        if status_table and status_table in tables:
            try:
                for row in conn.execute(
                    f'SELECT status, COUNT(*) AS n FROM "{status_table}" '
                    f"GROUP BY status"
                ):
                    out["status"][str(row["status"])] = row["n"]
                out["total"] = sum(out["status"].values())
            except sqlite3.Error as e:
                out["status_error"] = f"{type(e).__name__}: {e}"
        # Целостность базы: повреждённый файл виден сразу, без разбора логов.
        try:
            out["quick_check"] = str(
                conn.execute("PRAGMA quick_check").fetchone()[0]
            )[:200]
        except sqlite3.Error as e:
            out["quick_check"] = f"ошибка: {type(e).__name__}"
        # Дубли по имени: один и тот же файл, зарегистрированный дважды.
        columns = _table_columns(conn, status_table) if status_table else set()
        if "name" in columns:
            try:
                row = conn.execute(
                    f'SELECT COUNT(*) AS names, COALESCE(SUM(n - 1), 0) AS extra '
                    f'FROM (SELECT name, COUNT(*) AS n FROM "{status_table}" '
                    f"WHERE name IS NOT NULL GROUP BY name HAVING COUNT(*) > 1)"
                ).fetchone()
                out["duplicates"] = {"names": row["names"], "extra": row["extra"]}
            except sqlite3.Error:
                pass
        # Распределение попыток: 1 — упал один раз, 10+ — бьётся стабильно.
        if "attempts" in columns:
            try:
                buckets = {"0-1": 0, "2-3": 0, "4-9": 0, "10+": 0}
                for row in conn.execute(
                    f'SELECT attempts AS a, COUNT(*) AS n FROM "{status_table}" '
                    f"GROUP BY attempts"
                ):
                    a = int(row["a"] or 0)
                    key = ("0-1" if a <= 1 else "2-3" if a <= 3
                           else "4-9" if a <= 9 else "10+")
                    buckets[key] += int(row["n"])
                out["attempts"] = buckets
            except sqlite3.Error:
                pass
    except sqlite3.Error as e:
        out["error"] = f"{type(e).__name__}: {e}"
    finally:
        if conn is not None:
            conn.close()
    return out


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {"exists": False}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        return {"exists": True, "error": f"{type(e).__name__}: {e}"}
    if not isinstance(raw, dict):
        return {"exists": True, "error": "ожидался объект JSON"}
    out = {"exists": True}
    for key, value in raw.items():
        if isinstance(value, (str, int, float, bool)) or value is None:
            out[str(key)] = redact_text(value) if isinstance(value, str) else value
    if isinstance(raw.get("updated_at"), (int, float)):
        try:
            out["updated_at_local"] = datetime.fromtimestamp(
                float(raw["updated_at"])
            ).astimezone().isoformat(timespec="seconds")
        except (OverflowError, OSError, ValueError):
            pass
    return out


def status_info() -> dict:
    return {
        "disk_download": _read_json(paths.DISK_STATUS_FILE),
        "unlim_download": _read_json(paths.UNLIM_STATUS_FILE),
        "cookies_fetch": _read_json(paths.COOKIES_FETCH_STATUS),
    }


LOG_FILES = [
    ("backup.log", paths.BACKUP_LOG),
    ("unlim_backup.log", paths.UNLIM_LOG),
    ("unlim_worker.log", paths.UNLIM_WORKER_LOG),
    ("unlim.log", paths.UNLIM_COLLECT_LOG),
    ("unlim_direct.log", paths.UNLIM_DIRECT_LOG),
    ("webui_actions.log", paths.ACTION_LOG),
    ("migrations.log", paths.MIGRATIONS_LOG),
    ("launcher.log", paths.LAUNCHER_LOG),
    ("launcher_output.log", paths.LAUNCHER_OUTPUT_LOG),
    ("crash.log", paths.CRASH_LOG),
]


def logs_info() -> dict:
    out: dict[str, Any] = {"files": {}, "tails": {}, "errors": []}
    for name, path in LOG_FILES:
        tail = read_tail(path)
        out["files"][name] = {"exists": tail["exists"], "size_mb": _mb(tail["size"]),
                              "truncated": tail["truncated"]}
        if tail["exists"]:
            out["tails"][name] = tail["lines"]
    seen: set[str] = set()
    per_log = max(1, MAX_ERROR_LINES // max(1, len(out["tails"])))
    for name, lines in out["tails"].items():
        found = 0
        for line in reversed(lines):
            if ERROR_RE.search(line):
                key = redact_text(line).strip()
                if key and key not in seen:
                    seen.add(key)
                    out["errors"].append({"log": name, "line": key})
                    found += 1
                    if found >= per_log or len(out["errors"]) >= MAX_ERROR_LINES:
                        break
        if len(out["errors"]) >= MAX_ERROR_LINES:
            break
    return out


# Адрес сервера бывает в двух видах: полный URL и `HTTPSConnectionPool(host='…')`
# из исключений requests. Оба нужны, иначе группа сбоев остаётся без хоста.
HOST_RE = re.compile(r"(?:https?://|host=')([^/\s\"'()<>]+)")
FAILURE_EXAMPLES = 10
FAILURE_GROUPS_MAX = 12


def error_host(error: str) -> str:
    """Домен из текста ошибки: по нему видно, какой сервер отвечал."""
    match = HOST_RE.search(error or "")
    if not match:
        return ""
    host = match.group(1).lower()
    if "@" in host:
        host = host.rsplit("@", 1)[-1]
    return host.split(":")[0][:80]


def _signature(error: str) -> str:
    """Текст ошибки без меняющихся деталей — основа для группировки."""
    text = redact_text(error or "").strip()
    # Числовые хвосты (номера запросов, размеры, миллисекунды) мешают склейке.
    text = re.sub(r"\b[0-9a-f]{16,}\b", "<id>", text)
    text = re.sub(r"\b\d{4,}\b", "<n>", text)
    return text[:180]


def failure_groups(path: Path, table: str, path_col: str) -> dict:
    """Сбои по группам: ошибка + хост, количество, размер и примеры путей."""
    out: dict[str, Any] = {"total": 0, "groups": []}
    if not path.exists():
        return out
    conn = None
    try:
        conn = sqlite3.connect(_ro_uri(path), uri=True, timeout=15)
        conn.row_factory = sqlite3.Row
        columns = _table_columns(conn, table)
        if "error" not in columns:
            return out
        size_expr = "COALESCE(SUM(size),0)" if "size" in columns else "0"
        merged: dict[tuple[str, str], dict] = {}
        for row in conn.execute(
            f'SELECT error, COUNT(*) AS n, {size_expr} AS b FROM "{table}" '
            f"WHERE error IS NOT NULL AND TRIM(error) != '' "
            f"GROUP BY error ORDER BY n DESC"
        ):
            error = str(row["error"] or "")
            key = (_signature(error), error_host(error))
            item = merged.setdefault(
                key, {"error": key[0], "host": key[1], "count": 0,
                      "size_mb": 0.0, "examples": [], "_raw": []}
            )
            # Исходные тексты нужны, чтобы выбрать примеры: сигнатура их склеила,
            # а в базе они хранятся по-разному (номера запросов и т.п.).
            if len(item["_raw"]) < 50:
                item["_raw"].append(error)
            item["count"] += int(row["n"])
            item["size_mb"] = round(
                item["size_mb"] + _mb(row["b"]), 2
            )
        ordered = sorted(merged.values(), key=lambda i: i["count"], reverse=True)
        for item in ordered[:FAILURE_GROUPS_MAX]:
            # Примеры берём по самому частому исходному тексту группы: они
            # нужны, чтобы глазами увидеть, что именно не качается.
            raws = item["_raw"]
            try:
                placeholders = ",".join("?" * len(raws))
                rows = conn.execute(
                    f'SELECT "{path_col}" AS p FROM "{table}" '
                    f"WHERE error IN ({placeholders}) LIMIT ?",
                    (*raws, FAILURE_EXAMPLES),
                ).fetchall()
                item["examples"] = [redact_text(str(r["p"])) for r in rows]
            except sqlite3.Error:
                pass
            item.pop("_raw", None)
        out["total"] = sum(item["count"] for item in ordered)
        out["groups"] = ordered[:FAILURE_GROUPS_MAX]
        out["groups_total"] = len(ordered)
    except sqlite3.Error as e:
        out["error"] = f"{type(e).__name__}: {e}"
    finally:
        if conn is not None:
            conn.close()
    return out


def http_errors_info() -> dict:
    """Последние HTTP-ошибки сервера вместе с телом и заголовками."""
    path = paths.HTTP_ERRORS_FILE
    if not path.exists():
        return {"exists": False, "items": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return {"exists": True, "error": f"{type(e).__name__}: {e}", "items": []}
    items = data if isinstance(data, list) else []
    return {
        "exists": True,
        "items": [_redact_obj(item) for item in items[-20:]],
        # Повторяющиеся статусы — быстрый ответ «это 500 на всём шарде».
        "status_counts": _count_by(items, "status"),
    }


def _count_by(items: list, field: str) -> dict:
    out: dict[str, int] = {}
    for item in items:
        if isinstance(item, dict) and item.get(field) is not None:
            key = str(item[field])
            out[key] = out.get(key, 0) + 1
    return out


def volumes_info() -> list[dict]:
    """Все тома с буквами и свободным местом.

    Отключённый внешний диск видно сразу: буква есть, а места нет.
    """
    out: list[dict] = []
    if os.name != "nt":
        usage = _disk_usage("/")
        item = {k: v for k, v in usage.items() if k != "path"}
        item["path"] = "/"
        out.append(item)
        return out
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        buffer = ctypes.create_unicode_buffer(512)
        if not kernel32.GetLogicalDriveStringsW(511, buffer):
            return out
        for letter in str(buffer[:]).split("\x00"):
            if not letter:
                continue
            free = ctypes.c_ulonglong(0)
            total = ctypes.c_ulonglong(0)
            ok = kernel32.GetDiskFreeSpaceExW(
                ctypes.c_wchar_p(letter),
                ctypes.byref(free),
                ctypes.byref(total),
                None,
            )
            item: dict[str, Any] = {"path": letter, "ready": bool(ok)}
            if ok:
                item["total_gb"] = _gb(total.value)
                item["free_gb"] = _gb(free.value)
            out.append(item)
    except Exception as e:  # noqa: BLE001 — отчёт не должен падать из-за томов
        out.append({"error": f"{type(e).__name__}: {e}"})
    return out


def path_access(path_value: str) -> dict:
    """Существует ли папка выгрузки и можно ли в неё писать."""
    out: dict[str, Any] = {"path": path_value or "", "exists": False,
                           "is_dir": False, "writable": False}
    if not path_value:
        return out
    folder = Path(path_value).expanduser()
    out["exists"] = folder.exists()
    out["is_dir"] = folder.is_dir()
    probe_dir = folder if out["is_dir"] else folder.parent
    if out["exists"] and not out["is_dir"]:
        out["error"] = "путь существует, но это не папка"
    if not probe_dir.exists():
        return out
    try:
        import tempfile
        with tempfile.NamedTemporaryFile(dir=str(probe_dir), delete=True):
            pass
        out["writable"] = True
    except OSError as e:
        out["write_error"] = f"{type(e).__name__}: {e}"
    return out


def launch_failures(task_status: dict | None = None) -> list[dict]:
    """Упавшие запуски фоновых задач: код возврата и последние строки."""
    out: list[dict] = []
    for slot, data in (task_status or {}).items():
        if not isinstance(data, dict):
            continue
        code = data.get("exit_code")
        if code in (None, 0):
            continue
        tail = [redact_text(str(x)) for x in list(data.get("log_tail") or [])]
        out.append({
            "slot": str(slot),
            "exit_code": code,
            "elapsed_sec": round(float(data.get("elapsed") or 0), 1),
            "stderr_tail": tail[-15:],
        })
    return out


def crash_info() -> dict:
    """Необработанные исключения, записанные хуком."""
    tail = read_tail(paths.CRASH_LOG, max_bytes=64 * 1024, max_lines=80)
    return {"exists": tail["exists"], "size_mb": _mb(tail["size"]),
            "lines": tail["lines"]}


def _previous_report() -> dict | None:
    """report.json предыдущего сохранённого отчёта (для сравнения)."""
    try:
        files = sorted(
            paths.REPORT_DIR.glob("diagnostics_*.zip"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return None
    for path in files:
        try:
            with zipfile.ZipFile(path) as zf:
                return json.loads(zf.read("report.json").decode("utf-8"))
        except (OSError, KeyError, ValueError, zipfile.BadZipFile):
            continue
    return None


def _counters(report: dict) -> dict[str, int]:
    out: dict[str, int] = {}
    for key, name in (("disk_download", "disk"), ("unlim_download", "unlim")):
        st = (report.get("statuses") or {}).get(key) or {}
        if not isinstance(st, dict):
            continue
        for field in ("downloaded", "success", "skipped", "failed", "pending"):
            if isinstance(st.get(field), int):
                out[f"{name}.{field}"] = st[field]
    for key, name in (("state.db", "disk_db"), ("unlim_state.db", "unlim_db")):
        db = (report.get("databases") or {}).get(key) or {}
        for field, value in (db.get("status") or {}).items():
            if isinstance(value, int):
                out[f"{name}.{field}"] = value
            elif isinstance(value, str):
                try:
                    out[f"{name}.{field}"] = int(value)
                except ValueError:
                    continue
    return out


def report_delta(report: dict) -> dict:
    """Что изменилось с прошлого отчёта: новые сбои, докачанное, правки базы."""
    previous = _previous_report()
    if not previous:
        return {"available": False}
    now, before = _counters(report), _counters(previous)
    changes = {}
    for key in sorted(set(now) | set(before)):
        was, still = before.get(key, 0), now.get(key, 0)
        if was != still:
            changes[key] = {"was": was, "now": still, "delta": still - was}
    return {
        "available": True,
        "previous_report_id": previous.get("report_id"),
        "previous_created_at": previous.get("created_at"),
        "changes": changes,
    }


def task_info(task_status: dict | None = None,
              auto_status: dict | None = None) -> dict:
    """Сжимает статусы процессов до безопасного минимума."""
    out: dict[str, Any] = {"tasks": {}, "full_cycle": {}}
    for slot, data in (task_status or {}).items():
        if not isinstance(data, dict):
            continue
        tail = data.get("log_tail") or []
        out["tasks"][str(slot)] = {
            "running": bool(data.get("running")),
            "elapsed_sec": round(float(data.get("elapsed") or 0), 1),
            "exit_code": data.get("exit_code"),
            "log_tail": [redact_text(x) for x in list(tail)[-20:]],
        }
    if isinstance(auto_status, dict):
        lanes = {}
        for name, lane in (auto_status.get("lanes") or {}).items():
            if isinstance(lane, dict):
                lanes[str(name)] = {
                    "state": lane.get("state"),
                    "pass": lane.get("pass"),
                    "downloaded": lane.get("downloaded"),
                    "errors": lane.get("errors"),
                    "bytes": lane.get("bytes"),
                }
        out["full_cycle"] = {
            "state": auto_status.get("state"),
            "running": auto_status.get("running"),
            "step": auto_status.get("step"),
            "passes": auto_status.get("passes"),
            "lanes": lanes,
            "log_tail": [
                redact_text(x) for x in list(auto_status.get("log") or [])[-20:]
            ],
        }
    return out


def build_report(task_status: dict | None = None,
                 auto_status: dict | None = None) -> dict:
    created = datetime.now().astimezone()
    report = {
        "report_id": f"R-{created:%Y%m%d-%H%M%S}-{secrets.token_hex(2)}",
        "created_at": created.isoformat(timespec="seconds"),
        "app": version.version_info(),
        "install_id": install_id(),
        "system": system_info(),
        "paths": paths.describe(),
        "config": config_info(),
        "databases": {
            "state.db": database_info(paths.DB_FILE, "files"),
            "unlim_state.db": database_info(paths.UNLIM_DB_FILE, "unlim_files"),
        },
        "statuses": status_info(),
        "tasks": task_info(task_status, auto_status),
        "logs": logs_info(),
        "failures": {
            "disk": failure_groups(paths.DB_FILE, "files", "remote_path"),
            "unlim": failure_groups(paths.UNLIM_DB_FILE, "unlim_files", "name"),
        },
        "http_errors": http_errors_info(),
        "volumes": volumes_info(),
        "launch_failures": launch_failures(task_status),
        "crashes": crash_info(),
    }
    cfg = report["config"]
    report["output_paths"] = {
        "disk_dir": path_access(cfg.get("disk_dir", "")),
        "unlim_dir": path_access(cfg.get("unlim_dir", "")),
    }
    report["disk_usage"] = {
        "data_dir": _disk_usage(str(paths.DATA_DIR)),
        "disk_dir": _disk_usage(report["config"].get("disk_dir", "")),
        "unlim_dir": _disk_usage(report["config"].get("unlim_dir", "")),
    }
    report["delta"] = report_delta(report)
    return _redact_obj(report)


def render_summary(report: dict) -> str:
    """Короткая читаемая сводка для экрана и report.txt."""
    sysinfo = report.get("system") or {}
    cfg = report.get("config") or {}
    ram = sysinfo.get("ram") or {}
    lines: list[str] = [
        f"ОТЧЁТ {version.APP_NAME} {version.APP_VERSION}",
        f"ID отчёта: {report.get('report_id', '?')}",
        f"Установка: {report.get('install_id', '?')}",
        f"Дата: {report.get('created_at', '?')}",
        "",
        "СИСТЕМА",
        f"  ОС: {sysinfo.get('platform', '?')}",
        f"  Компьютер: {sysinfo.get('hostname', '?')} · {sysinfo.get('machine', '?')}",
        f"  Python: {sysinfo.get('python', '?')} "
        f"({sysinfo.get('python_implementation', '?')}, "
        f"{sysinfo.get('python_bits', '?')} бит), "
        f"собранная версия: {'да' if sysinfo.get('frozen') else 'нет'}",
        f"  Память: {ram.get('total_gb', '?')} ГБ, "
        f"свободно {ram.get('available_gb', '?')} ГБ; "
        f"рекомендовано потоков: {ram.get('recommended_workers', '?')}",
        f"  Права администратора: "
        f"{'да' if sysinfo.get('is_admin') else 'нет'}",
        "",
        "ПУТИ",
    ]
    for key in ("app_dir", "data_dir", "state_db", "unlim_db", "cookies"):
        lines.append(f"  {key}: {report.get('paths', {}).get(key, '?')}")

    lines += ["", "МЕСТО НА ДИСКАХ"]
    for key, label in (("data_dir", "данные"), ("disk_dir", "Диск"),
                       ("unlim_dir", "Безлимит")):
        usage = (report.get("disk_usage") or {}).get(key) or {}
        lines.append(
            f"  {label}: {usage.get('path', '?')} — "
            f"свободно {usage.get('free_gb', '?')} из {usage.get('total_gb', '?')} ГБ"
            + ("" if usage.get("exists", True) else " (папки ещё нет)")
        )

    lines += [
        "",
        "НАСТРОЙКИ",
        f"  Токен: {cfg.get('token_masked') or 'не задан'}",
        f"  Папка Диска: {cfg.get('disk_dir', '?')}",
        f"  Папка Безлимита: {cfg.get('unlim_dir', '?')}",
        f"  Куки: {'есть' if (cfg.get('cookies') or {}).get('exists') else 'нет'}"
        f" ({(cfg.get('cookies') or {}).get('count', 0)} шт., "
        f"обязательные: "
        f"{'да' if (cfg.get('cookies') or {}).get('important_present') else 'нет'})",
        f"  Ключ отправки отчётов: "
        f"{'настроен' if cfg.get('report_key_configured') else 'НЕ настроен'}",
        "",
        "БАЗЫ",
    ]
    for name, db in (report.get("databases") or {}).items():
        if not isinstance(db, dict):
            continue
        if not db.get("exists"):
            lines.append(f"  {name}: нет файла")
            continue
        statuses = ", ".join(f"{k}={v}" for k, v in (db.get("status") or {}).items())
        total = db.get("total") or 0
        lines.append(
            f"  {name}: {db.get('size_mb', 0)} МБ, схема v{db.get('schema_version')}"
            + (f", всего {total}" if total else "")
            + (f", {statuses}" if statuses else "")
            + (f", ОШИБКА: {db.get('error')}" if db.get("error") else "")
        )

    lines += ["", "СТАТУСЫ ВЫГРУЗКИ"]
    for key, label in (("disk_download", "Диск"),
                       ("unlim_download", "Безлимит"),
                       ("cookies_fetch", "Куки")):
        st = (report.get("statuses") or {}).get(key) or {}
        if not st.get("exists"):
            lines.append(f"  {label}: файла статуса нет")
            continue
        parts = []
        for field in ("active", "downloaded", "success", "skipped", "failed",
                      "total_files", "items_total", "item_current", "state"):
            if field in st and st[field] not in ("", None):
                parts.append(f"{field}={st[field]}")
        lines.append(f"  {label}: " + (", ".join(parts) if parts else "данные есть"))

    lines += ["", "ПОСЛЕДНИЕ ЗАДАЧИ"]
    tasks = (report.get("tasks") or {}).get("tasks") or {}
    if tasks:
        for slot, data in tasks.items():
            lines.append(
                f"  {slot}: {'идёт' if data.get('running') else 'остановлена'}"
                f", {data.get('elapsed_sec', 0)} сек"
            )
    else:
        lines.append("  нет данных (задачи не запускались в этом процессе)")
    auto = (report.get("tasks") or {}).get("full_cycle") or {}
    if auto:
        lines.append(f"  полный цикл: {auto.get('state', '?')}")

    lines += ["", "ЛОГИ"]
    for name, info in (report.get("logs", {}).get("files") or {}).items():
        if info.get("exists"):
            lines.append(f"  {name}: {info.get('size_mb', 0)} МБ")

    errors = (report.get("logs") or {}).get("errors") or []
    lines += ["", "ПОСЛЕДНИЕ ОШИБКИ"]
    if errors:
        for item in errors[:20]:
            lines.append(f"  [{item.get('log', '?')}] {item.get('line', '')}")
    else:
        lines.append("  не найдено")

    lines += ["", "СБОИ ПО ГРУППАМ"]
    any_group = False
    for key, label in (("disk", "Диск"), ("unlim", "Безлимит")):
        block = (report.get("failures") or {}).get(key) or {}
        groups = block.get("groups") or []
        lines.append(f"  {label}: всего {block.get('total', 0)}")
        for group in groups[:6]:
            host = group.get("host") or "—"
            lines.append(
                f"    {group.get('count', 0)} × {group.get('error', '?')} "
                f"[{host}], {group.get('size_mb', 0)} МБ"
            )
            any_group = True
    if not any_group:
        lines.append("  групп сбоев нет")

    http_errors = report.get("http_errors") or {}
    if http_errors.get("items"):
        lines += ["", "ОТВЕТЫ СЕРВЕРА (последние)"]
        for item in http_errors["items"][-5:]:
            headers = ", ".join(
                f"{k}={v}" for k, v in item.items()
                if k in ("retry-after", "x-request-id", "server")
            )
            lines.append(
                f"  HTTP {item.get('status', '?')} {item.get('path', '')}"
                + (f" — {headers}" if headers else "")
            )
            body = str(item.get("body_head") or "").strip().replace("\n", " ")
            if body:
                lines.append(f"    тело: {body[:160]}")

    delta = report.get("delta") or {}
    if delta.get("available"):
        lines += ["", f"СРАВНЕНИЕ С ПРОШЛЫМ ОТЧЁТОМ ({delta.get('previous_created_at', '?')})"]
        changes = delta.get("changes") or {}
        if changes:
            for key, item in list(changes.items())[:15]:
                lines.append(
                    f"  {key}: {item.get('was')} → {item.get('now')} "
                    f"({item.get('delta'):+d})"
                )
        else:
            lines.append("  без изменений")
    else:
        lines += ["", "СРАВНЕНИЕ С ПРОШЛЫМ ОТЧЁТОМ", "  прошлых отчётов нет"]

    lines += ["", "ПАПКИ ВЫГРУЗКИ"]
    for key, label in (("disk_dir", "Диск"), ("unlim_dir", "Безлимит")):
        info = (report.get("output_paths") or {}).get(key) or {}
        state = "ок" if info.get("writable") else (
            "НЕТ ПАПКИ" if not info.get("exists") else "НЕТ ЗАПИСИ"
        )
        lines.append(f"  {label}: {info.get('path', '?')} — {state}")
        if info.get("write_error"):
            lines.append(f"    {info['write_error']}")

    volumes = report.get("volumes") or []
    if volumes:
        lines += ["", "ТОМА"]
        for volume in volumes[:12]:
            if volume.get("error"):
                lines.append(f"  ошибка: {volume['error']}")
                continue
            ready = "готов" if volume.get("ready", True) else "НЕ ГОТОВ"
            free = (f"свободно {volume['free_gb']} из {volume['total_gb']} ГБ"
                    if "free_gb" in volume else "нет данных о месте")
            lines.append(f"  {volume.get('path', '?')}: {ready}, {free}")

    crashes = (report.get("crashes") or {}).get("lines") or []
    if crashes:
        lines += ["", "НЕОБРАБОТАННЫЕ ИСКЛЮЧЕНИЯ"]
        lines.extend(f"  {line}" for line in crashes[-10:])

    lines += [
        "",
        "Полные хвосты логов, статусы и JSON — внутри ZIP.",
        "Токен, cookies и содержимое баз в отчёт не включаются.",
    ]
    return redact_text("\n".join(lines))


def write_report(report: dict | None = None) -> Path:
    """Сохраняет ZIP в REPORT_DIR и возвращает путь."""
    if report is None:
        report = build_report()
    paths.REPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_id = re.sub(r"[^0-9A-Za-z_-]+", "", str(report.get("install_id", "")))[:16]
    name = f"diagnostics_{stamp}_{safe_id or 'report'}.zip"
    target = paths.REPORT_DIR / name
    summary = render_summary(report)
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("report.json", payload)
        zf.writestr("report.txt", summary)
        zf.writestr(
            "README.txt",
            "Диагностический отчёт Yad.backup.\n"
            "report.txt — краткая сводка, report.json — полные данные.\n"
            "logs/ — последние 2 МБ каждого журнала (полные логи не входят).\n"
            "Секреты и содержимое файлов в отчёт не включаются.\n",
        )
        for name, path in LOG_FILES:
            sample = read_tail(path, max_bytes=LOG_FILE_BYTES, max_lines=20000)
            if sample["exists"] and sample["lines"]:
                zf.writestr(f"logs/{name}", "\n".join(sample["lines"]))
    return target


def _read_report_key() -> str:
    env_key = (os.getenv("REPORT_KEY") or "").strip()
    if env_key:
        return env_key
    for key_file in (paths.REPORT_KEY_FILE, paths.BUNDLED_REPORT_KEY_FILE):
        try:
            if key_file.exists():
                value = key_file.read_text(encoding="utf-8").strip()
                if value:
                    return value
        except OSError:
            pass
    return ""


def save_report_key(key: str) -> None:
    """Сохраняет ключ отправки в DATA_DIR (пустая строка — удалить)."""
    value = (key or "").strip()
    if not value:
        try:
            paths.REPORT_KEY_FILE.unlink()
        except FileNotFoundError:
            pass
        return
    paths.REPORT_KEY_FILE.write_text(value, encoding="utf-8")
    try:
        os.chmod(paths.REPORT_KEY_FILE, 0o600)
    except OSError:
        pass


def report_key_configured() -> bool:
    return bool(_read_report_key())


def report_key() -> str:
    """Ключ отправки. Никогда не логировать и не показывать пользователю."""
    return _read_report_key()


def _report_url() -> str:
    return (os.getenv("REPORT_URL") or DEFAULT_REPORT_URL).strip().rstrip("/")


def report_url() -> str:
    """Публичный адрес сервера отчётов (для мониторинга и настроек)."""
    return _report_url()


def send_report(path: Path | None = None,
                report: dict | None = None) -> dict:
    """Отправляет ZIP через Cloudflare Worker в Telegram.

    Если ключ не настроен — отчёт остаётся на диске, ошибка не бросается.
    """
    if report is None:
        report = build_report()
    if path is None:
        path = write_report(report)
    path = Path(path)
    result = {
        "ok": False,
        "saved": str(path),
        "report_id": report.get("report_id"),
        "file_name": path.name,
        "size": _size_h(path),
    }
    key = _read_report_key()
    if not key:
        result["error"] = ("Ключ отправки не настроен. Отчёт сохранён на диске — "
                           "его можно передать вручную.")
        return result
    if result["size"] > MAX_UPLOAD_BYTES:
        result["error"] = "Отчёт больше 45 МБ — отправка невозможна."
        return result
    try:
        import requests
        with path.open("rb") as fh:
            response = requests.post(
                f"{_report_url()}/report",
                headers={"X-Report-Key": key},
                data={
                    "app_version": version.APP_VERSION,
                    "report_id": str(report.get("report_id", "")),
                    "machine_id": str(report.get("install_id", "")),
                },
                files={"file": (path.name, fh, "application/zip")},
                timeout=(15, 180),
            )
    except Exception as e:
        # Не показываем URL/заголовки: в тексте исключения их быть не должно.
        result["error"] = f"Не удалось связаться с сервером отчётов: {type(e).__name__}"
        return result
    try:
        data = response.json()
    except Exception:
        data = {}
    if response.status_code == 200 and data.get("ok"):
        result.update({
            "ok": True,
            "message_id": data.get("message_id"),
            "file_name": data.get("file_name", path.name),
        })
        return result
    error = data.get("error") or f"HTTP {response.status_code}"
    result["error"] = redact_text(str(error))[:300]
    return result


def latest_reports(limit: int = 10) -> list[dict]:
    """Список последних отчётов для интерфейса."""
    items: list[dict] = []
    try:
        files = sorted(
            paths.REPORT_DIR.glob("diagnostics_*.zip"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return items
    for path in files[:limit]:
        items.append({
            "name": path.name,
            "size_mb": _mb(_size_h(path)),
            "modified": datetime.fromtimestamp(path.stat().st_mtime)
                                .astimezone().isoformat(timespec="seconds"),
        })
    return items


# ── необработанные исключения ───────────────────────────────────────────────

CRASH_LOG_MAX_BYTES = 512 * 1024
_crash_lock = threading.Lock()
_crash_report_sent = False


def _append_crash(kind: str, text: str) -> None:
    """Пишет исключение в `logs/crash.log`, обрезая файл по размеру."""
    try:
        with _crash_lock:
            if paths.CRASH_LOG.exists() and paths.CRASH_LOG.stat().st_size > CRASH_LOG_MAX_BYTES:
                tail = paths.CRASH_LOG.read_bytes()[-100_000:]
                paths.CRASH_LOG.write_bytes(tail)
            with paths.CRASH_LOG.open("a", encoding="utf-8") as fh:
                fh.write(f"\n=== {_now_iso()} {kind} ===\n{text}\n")
    except OSError:
        pass


def _auto_crash_report() -> None:
    """Один раз за процесс отправляет отчёт после падения (если ключ настроен)."""
    global _crash_report_sent
    if not report_key_configured():
        return
    with _crash_lock:
        if _crash_report_sent:
            return
        _crash_report_sent = True
    try:
        send_report()  # build_report подхватит хвост crash.log
    except Exception:  # noqa: BLE001 - в обработчике падения падать нельзя
        pass


def _handle_exception(exc_type, exc, tb, kind: str) -> None:
    if issubclass(exc_type, KeyboardInterrupt):
        return
    text = redact_text("".join(traceback.format_exception(exc_type, exc, tb)))
    _append_crash(kind, text[:20000])
    if report_key_configured():
        # Отправляем в отдельном потоке, но ждём его: при падении главного
        # потока процесс завершится сразу после возврата из хука.
        worker = threading.Thread(target=_auto_crash_report,
                                  name="crash-report", daemon=True)
        worker.start()
        worker.join(timeout=60)


def install_crash_hook() -> None:
    """Ставит хуки исключений: пишет в crash.log и шлёт автоотчёт.

    Вызывается в точке входа. Идемпотентна: повторный вызов ничего не меняет.
    """
    if getattr(sys, "excepthook", None) is _sys_excepthook:
        return
    sys.excepthook = _sys_excepthook
    threading.excepthook = _thread_excepthook


def _sys_excepthook(exc_type, exc, tb) -> None:
    _handle_exception(exc_type, exc, tb, "main")
    if not issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc, tb)


def _thread_excepthook(args) -> None:
    name = getattr(args.thread, "name", "?")
    _handle_exception(args.exc_type, args.exc_value, args.exc_traceback,
                      f"thread {name}")


if __name__ == "__main__":  # ручная проверка: python diagnostics.py
    report = build_report()
    target = write_report(report)
    print(render_summary(report))
    print(f"\nZIP: {target} ({_mb(_size_h(target))} МБ)")
