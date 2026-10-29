"""Рекурсивный обход Яндекс.Диска и многопоточное скачивание.

Архитектура:
* scan() — обходит дерево с корня `/`, регистрирует все файлы в БД как pending.
* download_all() — берёт pending из БД и скачивает в пул потоков. Каждый поток
  имеет свой `yadisk.Client` (синхронный клиент не thread-safe для шаринга).
* Resume — если локальный файл существует и совпадает по размеру → skipped.
* Корзина (`/Trash`) не трогается.
"""

from __future__ import annotations

import errno
import json
import logging
import os
import shutil
import socket
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Callable, Iterator, Optional

import urllib3.util.connection as urllib3_cn

# Принудительно отключаем IPv6 (на Linux/VirtualBox IPv6 часто ведёт в таймаут)
urllib3_cn.allowed_gai_family = lambda: socket.AF_INET

# Ограничение MSS до 1360 помогает против «чёрных дыр» Path MTU на Linux/VPN/PPPoE,
# но на Windows setsockopt(IPPROTO_TCP, TCP_MAXSEG) не поддерживается и падает
# с WSAENOPROTOOPT (10042) на каждом соединении. Поэтому включаем патч только там,
# где эта опция реально работает (проверяем один раз на пробном сокете).
_TCP_MAXSEG_SUPPORTED = False
if hasattr(socket, "TCP_MAXSEG"):
    _probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        _probe.setsockopt(socket.IPPROTO_TCP, socket.TCP_MAXSEG, 1360)
        _TCP_MAXSEG_SUPPORTED = True
    except OSError:
        _TCP_MAXSEG_SUPPORTED = False
    finally:
        _probe.close()

if _TCP_MAXSEG_SUPPORTED:
    _orig_create_conn = urllib3_cn.create_connection

    def _patched_create_conn(address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, source_address=None, socket_options=None):
        socket_options = list(socket_options) if socket_options else []
        socket_options.append((socket.IPPROTO_TCP, socket.TCP_MAXSEG, 1360))
        return _orig_create_conn(address, timeout=timeout, source_address=source_address, socket_options=socket_options)

    urllib3_cn.create_connection = _patched_create_conn

import requests
import yadisk
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)
from tqdm import tqdm

import paths
import previews as preview_mod
from fetch import DeadHosts, FetchFailure, fetch_to_file
from state import State, is_deferred_error
from utils import (
    avoid_case_collision,
    check_target_dir,
    classify_local_error,
    ensure_dir,
    file_size_safe,
    resolve_local_path,
    to_long_path,
)

log = logging.getLogger("yadisk_backup")

# Скип-листы для путей которые НИКОГДА не качаем
SKIP_PATH_PREFIXES = ("disk:/Корзина", "disk:/Trash", "/Корзина", "/Trash")

# ── сбор HTTP-ошибок для отчёта ─────────────────────────────────────────────
# Без тела ответа и заголовков отчёт показывает только «HTTP 500», и понять,
# сгорел шард или это 503 с Retry-After, невозможно.

HTTP_ERRORS_MAX = 25
_http_errors_lock = threading.Lock()


def _http_error_details(exc: BaseException) -> Optional[dict]:
    """Статус, полезные заголовки и начало тела ответа из исключения ya-disk."""
    response = getattr(exc, "response", None)
    if response is None:
        return None
    try:
        status = int(getattr(response, "status_code", 0) or 0)
    except (TypeError, ValueError):
        return None
    if status < 400:
        return None
    out: dict = {"status": status}
    headers = getattr(response, "headers", None) or {}
    for name in ("Retry-After", "X-Request-Id", "X-Yandex-Req-Id", "Server"):
        try:
            value = str(headers.get(name, "") or "")
        except Exception:  # noqa: BLE001 — заголовки бывают любыми
            value = ""
        if value:
            out[name.lower()] = value[:200]
    try:
        out["body_head"] = str(getattr(response, "text", "") or "")[:1024]
    except Exception:  # noqa: BLE001
        out["body_head"] = ""
    return out


def record_http_error(remote_path: str, exc: BaseException) -> None:
    """Дописывает HTTP-ошибку в общий файл. Никогда не бросает исключений."""
    details = _http_error_details(exc)
    if not details:
        return
    details["path"] = remote_path[:300]
    details["at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    with _http_errors_lock:
        try:
            current: list = []
            if paths.HTTP_ERRORS_FILE.exists():
                try:
                    data = json.loads(paths.HTTP_ERRORS_FILE.read_text(encoding="utf-8"))
                    if isinstance(data, list):
                        current = data
                except (OSError, ValueError):
                    current = []
            current.append(details)
            tmp = paths.HTTP_ERRORS_FILE.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(current[-HTTP_ERRORS_MAX:], ensure_ascii=False, indent=1),
                encoding="utf-8",
            )
            os.replace(tmp, paths.HTTP_ERRORS_FILE)
        except OSError:
            pass


# ── retry-обёртка для сетевых вызовов ───────────────────────────────────────


# Ошибки, повторять которые бессмысленно: токен не станет валидным,
# а удалённый файл не появится, сколько ни жди.
NON_RETRYABLE = (
    yadisk.exceptions.UnauthorizedError,
    yadisk.exceptions.ForbiddenError,
    yadisk.exceptions.PathNotFoundError,
)

# Пауза при 429 — Яндекс просит подождать, экспонента с 1 секунды тут не поможет
RATE_LIMIT_WAIT_SEC = 60.0


def _is_retryable(exc: BaseException) -> bool:
    """Решает, стоит ли retry-ить исключение."""
    if isinstance(exc, NON_RETRYABLE):
        return False
    if isinstance(exc, yadisk.exceptions.YaDiskError):
        return True
    return isinstance(exc, (OSError, ConnectionError, TimeoutError))


_wait_exponential = wait_exponential(multiplier=1, min=1, max=10)


def _wait_policy(retry_state) -> float:
    """429 → ждём минуту, остальное → экспонента."""
    exc = retry_state.outcome.exception() if retry_state.outcome else None
    if isinstance(exc, yadisk.exceptions.TooManyRequestsError):
        log.warning("Поймали 429 TooManyRequests — жду %.0f сек", RATE_LIMIT_WAIT_SEC)
        return RATE_LIMIT_WAIT_SEC
    return _wait_exponential(retry_state)


_retry = retry(
    retry=retry_if_exception(_is_retryable),
    wait=_wait_policy,
    stop=stop_after_attempt(2),
    reraise=True,
)


# ── обход дерева ────────────────────────────────────────────────────────────


def _normalize_remote(path: str) -> str:
    """Приводит путь к виду 'disk:/foo/bar' — так возвращает API."""
    if path.startswith("disk:"):
        return path
    if path.startswith("/"):
        return "disk:" + path
    return "disk:/" + path


def _is_skipped(path: str) -> bool:
    return any(path.startswith(p) for p in SKIP_PATH_PREFIXES)


def scan_tree(
    client: yadisk.Client,
    state: State,
    root: str = "/",
    on_file: Optional[Callable[[str, int], None]] = None,
    on_dir: Optional[Callable[[str], None]] = None,
) -> tuple[int, int]:
    """Обходит дерево начиная с root. Регистрирует файлы как pending.
    Возвращает (количество файлов, суммарный размер).
    """
    files_count = 0
    bytes_total = 0

    # Очередь путей для обхода — итеративный BFS, не плодим рекурсию на 10 уровней
    queue: list[str] = [root]

    while queue:
        cur = queue.pop()
        if _is_skipped(_normalize_remote(cur)):
            log.info("Пропускаю корзину: %s", cur)
            continue

        try:
            entries = _list_dir(client, cur)
        except yadisk.exceptions.PathNotFoundError:
            log.warning("Путь не найден: %s", cur)
            continue
        except yadisk.exceptions.UnauthorizedError:
            raise  # пробрасываем выше — токен битый
        except Exception as e:
            log.error("Не смог прочитать %s: %s", cur, e)
            continue

        for entry in entries:
            entry_path = entry.path  # вид 'disk:/foo/bar'
            if _is_skipped(entry_path):
                continue

            if entry.type == "dir":
                if on_dir:
                    on_dir(entry_path)
                queue.append(entry_path)
            elif entry.type == "file":
                state.mark_pending(
                    remote_path=entry_path,
                    size=int(entry.size) if entry.size is not None else None,
                    md5=getattr(entry, "md5", None),
                )
                files_count += 1
                if entry.size:
                    bytes_total += int(entry.size)
                if on_file:
                    on_file(entry_path, int(entry.size or 0))

    return files_count, bytes_total


@_retry
def _list_dir(client: yadisk.Client, path: str) -> list:
    """listdir с retry. Возвращает list ResourceObject."""
    # max_items=None → yadisk сам пагинирует
    return list(client.listdir(path, max_items=None))


# ── обход только медиа (фото/видео) через get_files ─────────────────────────


def scan_media(
    client: yadisk.Client,
    state: State,
    media_types: tuple[str, ...] = ("image", "video"),
    on_file: Optional[Callable[[str, int], None]] = None,
) -> tuple[int, int]:
    """Альтернатива scan_tree: использует client.get_files() с фильтром по типу.
    Возвращает все фото и видео со всего диска плоским списком.

    Это покрывает альбомы «Камера», «Видео», «Скриншоты» (это умные виды Яндекса
    над теми же файлами), но НЕ читает «Альбом семьи», «Люди на фото» и пр. —
    их API не отдаёт. Зато берёт все медиа из всех папок диска целиком.
    """
    files_count = 0
    bytes_total = 0

    for media_type in media_types:
        log.info("Запрашиваю media_type=%s...", media_type)
        for entry in _iter_files(client, media_type=media_type):
            entry_path = entry.path
            if _is_skipped(entry_path):
                continue
            if entry.type != "file":
                continue

            state.mark_pending(
                remote_path=entry_path,
                size=int(entry.size) if entry.size is not None else None,
                md5=getattr(entry, "md5", None),
            )
            files_count += 1
            if entry.size:
                bytes_total += int(entry.size)
            if on_file:
                on_file(entry_path, int(entry.size or 0))

    return files_count, bytes_total


@_retry
def _get_files_page(
    client: yadisk.Client,
    media_type: str,
    offset: int,
    limit: int,
) -> list:
    """Одна страница из get_files с retry."""
    return list(
        client.get_files(
            media_type=media_type,
            offset=offset,
            limit=limit,
        )
    )


def _iter_files(client: yadisk.Client, media_type: str, page_size: int = 200):
    """Итератор по всем файлам диска для заданного media_type.

    Сами пагинируем — yadisk.get_files() умеет итератор, но мы хотим
    обернуть КАЖДУЮ страницу в retry, а не одну общую попытку.
    """
    offset = 0
    while True:
        page = _get_files_page(client, media_type, offset, page_size)
        if not page:
            return
        for item in page:
            yield item
        if len(page) < page_size:
            return
        offset += page_size


# ── скачивание ──────────────────────────────────────────────────────────────


@dataclass
class DownloadJob:
    remote_path: str
    size: Optional[int]
    md5: Optional[str]
    # «storage_down | host» из прошлого прохода: такой файл пробуем раньше других
    error: Optional[str] = None
    storage_host: Optional[str] = None

    @property
    def was_deferred(self) -> bool:
        return is_deferred_error(self.error)


@dataclass
class DownloadResult:
    job: DownloadJob
    status: str  # downloaded | skipped | failed | deferred | disk_full
    local_path: Optional[str]
    bytes_written: int
    error: Optional[str]
    duration_ms: int
    kind: Optional[str] = None  # тип ошибки: local_disk | local_file | storage_down …
    host: Optional[str] = None  # сервер хранения, который не ответил
    preview_path: Optional[str] = None  # превью отложенного фото

    @property
    def is_local_error(self) -> bool:
        return self.status == "failed" and self.kind in ("local_disk", "local_file")


def _ensure_local_dirs(local_path: str) -> None:
    parent = os.path.dirname(local_path)
    if parent:
        ensure_dir(to_long_path(parent))


def _cleanup_failed_file(local_path: str) -> None:
    """Удаляет недокачанный или пустой файл при сбое, чтобы не мусорить на диске."""
    try:
        p = to_long_path(local_path)
        if os.path.exists(p):
            os.remove(p)
    except OSError:
        pass


def _download_file(client: yadisk.Client, remote: str, local: str) -> None:
    """Низкоуровневый вызов download БЕЗ повторов — одна попытка.

    n_retries=0 отключает внутренние 3 повтора yadisk.
    Таймаут (10.0, 10.0): 10 сек на коннект, 10 сек между чанками.
    Внешнего @_retry намеренно нет: мёртвые шарды Яндекса отваливаются за один
    таймаут (10 сек) и не блокируют поток повторной попыткой. Сбойные файлы
    помечаются 'failed' и докачиваются позже командой retry-failed.
    """
    try:
        client.download(
            remote,
            to_long_path(local),
            timeout=(10.0, 10.0),
            n_retries=0,
            retry_interval=0.0,
        )
    except TypeError:
        # Для моков и тестов, где client.download не принимает kwargs
        client.download(remote, to_long_path(local))


def check_free_space(path: str, required_bytes: int = 0) -> bool:
    """Проверяет наличие свободного места (требуемый размер + 50 МБ запас)."""
    try:
        usage = shutil.disk_usage(path)
        return usage.free >= (required_bytes + 50 * 1024 * 1024)
    except OSError:
        return True


def _transfer(
    client: yadisk.Client,
    job: DownloadJob,
    local_path: str,
    dead_hosts: Optional[DeadHosts],
    http_session: Optional[requests.Session],
) -> None:
    """Одна попытка скачивания: ссылка → прямой запрос к серверу хранения.

    Клиенты без `get_download_link` (моки в тестах, очень старый yadisk)
    скачивают прежним способом — через сам клиент.
    """
    getter = getattr(client, "get_download_link", None)
    if getter is None:
        _download_file(client, job.remote_path, local_path)
        return
    link = getter(job.remote_path)
    fetch_to_file(
        link,
        local_path,
        expected_size=job.size,
        expected_md5=job.md5,
        dead_hosts=dead_hosts,
        session=http_session,
    )


def _download_one(
    job: DownloadJob,
    client: yadisk.Client,
    download_dir: str,
    dead_hosts: Optional[DeadHosts] = None,
    http_session: Optional[requests.Session] = None,
    make_previews: bool = False,
) -> DownloadResult:
    start = time.monotonic()
    local_path = resolve_local_path(download_dir, job.remote_path)
    local_path = avoid_case_collision(local_path)
    try:
        _ensure_local_dirs(local_path)
    except OSError as e:
        # Диск мог отключиться посреди прохода: файл помечаем сбойным, чтобы
        # потоки не падали с необработанным исключением. Класс ошибки важен:
        # по нему проход поймёт, что пора остановиться (B0).
        dur = int((time.monotonic() - start) * 1000)
        reason = getattr(e, "strerror", None) or str(e)
        return DownloadResult(
            job=job,
            status="failed",
            local_path=local_path,
            bytes_written=0,
            error=f"Не удалось создать папку: {reason}",
            duration_ms=dur,
            kind=classify_local_error(e),
        )

    # Resume-логика: если файл уже скачан корректно — skipped
    existing_size = file_size_safe(local_path)
    if (
        existing_size is not None
        and job.size is not None
        and existing_size == job.size
    ):
        dur = int((time.monotonic() - start) * 1000)
        return DownloadResult(
            job=job,
            status="skipped",
            local_path=local_path,
            bytes_written=0,
            error=None,
            duration_ms=dur,
        )

    # Проверка свободного места перед началом записи
    if job.size and not check_free_space(download_dir, job.size):
        dur = int((time.monotonic() - start) * 1000)
        return DownloadResult(
            job=job,
            status="disk_full",
            local_path=None,
            bytes_written=0,
            error="NoSpaceLeft: Недостаточно свободного места на диске",
            duration_ms=dur,
            kind="disk_full",
        )

    # Старый файл (частичный или другого размера) НЕ удаляем заранее: новый
    # пишется во временный .part и подменяет его атомарно. Если диск отключится
    # на середине, прежняя копия останется на месте.

    try:
        _transfer(client, job, local_path, dead_hosts, http_session)
    except FetchFailure as e:
        dur = int((time.monotonic() - start) * 1000)
        _cleanup_failed_file(local_path)
        if e.is_deferred:
            # Яндекс не отдаёт файл: он остаётся в очереди и попыток не тратит.
            if dead_hosts is not None and e.host:
                dead_hosts.mark(e.host)
            preview = None
            if make_previews and preview_mod.is_photo(job.remote_path):
                # Пока оригинал ждёт — кладём уменьшенную копию, чтобы человек
                # видел, чего не хватает. Тот же мёртвый сервер её не отдаст:
                # пропуск мгновенный, без сети.
                preview = preview_mod.make_disk_preview(
                    client, job.remote_path, download_dir,
                    dead_hosts=dead_hosts, session=http_session,
                )
            return DownloadResult(
                job, "deferred", None, 0, str(e), dur,
                kind=e.kind, host=e.host, preview_path=preview,
            )
        return DownloadResult(job, "failed", None, 0, str(e), dur, kind=e.kind, host=e.host)
    except yadisk.exceptions.TooManyRequestsError as e:
        dur = int((time.monotonic() - start) * 1000)
        _cleanup_failed_file(local_path)
        record_http_error(job.remote_path, e)
        return DownloadResult(
            job, "deferred", None, 0, f"429 TooManyRequests: {e}", dur, kind="rate_limit"
        )
    except yadisk.exceptions.PathNotFoundError as e:
        dur = int((time.monotonic() - start) * 1000)
        _cleanup_failed_file(local_path)
        record_http_error(job.remote_path, e)
        return DownloadResult(
            job, "failed", None, 0, f"PathNotFound: {e}", dur, kind="not_found"
        )
    except OSError as e:
        dur = int((time.monotonic() - start) * 1000)
        _cleanup_failed_file(local_path)
        if getattr(e, "errno", None) == errno.ENOSPC:
            return DownloadResult(
                job, "disk_full", None, 0, f"NoSpaceLeft: {e}", dur, kind="disk_full"
            )
        return DownloadResult(
            job, "failed", None, 0, f"{type(e).__name__}: {e}", dur,
            kind=classify_local_error(e),
        )
    except Exception as e:
        dur = int((time.monotonic() - start) * 1000)
        _cleanup_failed_file(local_path)
        # Тело и заголовки сохраняем до того, как исключение потеряется.
        record_http_error(job.remote_path, e)
        return DownloadResult(job, "failed", None, 0, f"{type(e).__name__}: {e}", dur)

    bytes_written = file_size_safe(local_path) or 0
    dur = int((time.monotonic() - start) * 1000)
    return DownloadResult(
        job=job,
        status="downloaded",
        local_path=local_path,
        bytes_written=bytes_written,
        error=None,
        duration_ms=dur,
    )


# Каждому потоку — свой клиент. Шарить yadisk.Client между потоками небезопасно.
_thread_local = threading.local()
# Реестр всех созданных клиентов — чтобы закрыть сокеты после остановки пула
_all_clients: list[yadisk.Client] = []
_all_sessions: list[requests.Session] = []
_all_clients_lock = threading.Lock()


def _get_thread_client(token: str) -> yadisk.Client:
    cli = getattr(_thread_local, "client", None)
    if cli is None:
        cli = yadisk.Client(token=token)
        _thread_local.client = cli
        with _all_clients_lock:
            _all_clients.append(cli)
    return cli


def _close_all_clients() -> None:
    """Закрывает клиентов всех воркеров. Вызывать ПОСЛЕ остановки пула —
    потоки уже мертвы, их thread-local исчез вместе с ними.
    """
    with _all_clients_lock:
        clients = list(_all_clients)
        _all_clients.clear()
    for cli in clients:
        try:
            cli.close()
        except Exception:  # noqa: BLE001 — закрытие не должно ронять выход
            pass
    with _all_clients_lock:
        sessions = list(_all_sessions)
        _all_sessions.clear()
    for s in sessions:
        try:
            s.close()
        except Exception:  # noqa: BLE001
            pass


def _get_thread_http_session() -> requests.Session:
    """Своё HTTP-соединение на поток: сервер хранения переиспользуется между файлами."""
    s = getattr(_thread_local, "http", None)
    if s is None:
        s = requests.Session()
        _thread_local.http = s
        with _all_clients_lock:
            _all_sessions.append(s)
    return s


# Пер-сессионный счётчик скачивания обычного диска: пишется в файл, чтобы веб-UI
# показывал, сколько файлов успешно скачано в ТЕКУЩЕЙ сессии (обнуляется при каждом
# запуске download_all). Файл остаётся на диске и после завершения/зависания сессии,
# поэтому видно, что работа не была напрасной.
DISK_STATUS_FILE = paths.DISK_STATUS_FILE

# Момент старта процесса: UI по нему отличает свой статус от прошлой сессии.
SESSION_STARTED_AT = time.time()


# Сколько локальных ошибок ПОДРЯД означает «диск отключился». Один-два файла с
# плохим именем — это про файлы; восемь подряд — про носитель. При достижении
# порога проход останавливается, а файлы остаются в очереди (попытки не тратятся).
LOCAL_ERROR_LIMIT = 8


def _write_disk_status(
    counters: dict[str, int],
    total_files: int,
    total_bytes: int,
    active: bool,
    state: str = "running",
    waiting: Optional[int] = None,
) -> None:
    data = {
        "active": active,
        "state": state,
        "started_at": SESSION_STARTED_AT,
        "downloaded": int(counters.get("downloaded", 0)),
        "skipped": int(counters.get("skipped", 0)),
        "failed": int(counters.get("failed", 0)),
        "deferred": int(counters.get("deferred", 0)),
        "bytes": int(counters.get("bytes", 0)),
        "total_files": int(total_files or 0),
        "total_bytes": int(total_bytes or 0),
        "updated_at": time.time(),
    }
    if waiting is not None:
        data["waiting"] = int(waiting)
    try:
        DISK_STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = DISK_STATUS_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, DISK_STATUS_FILE)
    except Exception:  # noqa: BLE001 — статус не должен ронять скачивание
        pass


def write_status_start() -> None:
    """Нулевой статус в начале сессии: UI сразу видит «идёт», а не «подготовка».

    Пишется до проверки токена и сканирования — иначе плашка висит в
    «⏳ подготовка», пока идёт сетевой запрос.
    """
    _write_disk_status({"downloaded": 0, "skipped": 0, "failed": 0, "bytes": 0}, 0, 0, True)


def download_all(
    token: str,
    state: State,
    download_dir: str,
    workers: int = 4,
    jobs: Optional[Iterator[DownloadJob]] = None,
    session: Optional[dict[str, int]] = None,
    dead_hosts: Optional[DeadHosts] = None,
    prioritize_deferred: bool = True,
    make_previews: bool = True,
) -> dict[str, int]:
    """Скачивает все pending файлы из state в пул потоков.
    Возвращает счётчики: {downloaded, skipped, failed, deferred, bytes}.

    ``session`` — счётчики предыдущих проходов этого же процесса. С них начинаем
    и в файл статуса пишем кумулятив: иначе повторный проход по сбойным
    перезаписывал итог сессии цифрами последнего прохода.

    ``dead_hosts`` — реестр молчащих серверов хранения. Если передать общий на
    весь полный цикл, мёртвый сервер перестанет тормозить каждый проход.
    """
    if workers < 1:
        workers = 1
    if workers > 5:
        log.warning("workers=%d > 5 — Яндекс будет резать по rate limit. Снижаю до 5.", workers)
        workers = 5

    # Папку проверяем до запуска потоков: отключённый диск должен давать
    # понятное сообщение, а не окно необработанного исключения.
    problem = check_target_dir(download_dir)
    if problem:
        print(f"[ОШИБКА] {problem}", file=sys.stderr)
        log.error(problem)
        raise SystemExit(2)

    if dead_hosts is None:
        dead_hosts = DeadHosts()

    # A3: первая строка журнала — куда кладём файлы. Пользователь должен видеть
    # это сразу, а не искать папку в настройках.
    target = os.path.abspath(download_dir)
    print(f"Скачиваем в: {target}", flush=True)
    log.info("Скачиваем в: %s", target)

    # Превью живут, только пока файл ждёт Яндекса. Убираем те, что уже не нужны,
    # и брошенные .part от прерванных проходов.
    try:
        swept = preview_mod.sweep(state, download_dir)
        if swept:
            log.info("Убрано лишних превью: %d", swept)
    except Exception:  # noqa: BLE001 — превью не должны мешать скачиванию
        pass

    # Собираем список jobs из БД (или из переданного итератора — для retry-failed)
    if jobs is None:
        jobs_list = [
            DownloadJob(
                remote_path=row["remote_path"],
                size=row["size"],
                md5=row["md5"],
                error=_row_get(row, "error"),
                storage_host=_row_get(row, "storage_host"),
            )
            for row in state.get_pending(deferred_first=prioritize_deferred)
        ]
    else:
        jobs_list = list(jobs)

    counters = {
        key: int((session or {}).get(key, 0) or 0)
        for key in ("downloaded", "skipped", "failed", "deferred", "bytes")
    }

    if not jobs_list:
        log.info("Нет файлов для скачивания.")
        # Пустой проход не должен обнулять итог сессии: пишем то, что уже накоплено.
        _write_disk_status(counters, 0, 0, False)
        return counters

    total_bytes = sum(j.size for j in jobs_list if j.size)
    waiting_count = _waiting_stats(state)
    _write_disk_status(counters, len(jobs_list), total_bytes, True, waiting=waiting_count)

    deferred_jobs = {j.remote_path for j in jobs_list if j.was_deferred}
    deferred_ok = 0

    file_bar = tqdm(total=len(jobs_list), unit="file", desc="Файлы", position=0)
    byte_bar = tqdm(
        total=total_bytes,
        unit="B",
        unit_scale=True,
        unit_divisor=1024,
        desc="Байты",
        position=1,
    )

    def worker(job: DownloadJob) -> DownloadResult:
        client = _get_thread_client(token)
        return _download_one(
            job, client, download_dir, dead_hosts, _get_thread_http_session(),
            make_previews=make_previews,
        )

    # Без `with`: его выход дожидается ВСЕЙ очереди, и Ctrl+C выглядит зависанием.
    pool = ThreadPoolExecutor(max_workers=workers)
    stop_state = "done"
    consecutive_local = 0
    try:
        futures = {pool.submit(worker, j): j for j in jobs_list}
        try:
            for fut in as_completed(futures):
                res = fut.result()
                _apply_result(state, res, counters, file_bar, byte_bar)

                if res.status == "disk_full":
                    print("\n[ВНИМАНИЕ] Недостаточно свободного места на диске! Прерываю скачивание...", file=sys.stderr)
                    log.error("Диск заполнен! Прерываю скачивание: %s", res.error)
                    stop_state = "disk_full"
                    pool.shutdown(wait=False, cancel_futures=True)
                    break

                # waiting_count уже содержит все отложенные файлы на момент старта
                # прохода, включая те, что сейчас в очереди. Прибавлять «снова
                # отложенные» нельзя — иначе 400 файлов превращались в «ещё ждут: 800».
                if res.status == "deferred":
                    if not res.job.was_deferred:
                        waiting_count += 1
                elif res.status in ("downloaded", "skipped") and res.job.was_deferred:
                    deferred_ok += 1
                    waiting_count = max(0, waiting_count - 1)

                # B0: не сжигаем попытки у сотен файлов, если отключился диск.
                if res.is_local_error:
                    consecutive_local += 1
                else:
                    consecutive_local = 0
                if consecutive_local >= LOCAL_ERROR_LIMIT:
                    print(
                        "\n[ОСТАНОВКА] Диск отключён — подключите его и нажмите «Продолжить». "
                        "Задание не потеряно, файлы остались в очереди.",
                        file=sys.stderr,
                        flush=True,
                    )
                    log.error(
                        "Останавливаю проход: %d локальных ошибок подряд (%s)",
                        consecutive_local, res.error,
                    )
                    stop_state = "disk_offline"
                    pool.shutdown(wait=False, cancel_futures=True)
                    break

                _write_disk_status(
                    counters, len(jobs_list), total_bytes, True, waiting=waiting_count
                )
        except KeyboardInterrupt:
            print("\nCtrl+C — отменяю очередь, дожидаюсь только текущих файлов...",
                  file=sys.stderr)
            log.warning("Ctrl+C — отменяю оставшиеся задачи и закрываю пул...")
            stop_state = "stopped"
            pool.shutdown(wait=False, cancel_futures=True)
            raise
        pool.shutdown(wait=True)
        # Закрываем сокеты только когда воркеры точно остановились
        _close_all_clients()
    finally:
        file_bar.close()
        byte_bar.close()
        _write_disk_status(
            counters, len(jobs_list), total_bytes, False,
            state=stop_state, waiting=waiting_count,
        )

    if deferred_jobs:
        tail = f" · ещё ждут: {waiting_count}" if waiting_count else ""
        print(
            f"Из {len(deferred_jobs)} ждавших Яндекса скачано {deferred_ok}{tail}",
            flush=True,
        )
        log.info(
            "Из %d ждавших Яндекса скачано %d (осталось ждать: %d)",
            len(deferred_jobs), deferred_ok, waiting_count,
        )

    return counters


def _row_get(row, name: str):
    """Значение колонки из sqlite3.Row, если она есть (старые выборки её не отдают)."""
    try:
        return row[name]
    except (IndexError, KeyError):
        return None


def _waiting_stats(state: State) -> int:
    """Сколько файлов ждут Яндекса. Не роняет скачивание, если БД не умеет."""
    try:
        return int(state.get_waiting_stats())
    except Exception:  # noqa: BLE001
        return 0


def _apply_result(
    state: State,
    res: DownloadResult,
    counters: dict[str, int],
    file_bar: tqdm,
    byte_bar: tqdm,
) -> None:
    """Применяет результат скачивания к БД и счётчикам."""
    if res.status == "downloaded":
        state.mark_downloaded(res.job.remote_path, res.local_path or "")
        counters["downloaded"] += 1
        counters["bytes"] += res.bytes_written
        byte_bar.update(res.bytes_written)
        log.info(
            "OK | %s | %d B | %d ms | %s",
            res.status,
            res.bytes_written,
            res.duration_ms,
            res.job.remote_path,
        )
    elif res.status == "skipped":
        state.mark_skipped(res.job.remote_path, res.local_path or "")
        counters["skipped"] += 1
        if res.job.size:
            byte_bar.update(res.job.size)
        log.info(
            "SKIP | already exists | %s",
            res.job.remote_path,
        )
    elif res.status == "deferred":
        # Файл не сбойный: Яндекс не отдал его сейчас. Остаётся pending с
        # маркером и попыток не тратит.
        state.mark_deferred(res.job.remote_path, res.kind or "storage_down", res.host)
        if res.preview_path:
            try:
                state.set_preview_path(res.job.remote_path, res.preview_path)
            except Exception:  # noqa: BLE001
                pass
        counters["deferred"] = int(counters.get("deferred", 0)) + 1
        log.warning(
            "ЖДЁТ | %s | %s | %s",
            res.kind or "storage_down",
            res.error,
            res.job.remote_path,
        )
    else:
        state.mark_failed(res.job.remote_path, res.error or "unknown")
        counters["failed"] += 1
        log.error(
            "FAIL | %s | %s | %s",
            res.error,
            res.duration_ms,
            res.job.remote_path,
        )
    file_bar.update(1)


# ── проверка токена ─────────────────────────────────────────────────────────


def check_token(token: str) -> tuple[bool, str]:
    """Проверяет, валиден ли токен. Возвращает (ok, message).

    Сеть до Яндекса иногда флапает на уровне сокета (WinError 10042),
    поэтому временные ошибки соединения повторяем несколько раз с паузой.
    """
    if not token or token.strip() == "" or token.startswith("AQAAA_paste"):
        return False, (
            "Токен не задан. Открой https://yandex.ru/dev/disk/poligon/, "
            "получи OAuth-токен по кнопке справа вверху и положи его в .env "
            "как YA_DISK_TOKEN=..."
        )

    last_error: Exception | None = None
    for attempt in range(6):
        try:
            cli = yadisk.Client(token=token)
            if not cli.check_token():
                return False, (
                    "Токен невалиден (Яндекс ответил, что он не работает). "
                    "Получи новый на https://yandex.ru/dev/disk/poligon/"
                )
            info = cli.get_disk_info()
            used = getattr(info, "used_space", 0) or 0
            total = getattr(info, "total_space", 0) or 0
            cli.close()
            return True, f"Токен OK. Использовано {used / 1e9:.2f} ГБ из {total / 1e9:.2f} ГБ."
        except yadisk.exceptions.UnauthorizedError:
            return False, (
                "Токен не принят (401 Unauthorized). Получи новый на "
                "https://yandex.ru/dev/disk/poligon/"
            )
        except Exception as e:
            last_error = e
            if attempt < 5:
                time.sleep(2 * (attempt + 1))

    return False, f"Не удалось проверить токен: {type(last_error).__name__}: {last_error}"
