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


def logs_info() -> dict:
    log_files = [
        ("backup.log", paths.BACKUP_LOG),
        ("unlim_backup.log", paths.UNLIM_LOG),
        ("unlim_worker.log", paths.UNLIM_WORKER_LOG),
        ("unlim.log", paths.UNLIM_COLLECT_LOG),
        ("unlim_direct.log", paths.UNLIM_DIRECT_LOG),
        ("webui_actions.log", paths.ACTION_LOG),
        ("migrations.log", paths.MIGRATIONS_LOG),
        ("launcher.log", paths.LAUNCHER_LOG),
        ("launcher_output.log", paths.LAUNCHER_OUTPUT_LOG),
    ]
    out: dict[str, Any] = {"files": {}, "tails": {}, "errors": []}
    for name, path in log_files:
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
    }
    report["disk_usage"] = {
        "data_dir": _disk_usage(str(paths.DATA_DIR)),
        "disk_dir": _disk_usage(report["config"].get("disk_dir", "")),
        "unlim_dir": _disk_usage(report["config"].get("unlim_dir", "")),
    }
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
            "Секреты и содержимое файлов в отчёт не включаются.\n",
        )
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


def _report_url() -> str:
    return (os.getenv("REPORT_URL") or DEFAULT_REPORT_URL).strip().rstrip("/")


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


if __name__ == "__main__":  # ручная проверка: python diagnostics.py
    report = build_report()
    target = write_report(report)
    print(render_summary(report))
    print(f"\nZIP: {target} ({_mb(_size_h(target))} МБ)")
