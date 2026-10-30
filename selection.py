"""Что качать: выбранные пользователем папки и файлы.

Зачем
-----
Раньше «качать» значило «всё, что лежит в базе в очереди». Сергей просил
открыть облако и отметить нужное мышкой — папки целиком или отдельные файлы.

Главное правило
---------------
Выбор **не меняет статусы в базе**. Невыбранное остаётся в очереди ровно в том
виде, в каком было, а уже скачанное (status='downloaded') в работу не попадает:
оно не pending, и весь набор заданий строится только из pending/failed/lost.

Поэтому запуск «Скачать выбранное» — это передача точного списка работ
(`download_all(jobs=...)`), а не массовая пометка чего-либо в базе.

Файл выбора лежит в каталоге данных (`paths.DATA_DIR`), то есть у каждого
профиля свой.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Iterable, Optional

import paths

VERSION = 1
# Больше 50 тысяч путей в файле выбора — это уже не выбор, а копия базы.
MAX_ENTRIES = 50000


def selection_file() -> Path:
    return paths.DATA_DIR / "selection.json"


def _empty() -> dict:
    return {"version": VERSION, "disk": {"folders": [], "files": []}}


def normalize_remote(path: str) -> str:
    """'Фото/2024' и '/Фото/2024' → 'disk:/Фото/2024'."""
    text = str(path or "").strip().replace("\\", "/")
    if not text:
        return ""
    if text.startswith("disk:"):
        text = text[len("disk:"):]
    if not text.startswith("/"):
        text = "/" + text
    parts = [p for p in text.split("/") if p]
    if any(p in (".", "..") for p in parts):
        raise ValueError("Недопустимый путь")
    return "disk:/" + "/".join(parts) if parts else "disk:/"


def _norm_size(value) -> Optional[int]:
    try:
        size = int(value)
    except (TypeError, ValueError):
        return None
    return size if size >= 0 else None


def _clean_files(items: Iterable) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    for item in items or []:
        if isinstance(item, dict):
            raw = item.get("path") or ""
            size = _norm_size(item.get("size"))
        else:
            raw = item
            size = None
        path = normalize_remote(raw)
        if not path or path == "disk:/":
            continue
        low = path.casefold()
        if low in seen:
            continue
        seen.add(low)
        out.append({"path": path, "size": size})
        if len(out) >= MAX_ENTRIES:
            break
    return out


def _clean_folders(items: Iterable) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in items or []:
        path = normalize_remote(item)
        if not path or path == "disk:/":
            continue
        low = path.casefold()
        if low in seen:
            continue
        seen.add(low)
        out.append(path)
        if len(out) >= MAX_ENTRIES:
            break
    return out


def load() -> dict:
    """Читает файл выбора. Битого или отсутствующего файла достаточно: пустой выбор."""
    try:
        data = json.loads(selection_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _empty()
    if not isinstance(data, dict):
        return _empty()
    disk = data.get("disk") if isinstance(data.get("disk"), dict) else {}
    try:
        folders = _clean_folders(disk.get("folders"))
        files = _clean_files(disk.get("files"))
    except ValueError:
        folders, files = [], []
    return {"version": VERSION, "disk": {"folders": folders, "files": files}}


def save(data: dict) -> None:
    """Пишет выбор атомарно: оборванная запись не оставит битый JSON."""
    path = selection_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def save_disk(folders: Iterable, files: Iterable) -> dict:
    data = {
        "version": VERSION,
        "disk": {
            "folders": _clean_folders(folders),
            "files": _clean_files(files),
        },
    }
    save(data)
    return data


def clear_disk() -> dict:
    data = _empty()
    save(data)
    return data


def disk_selection() -> dict:
    return load()["disk"]


def is_empty(selection: Optional[dict] = None) -> bool:
    disk = selection if selection is not None else disk_selection()
    return not disk.get("folders") and not disk.get("files")


def summary(selection: Optional[dict] = None) -> dict:
    """Числа для окна: сколько папок и файлов отмечено и сколько это байт."""
    disk = selection if selection is not None else disk_selection()
    files = disk.get("files") or []
    sizes = [f.get("size") for f in files if f.get("size")]
    return {
        "folders": len(disk.get("folders") or []),
        "files": len(files),
        "bytes": sum(sizes),
        "known_bytes": len(sizes),
    }


def build_disk_jobs(
    state,
    client=None,
    do_scan: bool = True,
    log=None,
) -> dict:
    """Готовит работы по сохранённому выбору, никого не помечая в базе лишним.

    Шаги:
      1. Выбранные папки обходятся (`scan_tree`) — так в базе появляются файлы,
         которых там ещё не было. Статусы скачанного обход не трогает.
      2. Из базы берутся только pending/failed/lost под выбранными папками.
      3. Явно выбранные файлы добавляются, если они не скачаны и не пропущены.
      4. Возвращаются задания для `download_all(jobs=...)`.
    """
    # Ленивый импорт: selection нужен и окну, и сборщику отчётов, а downloader
    # тянет за собой сетевые зависимости.
    from downloader import DownloadJob, scan_tree

    disk = disk_selection()
    folders = list(disk.get("folders") or [])
    files = list(disk.get("files") or [])
    result = {
        "jobs": [],
        "folders": len(folders),
        "files": len(files),
        "scanned_folders": 0,
        "already_done": 0,
    }
    if not folders and not files:
        return result

    if do_scan and client is not None:
        for folder in folders:
            try:
                scan_tree(client, state, root=folder)
                result["scanned_folders"] += 1
            except Exception as exc:  # noqa: BLE001 — одна пропавшая папка не срывает запуск
                if log is not None:
                    log.warning("Не удалось обойти %s: %s", folder, exc)

    wanted: dict[str, tuple[Optional[int], Optional[str]]] = {}
    if folders:
        for row in state.get_files_under(folders):
            wanted[row["remote_path"]] = (row["size"], row["md5"])

    explicit = [item["path"] for item in files]
    statuses = state.get_statuses(explicit) if explicit else {}
    for item in files:
        path = item["path"]
        if statuses.get(path) in ("downloaded", "skipped"):
            # Уже есть на диске: повторно не качаем, даже если отметили.
            result["already_done"] += 1
            continue
        wanted.setdefault(path, (item.get("size"), None))

    for path, (size, md5) in wanted.items():
        state.mark_pending(path, size, md5)

    result["jobs"] = [
        DownloadJob(remote_path=path, size=size, md5=md5)
        for path, (size, md5) in sorted(wanted.items())
    ]
    return result
