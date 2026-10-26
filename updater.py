"""Проверка обновлений через GitHub Releases.

Приложение не устанавливает обновления само: оно сравнивает свою версию с
последним релизом и даёт пользователю ссылку на страницу загрузки. Так мы не
выполняем неподписанный код автоматически.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request

import version

API_URL = f"https://api.github.com/repos/{version.APP_REPO}/releases/latest"
MAX_RESPONSE_BYTES = 1_000_000
TIMEOUT = 8

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


def _fetch_release() -> dict:
    request = urllib.request.Request(
        API_URL,
        headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": f"{version.APP_NAME}/{version.APP_VERSION}",
        },
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        raw = response.read(MAX_RESPONSE_BYTES + 1)
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ValueError("ответ GitHub слишком большой")
    return json.loads(raw.decode("utf-8"))


def release_url_allowed(url: str) -> bool:
    """Открываем только страницы релизов нашего репозитория."""
    prefix = f"https://github.com/{version.APP_REPO}/"
    return bool(version.APP_REPO) and str(url or "").startswith(prefix)


def check_for_updates() -> dict:
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
    }

    current = parse_version(current_raw)
    if current is None:
        result["error"] = "Не удалось разобрать текущую версию."
        return result
    if not version.APP_REPO:
        result["error"] = "Репозиторий обновлений не настроен."
        return result

    try:
        data = _fetch_release()
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
    result.update(
        ok=True,
        latest=".".join(str(part) for part in latest),
        update_available=is_newer(latest, current),
        url=url,
        name=str(data.get("name") or ""),
        published_at=str(data.get("published_at") or ""),
        notes=notes[:2000],
    )
    return result
