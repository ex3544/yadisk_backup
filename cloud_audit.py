"""Сверка с облаком: что реально лежит в Яндексе и как это сходится с базой.

Зачем отдельный модуль
----------------------
`scan` и «Пересоздать базу» собирают список заново и стирают статусы, а
`verify` смотрит только диск, в облако не ходит. Здесь третий, безопасный
режим: обойти облако онлайн, ничего не меняя, и сравнить с базой:

  * ✅ совпадает — путь (или id файла) есть в облаке и в базе, размер тот же;
  * ➕ новое — файл есть в облаке, а в базе его нет;
  * 🔁 изменилось — файл есть в обоих, но размер или MD5 другие;
  * ➖ пропало — файл есть в базе, а из облака исчез.

Сверка ничего не скачивает и не удаляет. Действия по её итогу делают
отдельные кнопки: «Добавить новые в очередь» и «Пометить пропавшие».
Оба действия тоже ничего не удаляют: старые копии изменившихся файлов
переименовываются с припиской «_старая», а файлы на диске остаются.
"""

from __future__ import annotations

import csv
import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Callable, Optional

import paths
from utils import human_bytes, human_duration, volume_ready

SCOPES = ("disk", "unlim")
SCOPE_NAMES = {"disk": "Диск", "unlim": "Безлимит", "both": "Обе базы"}
DB_TABLES = {"disk": "files", "unlim": "unlim_files"}
DB_KEYS = {"disk": "remote_path", "unlim": "file_id"}
KIND_NAMES = {
    "same": "совпадает",
    "new": "новое",
    "changed": "изменилось",
    "gone": "пропало",
}
OLD_SUFFIX = "_старая"
# Как часто переписывать статус во время долгого обхода облака.
STATUS_PAUSE = 1.5
# Сколько записей отдаём в окно. Полный список лежит в плане и CSV, а JSON
# статуса читается каждые 1,5 секунды — километровый список его бы только
# замедлил.
ITEMS_LIMIT = 500
# Сколько первых строк каждого списка печатаем в текстовом отчёте.
REPORT_LIMIT = 100
# Размер действия: UPDATE/DELETE пачками, чтобы не упереться в лимит параметров.
CHUNK = 300


class AuditError(ValueError):
    """Понятная человеку причина, почему сверка или действие невозможны."""


# ── чтение токена и куки ────────────────────────────────────────────────────


def read_token() -> str:
    """Токен Диска из окружения или .env. Наружу значение не выводится."""
    token = (os.getenv("YA_DISK_TOKEN") or "").strip()
    if token:
        return token
    env_file = paths.ENV_FILE
    try:
        if env_file.exists():
            for line in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line.startswith("YA_DISK_TOKEN"):
                    continue
                _, _, value = line.partition("=")
                return value.strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


# ── обход облака: Диск ──────────────────────────────────────────────────────


def scan_disk_cloud(
    token: str,
    on_progress: Optional[Callable[[str, int], None]] = None,
    on_error: Optional[Callable[[str], None]] = None,
) -> dict[str, dict]:
    """Обходит всё дерево обычного Диска и возвращает {путь: {name,size,md5}}.

    Ничего не пишет в базу и не скачивает: только читает список файлов.
    """
    if not token:
        raise AuditError(
            "Не задан токен Яндекс.Диска. Откройте «Настройки» и вставьте токен."
        )
    try:
        import yadisk
    except Exception as exc:  # noqa: BLE001
        raise AuditError(f"Библиотека yadisk недоступна: {exc}") from exc

    from downloader import _is_skipped, _list_dir, _normalize_remote

    files: dict[str, dict] = {}
    queue = ["disk:/"]
    dirs = 0
    client = yadisk.Client(token=token)
    try:
        with client:
            while queue:
                cur = queue.pop()
                if _is_skipped(_normalize_remote(cur)):
                    continue
                try:
                    entries = _list_dir(client, cur)
                except yadisk.exceptions.UnauthorizedError as exc:
                    raise AuditError(
                        "Яндекс не принял токен: он истёк или отозван. "
                        "Обновите токен в «Настройках»."
                    ) from exc
                except yadisk.exceptions.PathNotFoundError:
                    continue
                except Exception as exc:  # noqa: BLE001 — обход продолжаем
                    if on_error is not None:
                        on_error(f"{cur}: {exc}")
                    continue
                dirs += 1
                for entry in entries:
                    entry_path = entry.path
                    if _is_skipped(entry_path):
                        continue
                    if entry.type == "dir":
                        queue.append(entry_path)
                    elif entry.type == "file":
                        name = entry_path.rsplit("/", 1)[-1]
                        files[entry_path] = {
                            "name": name,
                            "size": int(entry.size) if entry.size is not None else None,
                            "md5": getattr(entry, "md5", None),
                        }
                if on_progress is not None:
                    on_progress("disk", len(files))
    finally:
        try:
            client.close()
        except Exception:  # noqa: BLE001 — клиент мог не открыться
            pass
    return files


# ── обход облака: Безлимит ──────────────────────────────────────────────────


def scan_unlim_cloud(
    cookies_path: Path,
    on_progress: Optional[Callable[[str, int], None]] = None,
    on_error: Optional[Callable[[str], None]] = None,
) -> dict[str, dict]:
    """Читает список всех медиа Безлимита и возвращает {file_id: {name,size,md5}}.

    Повторяет запросы штатного сбора базы, но не пишет ни кластеры, ни файлы:
    это только чтение.
    """
    if not cookies_path.exists():
        raise AuditError(
            "Не найден файл куки Безлимита. Загрузите его в «Настройках»."
        )
    import unlim_backup as ub

    try:
        cookies = ub.load_cookies(cookies_path)
        session = ub.make_session(cookies)
        sk, uid = ub.get_sk_uid(session)
    except SystemExit as exc:  # функции печатают причину и выходят
        raise AuditError(str(exc).lstrip("[-] ").strip() or "Куки не приняты") from exc
    except Exception as exc:  # noqa: BLE001
        raise AuditError(f"Не удалось войти в Безлимит по кукам: {exc}") from exc

    try:
        res = ub.api_call(session, sk, uid, "intapi/photo-init-snapshot")
        psid = res.get("photoslice_id")
        if not psid:
            raise AuditError("Яндекс не вернул слепок Безлимита — повторите позже.")
        snap = ub.api_call(
            session, sk, uid, "intapi/photo-get-snapshot",
            {"id": psid, "locale": "ru", "amount": 100000, "offset": 0},
        )
        clusters = [
            (str(it.get("id")), int(it.get("size") or 0))
            for it in (snap.get("items") or [])
            if it.get("id")
        ]
    except AuditError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise AuditError(f"Не удалось получить список Безлимита: {exc}") from exc

    files: dict[str, dict] = {}
    # Кластеры, которые сервер не вернул, пробуем ещё пару раз: без этого
    # часть файлов молча выпадала бы и попадала в «пропало».
    pending = list(clusters)
    tries: dict[str, int] = {}
    batch_size = 50
    while pending:
        batch = pending[:batch_size]
        pending = pending[batch_size:]
        request = {
            cid: {"range": [0, max(size - 1, 0)]}
            for cid, size in batch
        }
        try:
            res = ub.api_call(
                session, sk, uid, "intapi/photo-get-clusters-with-resources",
                {"photosliceId": psid, "clusters": request, "hideScreenshots": False},
            )
        except SystemExit as exc:
            raise AuditError(str(exc).lstrip("[-] ").strip() or "Сессия истекла") from exc
        except Exception as exc:  # noqa: BLE001
            if on_error is not None:
                on_error(f"кластеры {batch[0][0]}…: {exc}")
            continue

        fetched = (res.get("clusters") or {}).get("fetched") or []
        returned = set()
        for cluster in fetched:
            cid = cluster.get("id")
            if cid is not None:
                returned.add(str(cid))
            for item in cluster.get("items") or []:
                fid = item.get("id") or item.get("resource_id")
                if not fid:
                    continue
                # Яндекс для Безлимита больше не отдаёт размер: в ответе
                # только id, itemId, width и height. Раньше отсутствие
                # превращалось в 0, и сверка объявляла все скачанные файлы
                # «изменившимися» (в базе настоящий размер, в облаке 0).
                # None = «размер неизвестен», сравниваем только наличие.
                raw_size = item.get("size")
                size = int(raw_size) if raw_size not in (None, 0, "0", "") else None
                files[str(fid)] = {
                    "name": item.get("name") or str(fid).rsplit("/", 1)[-1],
                    "size": size,
                    "md5": item.get("md5") or item.get("etag"),
                }
        for cid, _size in batch:
            if cid in returned:
                tries.pop(cid, None)
                continue
            tries[cid] = tries.get(cid, 0) + 1
            if tries[cid] < 3:
                pending.append((cid, _size))
            elif on_error is not None:
                on_error(f"кластер {cid}: Яндекс не вернул его три раза")
        if on_progress is not None:
            on_progress("unlim", len(files))
    return files


# ── база ────────────────────────────────────────────────────────────────────


def load_db_rows(scope: str) -> dict[str, dict]:
    """Все строки базы: {ключ: {name,size,md5,status,local}}.

    Берём не только скачанные: файл, который ещё ждёт очереди, тоже должен
    сверяться с облаком — иначе он выглядел бы как «новое».
    """
    scope = (scope or "").strip().lower()
    if scope not in SCOPES:
        raise AuditError(f"Неизвестная база: {scope}")
    db_file = paths.DB_FILE if scope == "disk" else paths.UNLIM_DB_FILE
    if not db_file.exists():
        return {}
    table = DB_TABLES[scope]
    key_col = DB_KEYS[scope]
    name_sql = "name" if scope == "unlim" else "NULL"
    con = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True, timeout=30)
    con.row_factory = sqlite3.Row
    try:
        rows = list(
            con.execute(
                f"SELECT {key_col} AS key, {name_sql} AS name, size, md5, "
                f"status, local_path, error FROM {table}"
            )
        )
    except sqlite3.Error:
        # Старая база без local_path (теоретически) — читаем без него.
        rows = list(
            con.execute(
                f"SELECT {key_col} AS key, {name_sql} AS name, size, md5, "
                f"status, NULL AS local_path, error FROM {table}"
            )
        )
    finally:
        con.close()
    out: dict[str, dict] = {}
    for row in rows:
        key = row["key"]
        if not key:
            continue
        out[str(key)] = {
            "name": row["name"] or str(key).rsplit("/", 1)[-1],
            "size": row["size"],
            "md5": row["md5"],
            "status": row["status"] or "pending",
            "local": row["local_path"],
            "error": row["error"],
        }
    return out


def _differs(cloud: dict, row: dict) -> bool:
    """Отличается ли файл в облаке от того, что записано в базе.

    Если облако размер не сообщило (None), размер не сравниваем вовсе:
    иначе отсутствие данных выдавалось бы за изменение файла.
    """
    csize = cloud.get("size")
    dsize = row.get("size")
    if csize is not None and dsize is not None and int(csize) != int(dsize):
        return True
    cmd5 = (cloud.get("md5") or "").lower()
    dmd5 = (row.get("md5") or "").lower()
    if cmd5 and dmd5 and cmd5 != dmd5:
        return True
    return False


def _same_detail(info: dict, row: dict) -> str:
    """Пояснение для «совпадает»: чего сравнить не смогли и как дела у файла."""
    parts: list[str] = []
    if info.get("size") is None and row.get("size") not in (None, 0):
        parts.append("размер в облаке не отдаётся — сверено по наличию")
    status = row.get("status")
    if status == "lost":
        parts.append("отложен сервером Яндекса")
    elif status in ("pending", "in_progress") and str(
        row.get("error") or ""
    ).startswith("storage_down"):
        parts.append("ждёт Яндекса")
    return "; ".join(parts)


def compare(
    scope: str,
    cloud: dict[str, dict],
    db_rows: dict[str, dict],
) -> list[dict]:
    """Сравнивает облако с базой. Возвращает полный список записей."""
    items: list[dict] = []
    for key, info in cloud.items():
        row = db_rows.get(key)
        if row is None or row.get("status") == "deleted":
            kind = "new"
            detail = "в базе такого файла нет" if row is None else "в базе был помечен удалённым"
        elif _differs(info, row):
            kind = "changed"
            parts = []
            if info.get("size") is not None and row.get("size") is not None \
                    and int(info["size"]) != int(row["size"]):
                parts.append(
                    f"размер {human_bytes(int(row['size']))} → "
                    f"{human_bytes(int(info['size']))}"
                )
            else:
                parts.append("изменилось содержимое (MD5)")
            detail = "; ".join(parts)
        else:
            kind = "same"
            detail = _same_detail(info, row)
        items.append({
            "scope": scope,
            "key": key,
            "name": info.get("name") or str(key).rsplit("/", 1)[-1],
            "kind": kind,
            "size": info.get("size"),
            "db_size": (row or {}).get("size"),
            "md5": info.get("md5"),
            "db_md5": (row or {}).get("md5"),
            "local": (row or {}).get("local"),
            "db_status": (row or {}).get("status"),
            "detail": detail,
        })
    for key, row in db_rows.items():
        if key in cloud or row.get("status") == "deleted":
            continue
        items.append({
            "scope": scope,
            "key": key,
            "name": row.get("name") or str(key).rsplit("/", 1)[-1],
            "kind": "gone",
            "size": None,
            "db_size": row.get("size"),
            "md5": None,
            "db_md5": row.get("md5"),
            "local": row.get("local"),
            "db_status": row.get("status"),
            "detail": "файла больше нет в облаке",
        })
    return items


# ── статус, отчёт, CSV, план ────────────────────────────────────────────────


def _write_json(path: Path, data) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except Exception:  # noqa: BLE001 — статус не должен ронять сверку
        pass


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return default


def read_status() -> dict:
    """Последний статус сверки. Пустой словарь, если сверки ещё не было."""
    return _read_json(paths.CLOUD_AUDIT_STATUS_FILE, {})


def read_actions() -> list[dict]:
    data = _read_json(paths.CLOUD_AUDIT_ACTIONS_FILE, [])
    return data if isinstance(data, list) else []


def _scope_list(scope: str) -> list[str]:
    return list(SCOPES) if scope == "both" else [scope]


def _counts(items: list[dict]) -> dict:
    out = {"same": 0, "new": 0, "changed": 0, "gone": 0}
    for item in items:
        kind = item.get("kind")
        if kind in out:
            out[kind] += 1
    return out


def _db_status_counts(rows: dict[str, dict]) -> dict[str, int]:
    """Сколько файлов базы в каждом статусе: downloaded, lost, pending…"""
    out: dict[str, int] = {}
    for row in rows.values():
        status = row.get("status") or "pending"
        out[status] = out.get(status, 0) + 1
    return out


def _waiting_count(rows: dict[str, dict]) -> int:
    """Файлы, которые ждут ответа сервера хранения (не сбойные)."""
    return sum(
        1 for row in rows.values()
        if row.get("status") in ("pending", "in_progress")
        and str(row.get("error") or "").startswith("storage_down")
    )


def _base_stats(files: dict[str, dict], rows: dict[str, dict]) -> dict:
    """Итоги одной базы: облако, база и разбивка по статусам."""
    statuses = _db_status_counts(rows)
    waiting = _waiting_count(rows)
    return {
        "cloud": len(files),
        "db": len(rows),
        "downloaded": statuses.get("downloaded", 0),
        "lost": statuses.get("lost", 0),
        "waiting": waiting,
        "pending": statuses.get("pending", 0) + statuses.get("in_progress", 0),
        "statuses": statuses,
        "cloud_size_unknown": sum(
            1 for info in files.values() if info.get("size") is None
        ),
    }


def _public_items(items: list[dict], kind: str) -> list[dict]:
    picked = [it for it in items if it.get("kind") == kind]
    return [
        {
            "scope": it.get("scope"),
            "key": it.get("key"),
            "name": it.get("name"),
            "size": it.get("size"),
            "db_size": it.get("db_size"),
            "local": it.get("local"),
            "detail": it.get("detail"),
        }
        for it in picked[:ITEMS_LIMIT]
    ]


def write_error(scope: str, message: str) -> None:
    """Записывает причину сбоя, чтобы окно показало её вместо «идёт»."""
    current = read_status()
    data = current if current.get("scope") == scope else {}
    data.update({
        "active": False,
        "scope": scope,
        "stage": "error",
        "error": str(message),
        "finished_at": time.time(),
        "updated_at": time.time(),
    })
    _write_json(paths.CLOUD_AUDIT_STATUS_FILE, data)


def write_stopped(scope: str) -> None:
    """Отмечает сверку остановленной: процесс убит, сам статус не обновит."""
    current = read_status()
    data = current if current.get("scope") == scope else {"scope": scope}
    now = time.time()
    data.update({
        "active": False,
        "stage": "stopped",
        "finished_at": now,
        "updated_at": now,
    })
    _write_json(paths.CLOUD_AUDIT_STATUS_FILE, data)


def run(
    scope: str = "both",
    *,
    on_progress: Optional[Callable[[str, int], None]] = None,
    on_error: Optional[Callable[[str], None]] = None,
) -> dict:
    """Обходит облако и сравнивает с базой. Возвращает итоговый статус."""
    scope = (scope or "both").strip().lower()
    if scope not in ("disk", "unlim", "both"):
        raise AuditError("Неизвестная база для сверки: ожидается disk, unlim или both")

    started = time.time()
    status: dict = {
        "active": True,
        "scope": scope,
        "stage": "disk" if scope in ("disk", "both") else "unlim",
        "started_at": started,
        "updated_at": started,
        "finished_at": None,
        "same": 0, "new": 0, "changed": 0, "gone": 0,
        "errors": [],
    }
    _write_json(paths.CLOUD_AUDIT_STATUS_FILE, status)
    last_write = [0.0]

    def report_error(text: str) -> None:
        status["errors"].append(str(text))
        del status["errors"][:-50]
        if on_error is not None:
            on_error(text)

    def progress(stage: str, count: int, total: Optional[int] = None) -> None:
        status["stage"] = stage
        status["cloud_files"] = count
        if total is not None:
            status["cloud_total"] = total
        now = time.time()
        if now - last_write[0] >= STATUS_PAUSE:
            last_write[0] = now
            status["updated_at"] = now
            _write_json(paths.CLOUD_AUDIT_STATUS_FILE, status)
        if on_progress is not None:
            on_progress(stage, count)

    cloud: dict[str, dict] = {}
    db_rows: dict[str, dict] = {}
    per_base: dict[str, dict] = {}
    try:
        for base in _scope_list(scope):
            status["stage"] = base
            status["updated_at"] = time.time()
            _write_json(paths.CLOUD_AUDIT_STATUS_FILE, status)
            if base == "disk":
                files = scan_disk_cloud(read_token(), on_progress=progress,
                                        on_error=report_error)
            else:
                files = scan_unlim_cloud(paths.COOKIES_FILE, on_progress=progress,
                                         on_error=report_error)
            rows = load_db_rows(base)
            per_base[base] = _base_stats(files, rows)
            for key, info in files.items():
                cloud[f"{base}:{key}"] = info
            for key, row in rows.items():
                db_rows[f"{base}:{key}"] = row
    except AuditError as exc:
        status.update(active=False, stage="error", error=str(exc),
                      finished_at=time.time(), updated_at=time.time(),
                      per_base=per_base)
        _write_json(paths.CLOUD_AUDIT_STATUS_FILE, status)
        raise

    # Сравнение идёт по базе отдельно: ключи Диска и Безлимита не пересекаются,
    # но и смешивать их в одной таблице незачем.
    items: list[dict] = []
    for base in _scope_list(scope):
        base_cloud = {
            key.split(":", 1)[1]: info
            for key, info in cloud.items() if key.startswith(base + ":")
        }
        base_db = {
            key.split(":", 1)[1]: row
            for key, row in db_rows.items() if key.startswith(base + ":")
        }
        items += compare(base, base_cloud, base_db)

    counts = _counts(items)
    finished = time.time()
    status.update({
        "active": False,
        "stage": "done",
        "finished_at": finished,
        "updated_at": finished,
        "same": counts["same"],
        "new": counts["new"],
        "changed": counts["changed"],
        "gone": counts["gone"],
        "cloud_files": len(cloud),
        "db_files": len(db_rows),
        "per_base": per_base,
        "new_items": _public_items(items, "new"),
        "changed_items": _public_items(items, "changed"),
        "gone_items": _public_items(items, "gone"),
        "items_total": {
            "new": counts["new"],
            "changed": counts["changed"],
            "gone": counts["gone"],
        },
    })

    _write_plan(items)
    _write_csv(items)
    _write_json(paths.CLOUD_AUDIT_STATUS_FILE, status)
    # Действия от прошлой сверки к этой не относятся — начинаем чистый лист.
    _write_json(paths.CLOUD_AUDIT_ACTIONS_FILE, [])
    write_report()
    return status


def _write_plan(items: list[dict]) -> None:
    _write_json(paths.CLOUD_AUDIT_PLAN_FILE, {"items": items})


def _write_csv(items: list[dict]) -> None:
    try:
        paths.CLOUD_AUDIT_CSV_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(paths.CLOUD_AUDIT_CSV_FILE, "w", encoding="utf-8-sig",
                  newline="") as fh:
            writer = csv.writer(fh, delimiter=";")
            writer.writerow([
                "база", "состояние", "путь", "размер в облаке",
                "размер в базе", "примечание",
            ])
            for item in items:
                writer.writerow([
                    SCOPE_NAMES.get(item.get("scope"), item.get("scope")),
                    KIND_NAMES.get(item.get("kind"), item.get("kind")),
                    item.get("key") if item.get("scope") == "disk" else item.get("name"),
                    item.get("size") if item.get("size") is not None else "",
                    item.get("db_size") if item.get("db_size") is not None else "",
                    item.get("detail") or "",
                ])
    except OSError as exc:
        print(f"  [!] Не удалось записать CSV: {exc}", flush=True)


def _fmt_time(value) -> str:
    try:
        return time.strftime("%d.%m.%Y %H:%M", time.localtime(float(value)))
    except (OSError, TypeError, ValueError):
        return "—"


def render_report(data: dict, actions: list[dict] | None = None) -> str:
    """Читаемый отчёт: время, базы, цифры, первые 100 файлов и что нажали."""
    scope = str(data.get("scope") or "both")
    lines = [
        "СВЕРКА С ОБЛАКОМ",
        f"Когда: {_fmt_time(data.get('started_at'))}",
        f"Базы: {SCOPE_NAMES.get(scope, scope)}",
    ]
    per_base = data.get("per_base") or {}
    for base in _scope_list(scope):
        info = per_base.get(base)
        if info:
            lines.append(
                f"  {SCOPE_NAMES.get(base, base)}: в облаке "
                f"{int(info.get('cloud') or 0):,} · в базе {int(info.get('db') or 0):,}"
            )
            # Старые статусы (до 35.7.8) этих полей не знают — не показываем
            # нули, пока сверка не выполнена заново.
            if "downloaded" in info:
                bits = [f"скачано {int(info.get('downloaded') or 0):,}"]
                if info.get("lost"):
                    bits.append(f"отложено {int(info.get('lost') or 0):,}")
                if info.get("waiting"):
                    bits.append(f"ждут Яндекса {int(info.get('waiting') or 0):,}")
                queued = int(info.get("pending") or 0) - int(info.get("waiting") or 0)
                if queued > 0:
                    bits.append(f"в очереди {queued:,}")
                lines.append(f"    {', '.join(bits)}")
            if info.get("cloud_size_unknown"):
                lines.append(
                    f"    размер в облаке не отдаётся: "
                    f"{int(info.get('cloud_size_unknown') or 0):,} файлов — "
                    f"сверены по наличию"
                )
    started = data.get("started_at") or 0
    finished = data.get("finished_at")
    if started and finished:
        lines.append(f"Заняло: {human_duration(float(finished) - float(started))}")
    if data.get("error"):
        lines += ["", f"ОШИБКА: {data['error']}"]

    lines += [
        "",
        "ИТОГ",
        f"  ✅ Совпадает: {int(data.get('same') or 0):,}",
        f"  ➕ Новое в облаке: {int(data.get('new') or 0):,}",
        f"  🔁 Изменилось: {int(data.get('changed') or 0):,}",
        f"  ➖ Пропало из облака: {int(data.get('gone') or 0):,}",
    ]
    unknown_total = sum(
        int((per_base.get(base) or {}).get("cloud_size_unknown") or 0)
        for base in _scope_list(scope)
    )
    if unknown_total:
        lines.append(
            f"  ℹ️ У {unknown_total:,} файлов облако не сообщает размер — "
            f"они сверены по наличию (id и имя), а не по размеру."
        )

    for kind, title in (
        ("new", "НОВОЕ В ОБЛАКЕ"),
        ("changed", "ИЗМЕНИЛОСЬ"),
        ("gone", "ПРОПАЛО ИЗ ОБЛАКА"),
    ):
        items = data.get(f"{kind}_items") or []
        total = int((data.get("items_total") or {}).get(kind) or 0)
        if not items:
            continue
        lines += ["", f"{title} (первые {min(len(items), REPORT_LIMIT)})"]
        for item in items[:REPORT_LIMIT]:
            mark = "Диск" if item.get("scope") == "disk" else "Безлимит"
            shown = item.get("key") if item.get("scope") == "disk" else item.get("name")
            detail = f" — {item.get('detail')}" if item.get("detail") else ""
            lines.append(f"  [{mark}] {shown}{detail}")
        if total > REPORT_LIMIT:
            lines.append(f"  … и ещё {total - REPORT_LIMIT:,} — полный список в CSV.")

    errors = data.get("errors") or []
    if errors:
        lines += ["", "НЕ УДАЛОСЬ ПРОЧИТАТЬ"]
        lines += [f"  {text}" for text in errors[:10]]
        if len(errors) > 10:
            lines.append(f"  … и ещё {len(errors) - 10}")

    lines += ["", "ЧТО НАЖАЛИ ПОСЛЕ СВЕРКИ"]
    actions = actions if actions is not None else read_actions()
    if actions:
        for action in actions:
            lines.append(
                f"  {_fmt_time(action.get('at'))} · "
                f"{action.get('label') or action.get('action')}: "
                f"{action.get('text') or ''}".rstrip()
            )
    else:
        lines.append("  действий не было — ничего не меняли")

    lines += [
        "",
        "Сверка ничего не скачивала и не удаляла. Действия по кнопкам тоже "
        "не удаляют файлы.",
        f"Полный список: {paths.CLOUD_AUDIT_CSV_FILE}",
    ]
    return "\n".join(lines) + "\n"


def write_report() -> None:
    """Пересобирает текстовый отчёт из последней сверки и списка действий."""
    data = read_status()
    if not data:
        return
    try:
        paths.CLOUD_AUDIT_REPORT_FILE.parent.mkdir(parents=True, exist_ok=True)
        paths.CLOUD_AUDIT_REPORT_FILE.write_text(
            render_report(data, read_actions()), encoding="utf-8"
        )
    except OSError as exc:
        print(f"  [!] Не удалось записать отчёт: {exc}", flush=True)


def _record_action(action: str, label: str, text: str, counts: dict) -> dict:
    actions = read_actions()
    entry = {
        "at": time.time(),
        "action": action,
        "label": label,
        "text": text,
        "counts": counts,
    }
    actions.append(entry)
    _write_json(paths.CLOUD_AUDIT_ACTIONS_FILE, actions)
    write_report()
    return entry


# ── действия по итогу сверки ────────────────────────────────────────────────


def _load_last_audit() -> tuple[dict, list[dict]]:
    data = read_status()
    if not data or not data.get("started_at"):
        raise AuditError("Сначала выполните сверку с облаком.")
    if data.get("active"):
        raise AuditError("Сверка ещё идёт — дождитесь окончания.")
    if data.get("error"):
        raise AuditError("Прошлая сверка закончилась ошибкой — запустите её снова.")
    plan = _read_json(paths.CLOUD_AUDIT_PLAN_FILE, {})
    items = plan.get("items") if isinstance(plan, dict) else None
    if not isinstance(items, list):
        raise AuditError("Нет списка файлов от сверки — запустите её снова.")
    return data, items


def old_copy_path(path: str) -> str:
    """Свободное имя для старой копии: `Отчёт_старая.docx`, `_старая_2` и т. д."""
    p = Path(path)
    stem, ext = p.stem, p.suffix
    candidate = p.with_name(f"{stem}{OLD_SUFFIX}{ext}")
    number = 2
    while candidate.exists():
        candidate = p.with_name(f"{stem}{OLD_SUFFIX}_{number}{ext}")
        number += 1
    return str(candidate)


def _requeue_changed(scope: str, keys: list[str]) -> int:
    db_file = paths.DB_FILE if scope == "disk" else paths.UNLIM_DB_FILE
    table = DB_TABLES[scope]
    key_col = DB_KEYS[scope]
    extra = "" if scope == "disk" else ", worker_id = NULL, assigned_at = NULL"
    con = sqlite3.connect(str(db_file), timeout=30)
    try:
        updated = 0
        with con:
            for start in range(0, len(keys), CHUNK):
                chunk = keys[start:start + CHUNK]
                marks = ",".join("?" * len(chunk))
                cur = con.execute(
                    f"UPDATE {table} SET status = 'pending', local_path = NULL, "
                    f"error = NULL, attempts = 0, downloaded_at = NULL, "
                    f"retry_after = NULL, server_fails = 0{extra} "
                    f"WHERE {key_col} IN ({marks})",
                    chunk,
                )
                updated += max(0, cur.rowcount)
        return updated
    finally:
        con.close()


def _insert_new(scope: str, items: list[dict]) -> int:
    db_file = paths.DB_FILE if scope == "disk" else paths.UNLIM_DB_FILE
    table = DB_TABLES[scope]
    key_col = DB_KEYS[scope]
    con = sqlite3.connect(str(db_file), timeout=30)
    try:
        added = 0
        with con:
            for item in items:
                key = item.get("key")
                size = item.get("size")
                md5 = item.get("md5")
                name = item.get("name") or str(key).rsplit("/", 1)[-1]
                if scope == "disk":
                    cur = con.execute(
                        f"INSERT INTO {table} ({key_col}, size, md5, status) "
                        f"VALUES (?, ?, ?, 'pending') "
                        f"ON CONFLICT({key_col}) DO UPDATE SET "
                        f"status = 'pending', local_path = NULL, error = NULL, "
                        f"attempts = 0, downloaded_at = NULL",
                        (key, size, md5),
                    )
                else:
                    cur = con.execute(
                        f"INSERT INTO {table} ({key_col}, name, size, md5, status) "
                        f"VALUES (?, ?, ?, ?, 'pending') "
                        f"ON CONFLICT({key_col}) DO UPDATE SET "
                        f"status = 'pending', local_path = NULL, error = NULL, "
                        f"attempts = 0, downloaded_at = NULL, "
                        f"worker_id = NULL, assigned_at = NULL",
                        (key, name, size, md5),
                    )
                added += max(0, cur.rowcount)
        return added
    finally:
        con.close()


def add_new() -> dict:
    """Ставит в очередь новые и изменившиеся файлы из последней сверки.

    Для изменившихся старая копия на диске переименовывается с припиской
    «_старая» — ничего не удаляется. Если старую копию переименовать не
    удалось (файл занят, диск отключён), такой файл не трогаем и говорим
    об этом человеку.
    """
    _data, items = _load_last_audit()
    by_scope: dict[str, dict[str, list[dict]]] = {
        scope: {"new": [], "changed": []} for scope in SCOPES
    }
    for item in items:
        scope = str(item.get("scope") or "")
        kind = str(item.get("kind") or "")
        if scope in by_scope and kind in by_scope[scope]:
            by_scope[scope][kind].append(item)

    result = {
        "added": 0, "requeued": 0, "renamed": 0,
        "skipped": 0, "errors": [],
    }
    for scope in SCOPES:
        new_items = by_scope[scope]["new"]
        changed_items = by_scope[scope]["changed"]
        if not new_items and not changed_items:
            continue
        db_file = paths.DB_FILE if scope == "disk" else paths.UNLIM_DB_FILE
        if not db_file.exists():
            result["errors"].append(
                f"{SCOPE_NAMES[scope]}: файл базы не найден — сверка не применена"
            )
            continue

        requeue_keys: list[str] = []
        for item in changed_items:
            local = item.get("local")
            if local:
                if not volume_ready(local):
                    result["skipped"] += 1
                    result["errors"].append(
                        f"{item.get('name')}: диск не подключён — старая копия не тронута"
                    )
                    continue
                if os.path.exists(local):
                    target = old_copy_path(local)
                    try:
                        os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
                        os.rename(local, target)
                        result["renamed"] += 1
                    except OSError as exc:
                        result["skipped"] += 1
                        result["errors"].append(
                            f"{item.get('name')}: не удалось переименовать "
                            f"старую копию ({exc}) — файл оставлен как есть"
                        )
                        continue
            requeue_keys.append(str(item.get("key")))

        try:
            added = _insert_new(scope, new_items) if new_items else 0
            requeued = _requeue_changed(scope, requeue_keys) if requeue_keys else 0
        except sqlite3.Error as exc:
            raise AuditError(f"Ошибка базы {SCOPE_NAMES[scope]}: {exc}") from exc
        result["added"] += added
        result["requeued"] += requeued

    text = (
        f"новых добавлено {result['added']}, "
        f"изменившихся поставлено на повтор {result['requeued']}, "
        f"старых копий переименовано {result['renamed']}"
    )
    if result["skipped"]:
        text += f", пропущено {result['skipped']}"
    _record_action("add_new", "Добавить новые в очередь", text, result)
    return result


def mark_gone() -> dict:
    """Помечает файлы, исчезнувшие из облака. На диске они остаются."""
    _data, items = _load_last_audit()
    gone = [item for item in items if item.get("kind") == "gone"]
    result = {"marked": 0, "errors": []}
    for scope in SCOPES:
        keys = [str(it.get("key")) for it in gone
                if it.get("scope") == scope and it.get("key")]
        if not keys:
            continue
        db_file = paths.DB_FILE if scope == "disk" else paths.UNLIM_DB_FILE
        if not db_file.exists():
            result["errors"].append(f"{SCOPE_NAMES[scope]}: файл базы не найден")
            continue
        table = DB_TABLES[scope]
        key_col = DB_KEYS[scope]
        con = sqlite3.connect(str(db_file), timeout=30)
        try:
            with con:
                for start in range(0, len(keys), CHUNK):
                    chunk = keys[start:start + CHUNK]
                    marks = ",".join("?" * len(chunk))
                    cur = con.execute(
                        f"UPDATE {table} SET status = 'deleted', "
                        f"error = 'удалён из облака' "
                        f"WHERE {key_col} IN ({marks}) AND status <> 'deleted'",
                        chunk,
                    )
                    result["marked"] += max(0, cur.rowcount)
        except sqlite3.Error as exc:
            raise AuditError(f"Ошибка базы {SCOPE_NAMES[scope]}: {exc}") from exc
        finally:
            con.close()

    text = f"помечено «удалён из облака» {result['marked']}, файлы на диске остались"
    _record_action("mark_gone", "Пометить пропавшие", text, result)
    return result
