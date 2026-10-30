"""Обновления через GitHub Releases.

Проверка версии, а по запросу пользователя — загрузка установщика с проверкой
SHA-256. Сам по себе код ничего не запускает: установку запускает webui только
после явного подтверждения в интерфейсе.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

import paths
import version

API_URL = f"https://api.github.com/repos/{version.APP_REPO}/releases/latest"
MAX_RESPONSE_BYTES = 1_000_000
MAX_ASSET_BYTES = 400 * 1024 * 1024
DOWNLOAD_CHUNK = 256 * 1024
TIMEOUT = 8

# Сколько храним последнюю проверку без повтора: в отчёте диагностики старые
# данные допустимы, но не старше шести часов — иначе проверяем заново.
CHECK_TTL_SEC = 6 * 60 * 60
# Быстрая проверка для отчёта: ждать 8 секунд на вкладке «Диагностика» долго.
LIVE_TIMEOUT = 5

ASSET_URL_PREFIX = f"https://github.com/{version.APP_REPO}/releases/download/"

_INSTALLER_RE = re.compile(r"^YandexDiskBackup-(\d+(?:\.\d+)*)-setup\.exe$")
_PORTABLE_RE = re.compile(r"^YandexDiskBackup-(\d+(?:\.\d+)*)-portable\.zip$")
_SHA_RE = re.compile(r"^([0-9a-fA-F]{64})\b")

_VERSION_RE = re.compile(r"^v?(\d+(?:\.\d+)*)$")


def parse_version(value: str) -> tuple[int, ...] | None:
    """'v35.4.10' → (35, 4, 10); мусор → None."""
    match = _VERSION_RE.match((value or "").strip())
    if not match:
        return None
    return tuple(int(part) for part in match.group(1).split("."))


def is_newer(latest: tuple[int, ...], current: tuple[int, ...]) -> bool:
    """Сравнивает версии как числа, а не как строки (35.4.10 > 35.4.9)."""
    size = max(len(latest), len(current))
    latest = latest + (0,) * (size - len(latest))
    current = current + (0,) * (size - len(current))
    return latest > current


def _fetch_release(timeout: float | None = None) -> dict:
    request = urllib.request.Request(
        API_URL,
        headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": f"{version.APP_NAME}/{version.APP_VERSION}",
        },
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT if timeout is None else timeout) as response:
        raw = response.read(MAX_RESPONSE_BYTES + 1)
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ValueError("ответ GitHub слишком большой")
    return json.loads(raw.decode("utf-8"))


def release_url_allowed(url: str) -> bool:
    """Открываем только страницы релизов нашего репозитория."""
    prefix = f"https://github.com/{version.APP_REPO}/"
    return bool(version.APP_REPO) and str(url or "").startswith(prefix)


def asset_url_allowed(url: str) -> bool:
    """Скачиваем файлы только из раздела загрузок нашего репозитория."""
    return bool(version.APP_REPO) and str(url or "").startswith(ASSET_URL_PREFIX)


def release_assets(data: dict) -> dict:
    """Файлы релиза, у которых адрес ведёт в наш репозиторий."""
    out = {}
    for asset in data.get("assets") or []:
        if not isinstance(asset, dict):
            continue
        name = str(asset.get("name") or "")
        url = str(asset.get("browser_download_url") or "")
        if not name or not asset_url_allowed(url):
            continue
        if asset.get("state") and asset.get("state") != "uploaded":
            continue
        out[name] = {"name": name, "url": url,
                     "size": int(asset.get("size") or 0)}
    return out


def pick_assets(data: dict, latest: tuple[int, ...]):
    """Возвращает (setup.exe, portable.zip) именно этой версии."""
    assets = release_assets(data)
    setup = portable = None
    for name, asset in assets.items():
        match = _INSTALLER_RE.match(name)
        if match and parse_version(match.group(1)) == latest:
            setup = dict(asset)
            sha = assets.get(name + ".sha256")
            setup["sha256_url"] = sha["url"] if sha else ""
        match = _PORTABLE_RE.match(name)
        if match and parse_version(match.group(1)) == latest:
            portable = dict(asset)
            sha = assets.get(name + ".sha256")
            portable["sha256_url"] = sha["url"] if sha else ""
    return setup, portable


def _request(url: str) -> urllib.request.Request:
    return urllib.request.Request(
        url, headers={"User-Agent": f"{version.APP_NAME}/{version.APP_VERSION}"}
    )


def fetch_sha256(url: str) -> str:
    """Читает опубликованную контрольную сумму ('<hex>  <файл>')."""
    if not asset_url_allowed(url):
        raise ValueError("недопустимый адрес контрольной суммы")
    with urllib.request.urlopen(_request(url), timeout=TIMEOUT * 2) as response:
        raw = response.read(4096)
    match = _SHA_RE.match(raw.decode("utf-8", errors="replace").strip())
    if not match:
        raise ValueError("контрольная сумма в неизвестном формате")
    return match.group(1).lower()


def download_asset(url: str, dest, expected_sha256: str = "",
                   progress=None, cancel=None) -> str:
    """Скачивает файл релиза во временный путь и сверяет SHA-256.

    progress(done_bytes, total_bytes) вызывается по ходу загрузки.
    Возвращает фактический хеш. При любой ошибке .part-файл удаляется.
    """
    if not asset_url_allowed(url):
        raise ValueError("недопустимый адрес загрузки")
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    digest = hashlib.sha256()
    done = 0
    try:
        with urllib.request.urlopen(_request(url), timeout=60) as response:
            total = int(response.headers.get("Content-Length") or 0)
            if total > MAX_ASSET_BYTES:
                raise ValueError("файл обновления слишком большой")
            with part.open("wb") as handle:
                while True:
                    if cancel is not None and cancel.is_set():
                        raise RuntimeError("загрузка отменена")
                    chunk = response.read(DOWNLOAD_CHUNK)
                    if not chunk:
                        break
                    done += len(chunk)
                    if done > MAX_ASSET_BYTES:
                        raise ValueError("файл обновления слишком большой")
                    handle.write(chunk)
                    digest.update(chunk)
                    if progress is not None:
                        progress(done, total)
        actual = digest.hexdigest()
        if expected_sha256 and actual != expected_sha256.lower():
            raise ValueError("контрольная сумма не совпала — файл не установлен")
        os.replace(part, dest)
        return actual
    except Exception:
        try:
            part.unlink()
        except OSError:
            pass
        raise


def check_for_updates(timeout: float | None = None) -> dict:
    """Возвращает статус проверки. Сетевые ошибки не считаются сбоем программы."""
    current_raw = version.APP_VERSION
    result = {
        "ok": False,
        "current": current_raw,
        "latest": "",
        "update_available": False,
        "url": f"{version.APP_REPO_URL}/releases",
        "name": "",
        "published_at": "",
        "notes": "",
        "error": "",
        "installer": None,
        "portable": None,
    }

    current = parse_version(current_raw)
    if current is None:
        result["error"] = "Не удалось разобрать текущую версию."
        return result
    if not version.APP_REPO:
        result["error"] = "Репозиторий обновлений не настроен."
        return result

    try:
        data = _fetch_release(timeout)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            result["error"] = "Релизы ещё не опубликованы."
        else:
            result["error"] = f"GitHub ответил HTTP {exc.code}."
        return result
    except (urllib.error.URLError, TimeoutError):
        result["error"] = "Не удалось связаться с GitHub. Проверьте интернет."
        return result
    except (ValueError, json.JSONDecodeError):
        result["error"] = "GitHub вернул неожиданный ответ."
        return result

    if not isinstance(data, dict):
        result["error"] = "GitHub вернул неожиданный ответ."
        return result

    tag = str(data.get("tag_name") or "")
    latest = parse_version(tag)
    if latest is None:
        result["error"] = "У последнего релиза нечисловой тег версии."
        return result

    url = str(data.get("html_url") or result["url"])
    if not release_url_allowed(url):
        url = result["url"]

    notes = str(data.get("body") or "")
    installer, portable = pick_assets(data, latest)
    result.update(
        ok=True,
        installer=installer,
        portable=portable,
        latest=".".join(str(part) for part in latest),
        update_available=is_newer(latest, current),
        url=url,
        name=str(data.get("name") or ""),
        published_at=str(data.get("published_at") or ""),
        notes=notes[:2000],
    )
    return result


# ── Последняя проверка: для окна и отчёта диагностики ────────────────────────


def record_check(result: dict) -> None:
    """Сохраняет результат проверки, чтобы отчёт не ждал сеть.

    В файл попадает всё, что видит пользователь: версия, дата релиза и текст
    «что нового». Секретов здесь нет — это публичные данные GitHub.
    """
    data = {
        "checked_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "checked_ts": time.time(),
        "ok": bool(result.get("ok")),
        "current": str(result.get("current") or version.APP_VERSION),
        "latest": str(result.get("latest") or ""),
        "update_available": bool(result.get("update_available")),
        "url": str(result.get("url") or ""),
        "name": str(result.get("name") or ""),
        "published_at": str(result.get("published_at") or ""),
        # Описание релиза бывает длинным: в отчёте нужен текст, а не весь JSON.
        "notes": str(result.get("notes") or "")[:4000],
        "error": str(result.get("error") or ""),
    }
    try:
        paths.UPDATES_STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = paths.UPDATES_STATUS_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(tmp, paths.UPDATES_STATUS_FILE)
    except Exception:  # noqa: BLE001 — проверка не должна падать из-за записи
        pass


def last_check() -> dict:
    """Последняя сохранённая проверка. Пустой словарь — проверок ещё не было."""
    try:
        data = json.loads(paths.UPDATES_STATUS_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — файла может не быть или он битый
        return {}
    return data if isinstance(data, dict) else {}


def check_is_stale(last: dict, max_age: float = CHECK_TTL_SEC) -> bool:
    if not last:
        return True
    try:
        return time.time() - float(last.get("checked_ts") or 0) > max_age
    except (TypeError, ValueError):
        return True


def updates_snapshot(download: dict | None = None, live: bool = True,
                     max_age: float = CHECK_TTL_SEC) -> dict:
    """Блок «обновления» для отчёта диагностики.

    `live=True` — если сохранённой проверки нет или она старая, сходить на
    GitHub с коротким таймаутом. Ошибка сети не мешает отчёту: в блоке просто
    останется последняя известная проверка и текст ошибки.
    """
    last = last_check()
    checked_now = False
    if live and check_is_stale(last, max_age):
        try:
            result = check_for_updates(timeout=LIVE_TIMEOUT)
        except Exception as e:  # noqa: BLE001 — отчёт собирается в любом случае
            result = {"ok": False, "current": version.APP_VERSION,
                      "error": f"{type(e).__name__}: {e}"}
        record_check(result)
        last = last_check()
        checked_now = True
    return {
        "current": version.APP_VERSION,
        "last_check": last or None,
        "checked_now": checked_now,
        # Состояние загрузки обновления (idle/checking/downloading/ready/…).
        "download": dict(download or {}),
    }
