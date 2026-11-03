"""Прямое скачивание одного файла с сервера хранения Яндекса.

Зачем свой транспорт вместо `yadisk.Client.download()`:

* мы видим, на каком именно сервере хранения лежит файл (из редиректа),
  и запоминаем молчащие серверы — второй файл с того же сервера откладывается
  без запроса, а не ждёт 10 секунд впустую;
* OAuth-токен остаётся на первом запросе и не уходит на сервер хранения;
* размер и MD5 проверяются на лету — битый файл не попадёт в папку;
* ошибки типизированы: «Яндекс не отдаёт» (файл отложить) отличается от
  «файл битый» (сбойный) и «диск отключён» (остановить проход).

Модуль ничего не знает про Диск, Безлимит и базы: на входе ссылка, на выходе
файл и тип ошибки.
"""

from __future__ import annotations

import hashlib
import os
import re
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional
from urllib.parse import urljoin, urlparse

import requests

from utils import classify_local_error, to_long_path

# Типы ошибок, при которых файл не «сбойный», а ждёт Яндекса.
DEFER_KINDS = frozenset(
    {"storage_down", "storage_skip", "server_50x", "rate_limit", "timeout", "connection"}
)

# Сколько секунд считаем сервер хранения мёртвым. За полчаса Яндекс обычно
# возвращает сервер в строй. Пометка ставится только по сетевой ошибке —
# см. `DeadHosts.mark`.
DEAD_HOST_TTL = 30 * 60.0

_MD5_RE = re.compile(r"^[0-9a-fA-F]{32}$")
_URL_RE = re.compile(r"https?://\S+")


def is_md5(value: object) -> bool:
    """Валидная ли это контрольная сумма: 32 hex-символа."""
    return isinstance(value, str) and bool(_MD5_RE.match(value))


def scrub_urls(text: str) -> str:
    """Убирает подписанные ссылки из текста: они дают доступ к файлу."""
    return _URL_RE.sub("<ссылка скрыта>", text or "")


class FetchFailure(Exception):
    """Ошибка скачивания с типом. Тип решает, что делать с файлом."""

    def __init__(
        self,
        kind: str,
        detail: str = "",
        host: Optional[str] = None,
        status: Optional[int] = None,
    ) -> None:
        super().__init__(scrub_urls(detail) or kind)
        self.kind = kind
        self.detail = scrub_urls(detail)
        self.host = host
        self.status = status

    @property
    def is_deferred(self) -> bool:
        """Яндекс не отдаёт — файл ждёт следующего раза, а не считается сбойным."""
        return self.kind in DEFER_KINDS

    @property
    def is_local(self) -> bool:
        return self.kind in ("local_disk", "local_file", "disk_full")

    def __str__(self) -> str:  # для логов и поля error в БД
        return f"{self.kind}: {self.detail}" if self.detail else self.kind


class DeadHosts:
    """Реестр серверов хранения, которые не ответили.

    Живёт в памяти процесса: на одном проходе это ровно то, что нужно.
    Потоки пула делят один реестр, поэтому берём блокировку.

    Помечаем сервер мёртвым только по сетевой ошибке (`mark`): когда запрос
    висит и истекает таймаут, пропуск следующих файлов того же сервера
    экономит десятки секунд. Ответ 5xx приходит за доли секунды, и на него
    мы ничего не гасим: у тестера vovapuh 30.10 пачка из десяти 500 подряд
    выключила всю линию Диска на весь проход — из 1035 файлов скачалось три.
    """

    def __init__(
        self,
        ttl: float = DEAD_HOST_TTL,
        clock=time.monotonic,
        probes: Optional[int] = None,
    ) -> None:
        self._ttl = ttl
        self._clock = clock
        # Сколько настоящих запросов разрешить «мёртвому» хосту до истечения
        # TTL. Обычно 0 (старое поведение: пропуск без запроса). «Зверь»
        # задаёт 3 через YADISK_DEAD_HOST_PROBES, чтобы оживший сервер ловился
        # в этом же круге, а не через полчаса.
        if probes is None:
            try:
                probes = int(os.getenv("YADISK_DEAD_HOST_PROBES") or 0)
            except ValueError:
                probes = 0
        self._probes = max(0, int(probes))
        self._probes_left: dict[str, int] = {}
        self._hosts: dict[str, float] = {}
        self._lock = threading.Lock()

    def mark(self, host: Optional[str]) -> None:
        if not host:
            return
        with self._lock:
            self._hosts[host] = self._clock() + self._ttl
            self._probes_left[host] = self._probes

    def is_dead(self, host: Optional[str]) -> bool:
        """Чистая проверка для журнала и статусов — пробы не тратит."""
        if not host:
            return False
        now = self._clock()
        with self._lock:
            until = self._hosts.get(host)
            if until is None:
                return False
            if until <= now:
                self._hosts.pop(host, None)
                self._probes_left.pop(host, None)
                return False
            return True

    def allow(self, host: Optional[str]) -> bool:
        """Можно ли ходить на хост сейчас.

        Для живого — да. Для помеченного мёртвым — да, пока остались пробы:
        каждая такая проверка списывает одну пробу. Так «Зверь» делает
        несколько настоящих попыток за круг и замечает оживший сервер.
        """
        if not host:
            return True
        now = self._clock()
        with self._lock:
            until = self._hosts.get(host)
            if until is None:
                return True
            if until <= now:
                self._hosts.pop(host, None)
                self._probes_left.pop(host, None)
                return True
            left = self._probes_left.get(host, 0)
            if left <= 0:
                return False
            self._probes_left[host] = left - 1
            return True

    def clear(self) -> None:
        with self._lock:
            self._hosts.clear()
            self._probes_left.clear()

    def snapshot(self) -> list[str]:
        """Живые (не истёкшие) мёртвые серверы — для журнала и диагностики."""
        now = self._clock()
        with self._lock:
            self._hosts = {h: t for h, t in self._hosts.items() if t > now}
            self._probes_left = {h: n for h, n in self._probes_left.items()
                                 if h in self._hosts}
            return sorted(self._hosts)


@dataclass
class FetchResult:
    bytes_written: int
    md5: Optional[str]
    host: Optional[str]


def part_path_for(dst: str) -> str:
    """Скрытый временный файл рядом с целевым — один и тот же для одного пути.

    Имя зависит только от `dst`, поэтому недокачанный файл находится при
    следующей попытке и докачивается по `Range`. Не `<имя>.part`: на Диске
    вполне может лежать настоящий файл `видео.mp4.part`, и мы бы его затёрли.
    Плюс короткое имя не упирается в лимит длины пути.
    """
    parent = os.path.dirname(dst) or "."
    digest = hashlib.sha1(dst.encode("utf-8"), usedforsecurity=False).hexdigest()[:16]
    return os.path.join(parent, f".yadisk-{digest}.part")


def _absolute(url: str) -> str:
    """Протокол-относительные ссылки Яндекса ('//host/path') → https."""
    if url.startswith("//"):
        return "https:" + url
    return url


def _host_of(url: str) -> Optional[str]:
    try:
        return urlparse(url).hostname
    except ValueError:
        return None


def _remove_quiet(path: str) -> None:
    try:
        if os.path.exists(to_long_path(path)):
            os.remove(to_long_path(path))
    except OSError:
        pass


def _part_size(path: str) -> int:
    """Сколько уже лежит в недокачанном `.part` (0, если файла нет)."""
    try:
        return os.path.getsize(to_long_path(path))
    except OSError:
        return 0


def _check_status(status: int, host: Optional[str]) -> None:
    """Превращает HTTP-код в типизированную ошибку."""
    if status in (401, 403):
        raise FetchFailure("refused", f"HTTP {status} от сервера хранения", host=host, status=status)
    if status in (404, 410):
        raise FetchFailure("not_found", f"HTTP {status}: файла на сервере нет", host=host, status=status)
    if status == 429:
        raise FetchFailure("rate_limit", "HTTP 429: Яндекс просит подождать", host=host, status=status)
    if 500 <= status:
        raise FetchFailure("server_50x", f"HTTP {status} от сервера хранения", host=host, status=status)
    if status >= 400:
        raise FetchFailure("refused", f"HTTP {status}", host=host, status=status)


def fetch_to_file(
    link: str,
    dst: str,
    *,
    expected_size: Optional[int] = None,
    expected_md5: Optional[str] = None,
    dead_hosts: Optional[DeadHosts] = None,
    reject_pages: bool = False,
    session: Optional[requests.Session] = None,
    headers: Optional[dict[str, str]] = None,
    link_timeout: tuple[float, float] = (30.0, 60.0),
    storage_timeout: tuple[float, float] = (10.0, 10.0),
    chunk_size: int = 1024 * 1024,
    heartbeat: Optional[Callable[[], None]] = None,
    progress: Optional[Callable[[int, Optional[str]], None]] = None,
) -> FetchResult:
    """Скачивает ссылку в `dst` атомарно: временный файл → os.replace.

    `reject_pages=True` — для Безлимита и превью: если вместо файла пришёл
    HTML или JSON, это подмена (истёкшая сессия, капча, заглушка). У обычного
    Диска есть настоящие .html и .json, поэтому там проверка выключена.

    `headers` добавляются только к первому запросу — к самой ссылке от API
    (например, `Authorization: OAuth …` для превью). Редирект на сервер
    хранения уже подписан, и токен туда не уходит.
    """
    own_session = session is None
    s = session or requests.Session()
    url = _absolute(link)
    host: Optional[str] = None
    response = None

    # `.part` один и тот же для этого пути: если прошлая попытка оборвалась,
    # продолжаем с того места по Range, а не качаем гигабайты заново.
    part = part_path_for(dst)
    resume_from = _part_size(part)
    if expected_size is not None and resume_from >= expected_size:
        # Обрывок не меньше файла: либо прошлая попытка успела всё записать,
        # либо файл в облаке изменился. Проверять нечего — начинаем заново.
        _remove_quiet(part)
        resume_from = 0

    try:
        # Первый запрос — ссылка от API, редирект разбираем вручную: нужен host.
        for hop in range(5):
            hop_host = _host_of(url)
            if dead_hosts is not None and not dead_hosts.allow(hop_host):
                raise FetchFailure(
                    "storage_skip",
                    "сервер хранения не отвечает — пропускаю без запроса",
                    host=hop_host,
                )
            req_headers = {"Accept": "*/*"}
            if hop == 0 and headers:
                req_headers.update(headers)
            if resume_from > 0:
                req_headers["Range"] = f"bytes={resume_from}-"
            try:
                response = s.get(
                    url,
                    stream=True,
                    allow_redirects=False,
                    timeout=link_timeout if hop == 0 else storage_timeout,
                    headers=req_headers,
                )
            except requests.Timeout as e:
                wait = (storage_timeout if hop > 0 else link_timeout)[1]
                # По-русски и по делу: сколько секунд сервер не присылал данных.
                raise FetchFailure("timeout", f"нет данных {int(wait)} с", host=hop_host) from e
            except requests.RequestException as e:
                raise FetchFailure("connection", f"ошибка соединения: {e}", host=hop_host) from e

            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get("Location")
                response.close()
                response = None
                if not location:
                    raise FetchFailure("bad_redirect", "редирект без адреса", host=hop_host)
                url = urljoin(url, location)
                continue
            host = hop_host
            break
        else:
            raise FetchFailure("bad_redirect", "слишком много перенаправлений", host=host)

        if response.status_code == 416 and resume_from > 0:
            # Сервер не принял начало диапазона: обрывок не подходит.
            _remove_quiet(part)
            raise FetchFailure(
                "bad_file", "частичный файл не подошёл — скачаю заново", host=host
            )

        try:
            _check_status(response.status_code, host)
        except FetchFailure as e:
            # На отказ (403) и «нет файла» (404) держать обрывок незачем.
            # На 429 и 5xx — оставляем: следующий заход продолжит по Range.
            if not e.is_deferred:
                _remove_quiet(part)
            raise

        if reject_pages:
            ctype = (response.headers.get("Content-Type") or "").lower()
            if "text/html" in ctype or "application/json" in ctype:
                _remove_quiet(part)
                raise FetchFailure(
                    "bad_file",
                    f"вместо файла пришла страница ({ctype.split(';')[0]})",
                    host=host,
                )

        resumed = response.status_code == 206 and resume_from > 0
        hasher = hashlib.md5(usedforsecurity=False) if is_md5(expected_md5) else None
        if hasher is not None and resumed:
            # Контрольная сумма считается по всему файлу: сначала добавляем в
            # хеш уже скачанное, иначе итоговый MD5 будет неверным.
            try:
                with open(to_long_path(part), "rb") as f:
                    for block in iter(lambda: f.read(chunk_size), b""):
                        hasher.update(block)
            except OSError as e:
                _remove_quiet(part)
                raise FetchFailure(classify_local_error(e), str(e), host=host) from e
        written = resume_from if resumed else 0
        try:
            with open(to_long_path(part), "ab" if resumed else "wb") as f:
                for chunk in response.iter_content(chunk_size=chunk_size):
                    if not chunk:
                        continue
                    f.write(chunk)
                    written += len(chunk)
                    if hasher is not None:
                        hasher.update(chunk)
                    # «Зверь» по этому признаку видит, что файл действительно
                    # качается, даже если он большой и статус ещё не обновился.
                    if heartbeat is not None:
                        heartbeat()
                    # Ход скачивания в журнал: сколько уже записано и с какого
                    # сервера. Частоту строки ограничивает сам получатель.
                    if progress is not None:
                        progress(written, host)
        except requests.Timeout as e:
            # Сеть оборвалась — `.part` оставляем для докачки по Range.
            raise FetchFailure("timeout", f"нет данных {int(storage_timeout[1])} с", host=host) from e
        except requests.RequestException as e:
            # Важно раньше `OSError`: RequestException тоже наследник IOError,
            # иначе сетевой обрыв превратился бы в «локальный диск» и снёс
            # недокачанные байты.
            raise FetchFailure("connection", f"обрыв соединения: {e}", host=host) from e
        except OSError as e:
            # Локальная ошибка (место, права): недокачанное не годится.
            _remove_quiet(part)
            raise FetchFailure(classify_local_error(e), str(e), host=host) from e

        if expected_size is not None:
            if expected_size == 0 and written == 0:
                pass  # настоящий пустой файл
            elif written != expected_size:
                _remove_quiet(part)
                raise FetchFailure(
                    "bad_file",
                    f"размер не совпал: ожидали {expected_size}, получили {written}",
                    host=host,
                )

        actual_md5 = hasher.hexdigest() if hasher is not None else None
        if actual_md5 is not None and actual_md5.lower() != str(expected_md5).lower():
            _remove_quiet(part)
            raise FetchFailure("bad_file", "контрольная сумма не совпала", host=host)

        try:
            os.replace(to_long_path(part), to_long_path(dst))
        except OSError as e:
            _remove_quiet(part)
            raise FetchFailure(classify_local_error(e), str(e), host=host) from e

        return FetchResult(bytes_written=written, md5=actual_md5, host=host)
    finally:
        if response is not None:
            try:
                response.close()
            except Exception:  # noqa: BLE001 — закрытие не должно маскировать ошибку
                pass
        if own_session:
            s.close()
