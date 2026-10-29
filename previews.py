"""Превью отложенных фото.

Если Яндекс не отдаёт оригинал (сервер хранения молчит), в папке выгрузки
появляется уменьшенная копия снимка в `_превью/`. Человек видит, что именно
ещё не скачалось, а не пустые места. Когда оригинал докачается, превью
удаляется.

Правила:
* только фото — видео и документы превью не получают;
* только отложенные файлы — для нормально скачанных превью не создаётся;
* запись атомарная: `.part` → `os.replace`, как у обычного скачивания;
* путь превью хранится в БД (`preview_path`), чтобы его можно было убрать.
"""

from __future__ import annotations

import os
import time
from typing import Optional

import requests

from fetch import DeadHosts, fetch_to_file
from utils import ensure_dir, sanitize_remote_path, to_long_path

# Папка превью внутри папки выгрузки. Подчёркивание впереди — чтобы сортировка
# и «разложить по датам» не путали её с настоящими папками.
PREVIEW_DIR_NAME = "_превью"

PHOTO_EXTS = frozenset(
    {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".gif",
     ".bmp", ".tiff", ".tif", ".raw", ".arw", ".cr2", ".nef", ".dng", ".orf"}
)

# Размеры превью Яндекса от большего к меньшему. XXXL — самое крупное (до 1280 px).
PREVIEW_SIZES = ("XXXL", "XXL", "XL")


def is_photo(path: str) -> bool:
    return os.path.splitext(path or "")[1].lower() in PHOTO_EXTS


def preview_dir(download_dir: str) -> str:
    return os.path.join(download_dir, PREVIEW_DIR_NAME)


def preview_path_for(download_dir: str, remote_path: str) -> str:
    """`disk:/Фото/2019/a.jpg` → `<папка>/_превью/Фото/2019/a.jpg`."""
    rel = sanitize_remote_path(remote_path)
    stem, _ext = os.path.splitext(rel)
    return os.path.join(download_dir, PREVIEW_DIR_NAME, stem.replace("/", os.sep) + ".jpg")


def save_from_link(
    link: str,
    dst: str,
    *,
    dead_hosts: Optional[DeadHosts] = None,
    session: Optional[requests.Session] = None,
) -> str:
    """Скачивает картинку в `dst` атомарно. Бросает FetchFailure как обычный fetch."""
    ensure_dir(to_long_path(os.path.dirname(dst) or "."))
    fetch_to_file(
        link,
        dst,
        dead_hosts=dead_hosts,
        reject_pages=True,  # вместо картинки пришла HTML-заглушка — это ошибка
        session=session,
        link_timeout=(15.0, 30.0),
        storage_timeout=(10.0, 10.0),
    )
    return dst


def remove_preview(path: Optional[str]) -> bool:
    if not path:
        return False
    try:
        p = to_long_path(path)
        if os.path.exists(p):
            os.remove(p)
            return True
    except OSError:
        pass
    return False


def make_disk_preview(
    client,
    remote_path: str,
    download_dir: str,
    *,
    dead_hosts: Optional[DeadHosts] = None,
    session: Optional[requests.Session] = None,
) -> Optional[str]:
    """Превью файла обычного Диска через поле `preview` метаданных.

    Никогда не бросает исключений: превью — украшение, а не часть бэкапа.
    """
    if not is_photo(remote_path):
        return None
    link = None
    for size in PREVIEW_SIZES:
        try:
            meta = client.get_meta(remote_path, preview_size=size)
        except Exception:  # noqa: BLE001 — превью необязательно
            continue
        link = getattr(meta, "preview", None)
        if link:
            break
    if not link:
        return None
    dst = preview_path_for(download_dir, remote_path)
    try:
        return save_from_link(link, dst, dead_hosts=dead_hosts, session=session)
    except Exception:  # noqa: BLE001
        return None


def unlim_preview_url(
    session: requests.Session,
    sk: str,
    uid: str,
    file_id: str,
    base_url: str,
    timeout: tuple[float, float] = (10.0, 20.0),
) -> Optional[str]:
    """Свежая ссылка на самое крупное превью файла Безлимита (mpfs/info).

    Формат повторяет веб-клиент Яндекса: `requestParams: {path, meta: "sizes"}`,
    в ответе `meta.sizes` — список `{name, url}`. Ссылки подписаны и живут
    недолго, поэтому запрашиваются прямо перед скачиванием.
    """
    body = {
        "sk": sk,
        "connection_id": f"{uid}{int(time.time() * 1000)}",
        "apiMethod": "mpfs/info",
        "requestParams": {"path": file_id, "meta": "sizes"},
    }
    try:
        r = session.post(
            f"{base_url}/models-v2",
            params={"m": "mpfs/info"},
            json=body,
            timeout=timeout,
        )
        if r.status_code != 200:
            return None
        data = r.json()
    except (requests.RequestException, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    sizes = ((data.get("meta") or {}).get("sizes")
             if isinstance(data.get("meta"), dict) else None)
    by_name: dict[str, str] = {}
    for item in sizes or []:
        if isinstance(item, dict) and item.get("url"):
            by_name[str(item.get("name"))] = str(item["url"])
    for size in PREVIEW_SIZES:
        url = by_name.get(size)
        if url:
            return "https:" + url if url.startswith("//") else url
    return None


def sweep(state, download_dir: str) -> int:
    """Убирает превью, чей оригинал уже скачан, и брошенные `.part`.

    Вызывается перед проходом: превью должно жить, только пока файл ждёт.
    """
    removed = 0
    try:
        rows = list(state.get_preview_paths())
    except Exception:  # noqa: BLE001 — старая база без колонки
        rows = []
    for row in rows:
        status = row["status"]
        path = row["preview_path"]
        if status in ("downloaded", "skipped"):
            if remove_preview(path):
                removed += 1
            try:
                state.set_preview_path(row["remote_path"], None)
            except Exception:  # noqa: BLE001
                pass
        elif path and not os.path.exists(to_long_path(path)):
            # Файл превью пропал (диск отключили) — забываем путь, чтобы не
            # считать его существующим.
            try:
                state.set_preview_path(row["remote_path"], None)
            except Exception:  # noqa: BLE001
                pass

    # .part от прерванных превью старше суток
    root = preview_dir(download_dir)
    try:
        cutoff = time.time() - 24 * 3600
        for dirpath, _dirnames, filenames in os.walk(root):
            for name in filenames:
                if not name.endswith(".part") and not name.startswith(".yadisk-"):
                    continue
                full = os.path.join(dirpath, name)
                try:
                    if os.path.getmtime(full) < cutoff:
                        os.remove(full)
                        removed += 1
                except OSError:
                    pass
    except OSError:
        pass
    return removed
