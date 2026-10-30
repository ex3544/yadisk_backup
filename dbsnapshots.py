"""Снимки баз Диска и Безлимита — «точка отсчёта» перед изменениями.

Зачем
-----
База хранит, что уже скачано и что ещё в очереди. Любая ошибка в выборе
файлов или в пересборке портит именно её, и откатить это вручную нечем.

Как устроено
------------
Снимок делается SQLite backup API — обычное копирование файла живой базы в
режиме WAL даёт битый результат. Файлы лежат в `DATA_DIR/snapshots` рядом с
базами профиля: `<вид>-ГГГГММДД-ЧЧММСС.db`, где вид — `state` или `unlim`.

Храним последние `KEEP` снимков каждого вида, старые удаляем.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Optional

import paths

KEEP = 5

# Имя снимка начинается с вида базы — по нему и восстанавливаем.
KINDS = {
    "state": "disk",
    "unlim": "unlim",
}


def snapshot_dir() -> Path:
    return paths.DATA_DIR / "snapshots"


def _databases() -> dict[str, Path]:
    """Имя снимка → путь к базе. Имя снимка строится из этой пары."""
    return {
        "state": paths.DB_FILE,
        "unlim": paths.UNLIM_DB_FILE,
    }


def _db_for_kind(kind: str) -> Optional[Path]:
    return _databases().get(kind)


def _copy_db(src_path: Path, dst_path: Path) -> None:
    src = sqlite3.connect(str(src_path), timeout=30)
    try:
        dst = sqlite3.connect(str(dst_path))
        try:
            with dst:
                src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def make_snapshot(reason: str = "manual") -> list[dict]:
    """Снимает копии обеих баз. Возвращает то, что реально сохранилось."""
    out_dir = snapshot_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    made: list[dict] = []
    for kind, db_file in _databases().items():
        if not db_file.is_file():
            continue
        target = out_dir / f"{kind}-{stamp}.db"
        n = 1
        while target.exists():
            target = out_dir / f"{kind}-{stamp}-{n}.db"
            n += 1
        try:
            _copy_db(db_file, target)
        except sqlite3.Error:
            continue
        made.append(_describe(target, reason))
    prune()
    return made


def _describe(path: Path, reason: str = "") -> dict:
    try:
        stat = path.stat()
        size, mtime = stat.st_size, stat.st_mtime
    except OSError:
        size, mtime = 0, 0.0
    kind = path.name.split("-", 1)[0]
    return {
        "name": path.name,
        "kind": kind,
        "scope": KINDS.get(kind, ""),
        "size": size,
        "created": path.name[len(kind) + 1:-3] if path.name.startswith(kind + "-") else "",
        "reason": reason,
        "mtime": mtime,
    }


def list_snapshots() -> list[dict]:
    out_dir = snapshot_dir()
    if not out_dir.is_dir():
        return []
    items = []
    for path in out_dir.glob("*.db"):
        if path.name.split("-", 1)[0] in KINDS:
            items.append(_describe(path))
    # Новые сверху. По времени файла, а не по имени: два снимка в одну секунду
    # различаются суффиксом, и имя сравнивалось бы неправильно.
    return sorted(items, key=lambda item: (item["mtime"], item["name"]), reverse=True)


def prune(keep: int = KEEP) -> None:
    by_kind: dict[str, list[Path]] = {}
    out_dir = snapshot_dir()
    if not out_dir.is_dir():
        return
    for path in out_dir.glob("*.db"):
        kind = path.name.split("-", 1)[0]
        if kind not in KINDS:
            continue
        by_kind.setdefault(kind, []).append(path)
    for files in by_kind.values():
        files.sort(key=lambda p: (p.stat().st_mtime, p.name), reverse=True)
        for old in files[keep:]:
            try:
                old.unlink()
            except OSError:
                pass


def _resolve(name: str) -> Path:
    """Проверяет имя снимка и возвращает путь. Папки и переходы запрещены."""
    clean = Path(str(name or "")).name
    if not clean or clean != str(name):
        raise ValueError("Недопустимое имя снимка")
    path = snapshot_dir() / clean
    if not path.is_file():
        raise ValueError("Снимок не найден")
    if clean.split("-", 1)[0] not in KINDS:
        raise ValueError("Неизвестный тип снимка")
    return path


def restore(name: str) -> Path:
    """Возвращает базу к состоянию снимка. Задачи должны быть остановлены."""
    snapshot = _resolve(name)
    kind = snapshot.name.split("-", 1)[0]
    target = _db_for_kind(kind)
    if target is None:
        raise ValueError("Неизвестный тип снимка")
    target.parent.mkdir(parents=True, exist_ok=True)
    _copy_db(snapshot, target)
    return target


def delete(name: str) -> None:
    path = _resolve(name)
    try:
        path.unlink()
    except OSError as exc:
        raise ValueError(f"Не удалось удалить снимок: {exc}") from None
