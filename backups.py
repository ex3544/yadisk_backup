"""Резервные копии баз со статусами — без самих файлов.

Копия содержит только SQLite-базу: что скачано, что с ошибкой, какие пути
записаны. Файлы на дисках не читаются, поэтому отключённые диски, другие буквы
и перенос на новую машину не мешают.

Форматы:
  * одиночная копия — `backups/disk-20261027-153000-manual.db`;
  * общая копия — `backups/all-20261027-153000-manual.zip` с двумя базами и
    `manifest.json` (дата, версия программы, счётчики, размеры).

Все снимки делаются через sqlite3 backup API: это консистентно даже при
работающей выгрузке и корректно при WAL. Восстановление заменяет содержимое
живой базы, поэтому вызывать его можно только при остановленных задачах —
эту проверку делает webui.
"""

from __future__ import annotations

import json
import re
import sqlite3
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

import db_migrations
import state
import unlim_schema
from utils import sqlite_ro_uri

MANIFEST_NAME = "manifest.json"
FORMAT_VERSION = 1
SCOPES = ("disk", "unlim")
SCOPE_TITLES = {"disk": "Диск", "unlim": "Безлимит", "all": "Общая"}
TABLES = {"disk": "files", "unlim": "unlim_files"}
AUTO_KEEP = 5  # сколько автоматических копий хранить на каждую базу

NAME_RE = re.compile(r"^(disk|unlim|all)-(\d{8}-\d{6})-([a-z\-]+)\.(db|zip)$")
LEGACY_PREFIXES = {"disk": "state.db.bak_", "unlim": "unlim_state.db.bak_"}

# Максимальная версия схемы, которую понимает текущая программа.
TARGET_VERSIONS = {
    "disk": max(v for v, _ in state.MIGRATIONS),
    "unlim": max(v for v, _ in unlim_schema.MIGRATIONS),
}


class BackupError(Exception):
    """Ошибка, которую можно показать пользователю как есть."""


# ── снимки ───────────────────────────────────────────────────────────────────


def _snapshot(src: Path, dst: Path) -> None:
    """Консистентная копия src → dst через sqlite backup API.

    Копию переводим в journal_mode=DELETE: иначе SQLite создаёт рядом
    `-wal`/`-shm` при каждом чтении, и одиночный файл копии перестаёт быть
    самодостаточным.
    """
    src_con = sqlite3.connect(str(src), timeout=30)
    try:
        dst_con = sqlite3.connect(str(dst))
        try:
            src_con.backup(dst_con)
            dst_con.commit()
            try:
                dst_con.execute("PRAGMA journal_mode=DELETE")
            except sqlite3.Error:
                pass
        finally:
            dst_con.close()
    finally:
        src_con.close()


def _immutable_uri(path: Path) -> str:
    """URI для чтения готовой копии без создания -wal/-shm рядом с ней."""
    return sqlite_ro_uri(path) + "&immutable=1"


def _counts(db_path: Path, key: str, *, immutable: bool = False) -> dict:
    out = {"total": 0, "pending": 0, "downloaded": 0, "skipped": 0,
           "failed": 0, "in_progress": 0}
    uri = _immutable_uri(db_path) if immutable else sqlite_ro_uri(db_path)
    try:
        con = sqlite3.connect(uri, uri=True, timeout=10)
    except sqlite3.Error:
        return out
    try:
        table = TABLES[key]
        for status, n in con.execute(
            f"SELECT status, COUNT(*) FROM {table} GROUP BY status"
        ):
            out[status or "unknown"] = n
            out["total"] += n
    except sqlite3.Error:
        return out
    finally:
        con.close()
    return out


def _describe(db_path: Path, key: str) -> dict:
    return {
        "file": f"{key}.db",
        "counts": _counts(db_path, key, immutable=True),
        "size_bytes": db_path.stat().st_size,
    }


def _unique(path: Path) -> Path:
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    n = 2
    while True:
        candidate = path.with_name(f"{stem}-{n}{suffix}")
        if not candidate.exists():
            return candidate
        n += 1


def backups_dir(data_dir: Path) -> Path:
    d = Path(data_dir) / "backups"
    d.mkdir(parents=True, exist_ok=True)
    return d


def create_backup(
    data_dir: Path,
    scope: str,
    sources: dict[str, Path],
    app_version: str,
    reason: str = "manual",
) -> dict:
    """Создаёт копию `disk`, `unlim` или `all`. Возвращает описание для UI."""
    if scope not in ("disk", "unlim", "all"):
        raise BackupError("Неизвестный тип копии")
    if not re.fullmatch(r"[a-z\-]+", reason):
        raise BackupError("Некорректная причина копии")

    wanted = SCOPES if scope == "all" else (scope,)
    present = {
        key: Path(src)
        for key, src in sources.items()
        if key in wanted and Path(src).exists() and Path(src).stat().st_size > 0
    }
    if not present:
        raise BackupError("Нет базы для копирования — сначала сформируйте её")

    folder = backups_dir(data_dir)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    suffix = "zip" if scope == "all" else "db"
    path = _unique(folder / f"{scope}-{stamp}-{reason}.{suffix}")

    meta = {
        "format": FORMAT_VERSION,
        "scope": scope,
        "reason": reason,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "app_version": app_version,
        "databases": {},
    }

    if scope == "all":
        with tempfile.TemporaryDirectory() as td:
            tmp_zip = Path(td) / path.name
            with zipfile.ZipFile(tmp_zip, "w", zipfile.ZIP_DEFLATED) as zf:
                for key, src in present.items():
                    tmp_db = Path(td) / f"{key}.db"
                    _snapshot(src, tmp_db)
                    meta["databases"][key] = _describe(tmp_db, key)
                    zf.write(tmp_db, f"{key}.db")
                zf.writestr(
                    MANIFEST_NAME,
                    json.dumps(meta, ensure_ascii=False, indent=2),
                )
            tmp_zip.replace(path)
    else:
        with tempfile.TemporaryDirectory() as td:
            tmp_db = Path(td) / path.name
            _snapshot(present[scope], tmp_db)
            meta["databases"][scope] = _describe(tmp_db, scope)
            tmp_db.replace(path)

    _prune_auto(data_dir)
    return _item_from_meta(path, meta)


# ── список ──────────────────────────────────────────────────────────────────


def _item_from_meta(path: Path, meta: dict) -> dict:
    dbs = meta.get("databases") or {}
    counts = {"total": 0, "pending": 0, "downloaded": 0, "skipped": 0,
              "failed": 0, "in_progress": 0}
    for info in dbs.values():
        for name, value in (info.get("counts") or {}).items():
            counts[name] = counts.get(name, 0) + value
    return {
        "id": path.name,
        "scope": meta.get("scope") or "all",
        "scope_title": SCOPE_TITLES.get(meta.get("scope") or "all", "Копия"),
        "reason": meta.get("reason") or "manual",
        "created_at": meta.get("created_at") or "",
        "app_version": meta.get("app_version"),
        "size_bytes": path.stat().st_size,
        "databases": sorted(dbs),
        "counts": counts,
        "legacy": False,
    }


def _read_manifest(path: Path) -> dict | None:
    try:
        with zipfile.ZipFile(path) as zf:
            data = zf.read(MANIFEST_NAME)
        meta = json.loads(data.decode("utf-8"))
        if not isinstance(meta, dict):
            return None
        return meta
    except (KeyError, OSError, ValueError, zipfile.BadZipFile):
        return None


def _legacy_item(path: Path, key: str) -> dict:
    tail = path.name[len(LEGACY_PREFIXES[key]):]
    created = ""
    m = re.search(r"(\d{8})_(\d{6})", tail)
    if m:
        try:
            created = datetime.strptime(
                f"{m.group(1)}-{m.group(2)}", "%Y%m%d-%H%M%S"
            ).isoformat(timespec="seconds")
        except ValueError:
            created = ""
    if not created:
        created = datetime.fromtimestamp(path.stat().st_mtime).isoformat(
            timespec="seconds"
        )
    meta = {
        "scope": key,
        "reason": "migration",
        "created_at": created,
        "databases": {key: _describe(path, key)},
    }
    item = _item_from_meta(path, meta)
    item["legacy"] = True
    item["scope_title"] = SCOPE_TITLES[key]
    return item


def list_backups(data_dir: Path) -> list[dict]:
    data_dir = Path(data_dir)
    items: list[dict] = []

    folder = data_dir / "backups"
    if folder.exists():
        for path in folder.iterdir():
            m = NAME_RE.match(path.name)
            if not m or not path.is_file():
                continue
            scope, stamp, _reason, ext = m.groups()
            try:
                created = datetime.strptime(stamp, "%Y%m%d-%H%M%S").isoformat(
                    timespec="seconds"
                )
            except ValueError:
                created = datetime.fromtimestamp(
                    path.stat().st_mtime
                ).isoformat(timespec="seconds")
            if ext == "zip":
                meta = _read_manifest(path)
                if meta is None:
                    continue
                meta.setdefault("scope", scope)
                meta.setdefault("created_at", created)
            else:
                meta = {
                    "scope": scope,
                    "reason": _reason,
                    "created_at": created,
                    "databases": {scope: _describe(path, scope)},
                }
            items.append(_item_from_meta(path, meta))

    for key, prefix in LEGACY_PREFIXES.items():
        for path in data_dir.glob(prefix + "*"):
            if not path.is_file():
                continue
            # SQLite может оставить рядом служебные файлы — это не копии.
            if path.name.endswith(("-wal", "-shm", "-journal")):
                continue
            if path.stat().st_size == 0:
                continue
            items.append(_legacy_item(path, key))

    items.sort(key=lambda x: x["created_at"], reverse=True)
    return items


# ── восстановление ──────────────────────────────────────────────────────────


def _find_backup(data_dir: Path, backup_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9._-]+", backup_id or ""):
        raise BackupError("Некорректное имя копии")
    if backup_id.endswith(("-wal", "-shm", "-journal")):
        raise BackupError("Это служебный файл SQLite, а не копия базы")
    data_dir = Path(data_dir).resolve()

    folder = (data_dir / "backups").resolve()
    candidate = (folder / backup_id).resolve()
    if candidate.parent == folder and candidate.is_file() and NAME_RE.match(candidate.name):
        return candidate

    for prefix in LEGACY_PREFIXES.values():
        if backup_id.startswith(prefix):
            legacy = (data_dir / backup_id).resolve()
            if legacy.parent == data_dir and legacy.is_file():
                return legacy
    raise BackupError("Копия не найдена")


def _check_source(path: Path, key: str) -> None:
    try:
        con = sqlite3.connect(_immutable_uri(path), uri=True, timeout=10)
    except sqlite3.Error as e:
        raise BackupError(f"Не удалось открыть копию: {e}") from e
    try:
        row = con.execute("PRAGMA integrity_check").fetchone()
        if not row or row[0] != "ok":
            raise BackupError("Копия повреждена — восстановление отменено")
        version = int(con.execute("PRAGMA user_version").fetchone()[0])
        if version > TARGET_VERSIONS[key]:
            raise BackupError(
                "Копия сделана более новой версией программы. "
                "Обновите приложение и повторите."
            )
        table = TABLES[key]
        exists = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if not exists:
            raise BackupError(f"В копии нет таблицы {table} — это не база программы")
    except sqlite3.Error as e:
        raise BackupError(f"Не удалось проверить копию: {e}") from e
    finally:
        con.close()


def _copy_into(src: Path, target: Path) -> None:
    src_con = sqlite3.connect(_immutable_uri(src), uri=True, timeout=30)
    try:
        dst_con = sqlite3.connect(str(target), timeout=30)
        try:
            src_con.backup(dst_con)
            dst_con.commit()
            try:
                dst_con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error:
                pass
        finally:
            dst_con.close()
    finally:
        src_con.close()


def _migrate_after_restore(con: sqlite3.Connection, key: str) -> None:
    if key == "disk":
        db_migrations.migrate(con, "state.db", state.MIGRATIONS, make_backup=False)
    else:
        db_migrations.migrate(
            con, "unlim_state.db", unlim_schema.MIGRATIONS, make_backup=False
        )


def _open_meta(data_dir: Path, path: Path, scopes: list[str]) -> dict:
    """Возвращает manifest и, если нужно, распакованную временную папку."""
    m = NAME_RE.match(path.name)
    if not m or m.group(4) != "zip":
        key = m.group(1) if m else None
        if key is None:
            for k, prefix in LEGACY_PREFIXES.items():
                if path.name.startswith(prefix):
                    key = k
                    break
        if key is None:
            raise BackupError("Не удалось определить тип копии")
        if key not in scopes:
            raise BackupError("В этой копии нет выбранной базы")
        return {"meta": {"databases": {key: {}}, "scope": key},
                "files": {key: path}, "tmp": None}

    meta = _read_manifest(path)
    if meta is None:
        raise BackupError("В архиве нет описания копии — восстановление отменено")
    dbs = meta.get("databases") or {}
    chosen = {k: v for k, v in dbs.items() if k in scopes}
    if not chosen:
        raise BackupError("В копии нет выбранных баз")
    tmp = tempfile.TemporaryDirectory()
    files: dict[str, Path] = {}
    try:
        with zipfile.ZipFile(path) as zf:
            for key in chosen:
                member = f"{key}.db"
                target = Path(tmp.name) / member
                with zf.open(member) as src, open(target, "wb") as dst:
                    dst.write(src.read())
                files[key] = target
    except (KeyError, OSError, zipfile.BadZipFile) as e:
        tmp.cleanup()
        raise BackupError(f"Не удалось прочитать архив: {e}") from e
    return {"meta": meta, "files": files, "tmp": tmp}


def restore_backup(
    data_dir: Path,
    backup_id: str,
    scopes: list[str],
    targets: dict[str, Path],
    app_version: str,
) -> dict:
    """Заменяет выбранные базы содержимым копии. Сначала делает их бэкап."""
    chosen = [s for s in SCOPES if s in (scopes or SCOPES)]
    if not chosen:
        raise BackupError("Не выбрано ни одной базы для восстановления")

    path = _find_backup(data_dir, backup_id)
    opened = _open_meta(data_dir, path, chosen)
    try:
        files = opened["files"]

        # 1. Проверяем всё до единой записи, чтобы не восстановить половину.
        for key, src in files.items():
            _check_source(src, key)

        # 2. Сохраняем текущее состояние — восстановление должно быть обратимым.
        pre: list[dict] = []
        for key in files:
            target = Path(targets[key])
            if target.exists() and target.stat().st_size > 0:
                pre.append(
                    create_backup(
                        data_dir,
                        key,
                        {key: target},
                        app_version,
                        reason="before-restore",
                    )
                )

        # 3. Подменяем содержимое и доводим схему до текущей версии.
        for key, src in files.items():
            target = Path(targets[key])
            target.parent.mkdir(parents=True, exist_ok=True)
            _copy_into(src, target)
            con = sqlite3.connect(str(target), timeout=30)
            try:
                _migrate_after_restore(con, key)
            finally:
                con.close()

        return {
            "id": path.name,
            "restored": sorted(files),
            "pre_backups": [p["id"] for p in pre],
            "counts": {
                key: _counts(Path(targets[key]), key) for key in files
            },
        }
    finally:
        if opened.get("tmp") is not None:
            opened["tmp"].cleanup()


# ── удаление и ротация ──────────────────────────────────────────────────────


def delete_backup(data_dir: Path, backup_id: str) -> None:
    path = _find_backup(data_dir, backup_id)
    try:
        path.unlink()
    except OSError as e:
        raise BackupError(f"Не удалось удалить копию: {e}") from e


def _prune_auto(data_dir: Path, keep: int = AUTO_KEEP) -> None:
    folder = Path(data_dir) / "backups"
    if not folder.exists():
        return
    groups: dict[str, list[Path]] = {}
    for path in folder.iterdir():
        m = NAME_RE.match(path.name)
        if not m or m.group(3) != "before-restore":
            continue
        groups.setdefault(m.group(1), []).append(path)
    for paths in groups.values():
        paths.sort(key=lambda p: p.name, reverse=True)
        for old in paths[keep:]:
            try:
                old.unlink()
            except OSError:
                pass
