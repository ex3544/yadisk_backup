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
import uuid
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urljoin, urlparse

import requests

from utils import classify_local_error, to_long_path

# Типы ошибок, при которых файл не «сбойный», а ждёт Яндекса.
DEFER_KINDS = frozenset(
    {"storage_down", "storage_skip", "server_50x", "rate_limit", "timeout", "connection"}
)

# Сколько секунд считаем сервер хранения мёртвым. За полчаса Яндекс обычно
# возвращает сервер в строй.
DEAD_HOST_TTL = 30 * 60.0

# Сколько подряд ответов 5xx терпим, прежде чем признать сервер хранения
# больным. Один-два 500 — это «файл сгорел»: тот же сервер отдаёт соседние
# файлы. Гасить весь проход из-за первого же такого ответа нельзя — именно
# из-за этого у тестера 29.10 из 1059 файлов было запрошено семь.
SERVER_ERROR_LIMIT = 10

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
    """

    def __init__(
        self,
        ttl: float = DEAD_HOST_TTL,
        clock=time.monotonic,
        server_error_limit: int = SERVER_ERROR_LIMIT,
    ) -> None:
        self._ttl = ttl
        self._clock = clock
        self._server_error_limit = max(1, int(server_error_limit))
        self._hosts: dict[str, float] = {}
        # Подряд идущие ответы 5xx на разных файлах: пока счётчик ниже порога,
        # хост живой, просто некоторые файлы он не отдаёт.
        self._errors: dict[str, int] = {}
        self._lock = threading.Lock()

    def mark(self, host: Optional[str]) -> None:
        if not host:
            return
        with self._lock:
            self._hosts[host] = self._clock() + self._ttl

    def is_dead(self, host: Optional[str]) -> bool:
        if not host:
            return False
        now = self._clock()
        with self._lock:
            until = self._hosts.get(host)
            if until is None:
                return False
            if until <= now:
                self._hosts.pop(host, None)
                return False
            return True

    def note_reply_error(self, host: Optional[str]) -> None:
        """Сервер ответил 5xx. Сам по себе такой ответ ничего не значит.

        Яндекс нередко отвечает 500 именно на конкретный файл (у него нет
        копии), продолжая отдавать остальные. Поэтому хост считается больным
        только после `server_error_limit` таких ответов на разных файлах подряд.
        """
        if not host:
            return
        with self._lock:
            n = self._errors.get(host, 0) + 1
            if n >= self._server_error_limit:
                self._errors.pop(host, None)
                self._hosts[host] = self._clock() + self._ttl
            else:
                self._errors[host] = n

    def note_ok(self, host: Optional[str]) -> None:
        """Файл скачался — сервер жив, счётчик сбоев сбрасываем."""
        if not host:
            return
        with self._lock:
            self._errors.pop(host, None)

    def error_count(self, host: Optional[str]) -> int:
        """Сколько 5xx подряд накопилось без успешных ответов (для журнала)."""
        if not host:
            return 0
        with self._lock:
            return self._errors.get(host, 0)

    def clear(self) -> None:
        with self._lock:
            self._hosts.clear()
            self._errors.clear()

    def snapshot(self) -> list[str]:
        """Живые (не истёкшие) мёртвые серверы — для журнала и диагностики."""
        now = self._clock()
        with self._lock:
            self._hosts = {h: t for h, t in self._hosts.items() if t > now}
            return sorted(self._hosts)


@dataclass
class FetchResult:
    bytes_written: int
    md5: Optional[str]
    host: Optional[str]


def part_path_for(dst: str) -> str:
    """Скрытый временный файл рядом с целевым.

    Не `<имя>.part`: на Диске вполне может лежать настоящий файл `видео.mp4.part`,
    и мы бы его затёрли. Плюс короткое имя не упирается в лимит длины пути.
    """
    parent = os.path.dirname(dst) or "."
    return os.path.join(parent, f".yadisk-{uuid.uuid4().hex[:16]}.part")


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
    link_timeout: tuple[float, float] = (30.0, 60.0),
    storage_timeout: tuple[float, float] = (10.0, 10.0),
    chunk_size: int = 1024 * 1024,
) -> FetchResult:
    """Скачивает ссылку в `dst` атомарно: временный файл → os.replace.

    `reject_pages=True` — для Безлимита и превью: если вместо файла пришёл
    HTML или JSON, это подмена (истёкшая сессия, капча, заглушка). У обычного
    Диска есть настоящие .html и .json, поэтому там проверка выключена.
    """
    own_session = session is None
    s = session or requests.Session()
    url = _absolute(link)
    host: Optional[str] = None
    response = None

    try:
        # Первый запрос — ссылка от API, редирект разбираем вручную: нужен host.
        for hop in range(5):
            hop_host = _host_of(url)
            if dead_hosts is not None and dead_hosts.is_dead(hop_host):
                raise FetchFailure(
                    "storage_skip",
                    "сервер хранения не отвечает — пропускаю без запроса",
                    host=hop_host,
                )
            try:
                response = s.get(
                    url,
                    stream=True,
                    allow_redirects=False,
                    timeout=link_timeout if hop == 0 else storage_timeout,
                    headers={"Accept": "*/*"},
                )
            except requests.Timeout as e:
                raise FetchFailure("timeout", f"таймаут соединения: {e}", host=hop_host) from e
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

        _check_status(response.status_code, host)

        if reject_pages:
            ctype = (response.headers.get("Content-Type") or "").lower()
            if "text/html" in ctype or "application/json" in ctype:
                raise FetchFailure(
                    "bad_file",
                    f"вместо файла пришла страница ({ctype.split(';')[0]})",
                    host=host,
                )

        part = part_path_for(dst)
        _remove_quiet(part)
        hasher = hashlib.md5(usedforsecurity=False) if is_md5(expected_md5) else None
        written = 0
        try:
            with open(to_long_path(part), "wb") as f:
                for chunk in response.iter_content(chunk_size=chunk_size):
                    if not chunk:
                        continue
                    f.write(chunk)
                    written += len(chunk)
                    if hasher is not None:
                        hasher.update(chunk)
        except OSError as e:
            _remove_quiet(part)
            raise FetchFailure(classify_local_error(e), str(e), host=host) from e
        except requests.Timeout as e:
            _remove_quiet(part)
            raise FetchFailure("timeout", f"обрыв по таймауту: {e}", host=host) from e
        except requests.RequestException as e:
            _remove_quiet(part)
            raise FetchFailure("connection", f"обрыв соединения: {e}", host=host) from e

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
