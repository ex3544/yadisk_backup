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

import yadisk
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)
from tqdm import tqdm

import paths
from state import State
from utils import (
    avoid_case_collision,
    ensure_dir,
    file_size_safe,
    resolve_local_path,
    to_long_path,
)

log = logging.getLogger("yadisk_backup")

# Скип-листы для путей которые НИКОГДА не качаем
SKIP_PATH_PREFIXES = ("disk:/Корзина", "disk:/Trash", "/Корзина", "/Trash")


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


@dataclass
class DownloadResult:
    job: DownloadJob
    status: str  # downloaded | skipped | failed
    local_path: Optional[str]
    bytes_written: int
    error: Optional[str]
    duration_ms: int


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


def _download_one(
    job: DownloadJob,
    client: yadisk.Client,
    download_dir: str,
) -> DownloadResult:
    start = time.monotonic()
    local_path = resolve_local_path(download_dir, job.remote_path)
    local_path = avoid_case_collision(local_path)
    _ensure_local_dirs(local_path)

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
        )

    # Если файл частично скачан или с другим размером — перезаписываем
    if existing_size is not None:
        try:
            os.remove(to_long_path(local_path))
        except OSError:
            pass

    try:
        _download_file(client, job.remote_path, local_path)
    except yadisk.exceptions.TooManyRequestsError as e:
        dur = int((time.monotonic() - start) * 1000)
        _cleanup_failed_file(local_path)
        return DownloadResult(job, "failed", None, 0, f"429 TooManyRequests: {e}", dur)
    except yadisk.exceptions.PathNotFoundError as e:
        dur = int((time.monotonic() - start) * 1000)
        _cleanup_failed_file(local_path)
        return DownloadResult(job, "failed", None, 0, f"PathNotFound: {e}", dur)
    except OSError as e:
        dur = int((time.monotonic() - start) * 1000)
        _cleanup_failed_file(local_path)
        if getattr(e, "errno", None) == errno.ENOSPC:
            return DownloadResult(job, "disk_full", None, 0, f"NoSpaceLeft: {e}", dur)
        return DownloadResult(job, "failed", None, 0, f"{type(e).__name__}: {e}", dur)
    except Exception as e:
        dur = int((time.monotonic() - start) * 1000)
        _cleanup_failed_file(local_path)
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


# Пер-сессионный счётчик скачивания обычного диска: пишется в файл, чтобы веб-UI
# показывал, сколько файлов успешно скачано в ТЕКУЩЕЙ сессии (обнуляется при каждом
# запуске download_all). Файл остаётся на диске и после завершения/зависания сессии,
# поэтому видно, что работа не была напрасной.
DISK_STATUS_FILE = paths.DISK_STATUS_FILE

# Момент старта процесса: UI по нему отличает свой статус от прошлой сессии.
SESSION_STARTED_AT = time.time()


def _write_disk_status(counters: dict[str, int], total_files: int, total_bytes: int, active: bool) -> None:
    data = {
        "active": active,
        "started_at": SESSION_STARTED_AT,
        "downloaded": int(counters.get("downloaded", 0)),
        "skipped": int(counters.get("skipped", 0)),
        "failed": int(counters.get("failed", 0)),
        "bytes": int(counters.get("bytes", 0)),
        "total_files": int(total_files or 0),
        "total_bytes": int(total_bytes or 0),
        "updated_at": time.time(),
    }
    try:
        DISK_STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = DISK_STATUS_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, DISK_STATUS_FILE)
    except Exception:  # noqa: BLE001 — статус не должен ронять скачивание
        pass


def download_all(
    token: str,
    state: State,
    download_dir: str,
    workers: int = 4,
    jobs: Optional[Iterator[DownloadJob]] = None,
) -> dict[str, int]:
    """Скачивает все pending файлы из state в пул потоков.
    Возвращает счётчики: {downloaded, skipped, failed, bytes}.
    """
    if workers < 1:
        workers = 1
    if workers > 5:
        log.warning("workers=%d > 5 — Яндекс будет резать по rate limit. Снижаю до 5.", workers)
        workers = 5

    ensure_dir(download_dir)

    # Собираем список jobs из БД (или из переданного итератора — для retry-failed)
    if jobs is None:
        jobs_list = [
            DownloadJob(
                remote_path=row["remote_path"],
                size=row["size"],
                md5=row["md5"],
            )
            for row in state.get_pending()
        ]
    else:
        jobs_list = list(jobs)

    if not jobs_list:
        log.info("Нет файлов для скачивания.")
        _write_disk_status({"downloaded": 0, "skipped": 0, "failed": 0, "bytes": 0}, 0, 0, False)
        return {"downloaded": 0, "skipped": 0, "failed": 0, "bytes": 0}

    total_bytes = sum(j.size for j in jobs_list if j.size)
    counters = {"downloaded": 0, "skipped": 0, "failed": 0, "bytes": 0}
    _write_disk_status(counters, len(jobs_list), total_bytes, True)

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
        return _download_one(job, client, download_dir)

    # Без `with`: его выход дожидается ВСЕЙ очереди, и Ctrl+C выглядит зависанием.
    pool = ThreadPoolExecutor(max_workers=workers)
    try:
        futures = {pool.submit(worker, j): j for j in jobs_list}
        try:
            for fut in as_completed(futures):
                res = fut.result()
                if res.status == "disk_full":
                    file_bar.close()
                    byte_bar.close()
                    print("\n[ВНИМАНИЕ] Недостаточно свободного места на диске! Прерываю скачивание...", file=sys.stderr)
                    log.error("Диск заполнен! Прерываю скачивание: %s", res.error)
                    pool.shutdown(wait=False, cancel_futures=True)
                    _apply_result(state, res, counters, file_bar, byte_bar)
                    break
                _apply_result(state, res, counters, file_bar, byte_bar)
                _write_disk_status(counters, len(jobs_list), total_bytes, True)
        except KeyboardInterrupt:
            file_bar.close()
            byte_bar.close()
            print("\nCtrl+C — отменяю очередь, дожидаюсь только текущих файлов...",
                  file=sys.stderr)
            log.warning("Ctrl+C — отменяю оставшиеся задачи и закрываю пул...")
            pool.shutdown(wait=False, cancel_futures=True)
            raise
        pool.shutdown(wait=True)
        # Закрываем сокеты только когда воркеры точно остановились
        _close_all_clients()
    finally:
        file_bar.close()
        byte_bar.close()
        _write_disk_status(counters, len(jobs_list), total_bytes, False)

    return counters


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
