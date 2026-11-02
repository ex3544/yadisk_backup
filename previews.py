"""Превью отложенных фото.

Если Яндекс не отдаёт оригинал (сервер хранения молчит), появляется
уменьшенная копия снимка — по умолчанию в `<данные>/previews/` (на Windows это
`%LOCALAPPDATA%\\YandexDiskBackup\\previews`, диск C). Внутри — две плоские
папки: `disk/` для обычного Диска и `unlim/` для Безлимита. Раньше превью
лежали в `_превью/` внутри папки выгрузки и терялись вместе со сменой диска;
теперь корень один на программу и задаётся настройкой `PREVIEWS_DIR`. База
хранит путь превью, поэтому смена диска выгрузки их не трогает. Когда оригинал
докачается, превью удаляется сразу.

Ссылки превью Яндекс отдаёт только авторизованному клиенту: без токена или
кук приходит 403 и HTML-заглушка. Поэтому Диск качает превью с заголовком
`Authorization: OAuth <токен>`, а Безлимит — в уже авторизованной сессии с
куками.

Правила:
* только фото — видео и документы превью не получают;
* только отложенные файлы — для нормально скачанных превью не создаётся;
* превью не меняет статус файла и не попадает в счётчики «скачано»;
* запись атомарная: `.part` → `os.replace`, как у обычного скачивания;
* путь превью хранится в БД (`preview_path`), чтобы его можно было убрать.
"""

from __future__ import annotations

import hashlib
import os
import time
from typing import Optional

import requests

import paths
from fetch import DeadHosts, fetch_to_file
from utils import ensure_dir, sanitize_remote_path, to_long_path

# Имя папки по умолчанию внутри каталога данных программы (диск C).
PREVIEW_DIR_NAME = "previews"
# Где превью лежали до 35.6.6 — внутри папки выгрузки. Нужно только для
# совместимости со старыми записями в базах.
LEGACY_PREVIEW_DIR_NAME = "_превью"

PHOTO_EXTS = frozenset(
    {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".gif",
     ".bmp", ".tiff", ".tif", ".raw", ".arw", ".cr2", ".nef", ".dng", ".orf"}
)

# Размеры превью Яндекса от большего к меньшему. XXXL — самое крупное (до 1280 px).
PREVIEW_SIZES = ("XXXL", "XXL", "XL")

# Плоские подпапки внутри корня превью: Диск и Безлимит не смешиваются.
PREVIEW_KINDS = ("disk", "unlim")


def is_photo(path: str) -> bool:
    return os.path.splitext(path or "")[1].lower() in PHOTO_EXTS


def preview_root() -> str:
    """Куда складывать превью.

    Настройка `PREVIEWS_DIR` важнее всего; по умолчанию — `<данные>/previews`,
    то есть рядом с базами на диске C. Папка одна на Диск и Безлимит: пути
    превью хранятся в базах, поэтому смена диска выгрузки их не теряет.
    """
    raw = (os.getenv("PREVIEWS_DIR") or "").strip()
    if raw:
        return os.path.abspath(os.path.expanduser(raw))
    return str(paths.DATA_DIR / PREVIEW_DIR_NAME)


def preview_dir() -> str:
    return preview_root()


def preview_kind_dir(kind: str) -> str:
    """Папка одного хранилища: `<корень>/disk` или `<корень>/unlim`."""
    if kind not in PREVIEW_KINDS:
        raise ValueError(f"неизвестный вид превью: {kind}")
    return os.path.join(preview_root(), kind)


def _flat_preview_name(source: str) -> str:
    """Плоское имя файла превью без папок облака.

    Одинаковые имена (`IMG_0001.JPG` в разных папках) не должны затирать друг
    друга, поэтому к имени добавляется короткий хеш исходного пути.
    `Фото/2019/a.jpg` → `a__9f3c1a2b.jpg`.
    """
    rel = sanitize_remote_path(source)
    base = os.path.basename(rel) or "preview"
    stem, _ext = os.path.splitext(base)
    stem = stem[:80] or "preview"
    digest = hashlib.sha1(source.encode("utf-8")).hexdigest()[:8]
    return f"{stem}__{digest}.jpg"


def preview_path_for(kind: str, source: str) -> str:
    """Путь превью в плоской папке: `disk`/`unlim`, без структуры облака.

    `source` — удалённый путь Диска (`disk:/Фото/a.jpg`) или идентификатор
    файла Безлимита (`/photounlim/…/a.jpg`). Хеш в имени делает путь
    уникальным и стабильным.
    """
    return os.path.join(preview_kind_dir(kind), _flat_preview_name(source))


def save_from_link(
    link: str,
    dst: str,
    *,
    dead_hosts: Optional[DeadHosts] = None,
    session: Optional[requests.Session] = None,
    headers: Optional[dict[str, str]] = None,
) -> str:
    """Скачивает картинку в `dst` атомарно. Бросает FetchFailure как обычный fetch.

    `headers` уходят только в первый запрос — саму ссылку превью. Перенаправление
    на сервер хранения уже подписано и в токене не нуждается.
    """
    ensure_dir(to_long_path(os.path.dirname(dst) or "."))
    fetch_to_file(
        link,
        dst,
        dead_hosts=dead_hosts,
        reject_pages=True,  # вместо картинки пришла HTML-заглушка — это ошибка
        session=session,
        headers=headers,
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
    *,
    token: Optional[str] = None,
    dead_hosts: Optional[DeadHosts] = None,
    session: Optional[requests.Session] = None,
) -> Optional[str]:
    """Превью файла обычного Диска через поле `preview` метаданных.

    Ссылка превью отдаётся только с авторизацией: без токена Яндекс отвечает
    403 и HTML-заглушкой. Токен берётся из клиента, если не передан явно.

    Никогда не бросает исключений: превью — украшение, а не часть бэкапа.
    """
    if not is_photo(remote_path):
        return None
    dst = preview_path_for("disk", remote_path)
    # Превью уже лежит с прошлого прохода — не тратим запросы и не качаем заново.
    if os.path.exists(to_long_path(dst)):
        return dst
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
    auth = (token or getattr(client, "token", None) or "").strip()
    headers = {"Authorization": f"OAuth {auth}"} if auth else None
    try:
        return save_from_link(
            link, dst, dead_hosts=dead_hosts, session=session, headers=headers
        )
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


def sweep(state) -> int:
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
    root = preview_root()
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
