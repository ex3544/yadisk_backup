"""Полуавтоматическое обновление cookies.json через Playwright.

Запускает видимое окно браузера (Chrome, если установлен, иначе встроенный
Chromium), открывает disk.yandex.ru и ждёт, пока пользователь войдёт в аккаунт
Яндекса. Как только появляются Session_id / yandexuid / sessionid2, снимает все
куки Яндекса и сохраняет их в cookies.json в формате Cookie-Editor.

Запускается из веб-интерфейса (webui.py) как фоновый процесс. Пишет свой статус
в logs/cookies_fetch_status.json, чтобы UI мог показывать прогресс и результат.
"""

from __future__ import annotations

import json
import os
import sys
import time

import paths

COOKIES_FILE = paths.COOKIES_FILE
STATUS_FILE = paths.COOKIES_FETCH_STATUS

REQUIRED = ("Session_id", "yandexuid", "sessionid2")
TIMEOUT_SEC = 15 * 60  # сколько ждём входа пользователя


def write_status(state: str, msg: str = "", **extra) -> None:
    STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    data = {"state": state, "msg": msg, "updated_at": time.time(), **extra}
    tmp = STATUS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, STATUS_FILE)


def save_cookies(cookies: list[dict]) -> int:
    """Сохраняет куки Яндекса в cookies.json (атомарно). Возвращает число кук."""
    yandex = [c for c in cookies if "yandex" in (c.get("domain") or "")]
    out = []
    for c in yandex:
        expires = c.get("expires", -1) or -1
        out.append({
            "name": c.get("name"),
            "value": c.get("value"),
            "domain": c.get("domain"),
            "path": c.get("path", "/"),
            "expirationDate": None if expires < 0 else expires,
            "hostOnly": not (c.get("domain") or "").startswith("."),
            "httpOnly": bool(c.get("httpOnly")),
            "secure": bool(c.get("secure")),
            "sameSite": c.get("sameSite", "no_restriction"),
        })
    tmp = COOKIES_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, COOKIES_FILE)
    return len(out)


def main() -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        write_status(
            "error",
            "Playwright не установлен. Выполни: python -m pip install playwright "
            "&& python -m playwright install chromium",
        )
        print("[!] Playwright не установлен", flush=True)
        return 1

    write_status("running", "Запускаю браузер…")
    print("[*] Запускаю браузер…", flush=True)

    try:
        with sync_playwright() as p:
            launch_args = {
                "headless": False,
                "args": ["--disable-blink-features=AutomationControlled", "--no-first-run"],
            }
            browser = None
            # Предпочитаем установленный Chrome — у него «настоящий» отпечаток,
            # меньше шансов нарваться на капчу Яндекса. Если нет — встроенный Chromium.
            try:
                browser = p.chromium.launch(channel="chrome", **launch_args)
            except Exception as e:
                print(f"[i] Установленный Chrome не найден ({e}), беру встроенный Chromium", flush=True)
                browser = p.chromium.launch(**launch_args)

            try:
                context = browser.new_context()
                page = context.new_page()
                write_status("running", "Открываю disk.yandex.ru…")
                print("[*] Открываю disk.yandex.ru…", flush=True)
                try:
                    page.goto(
                        "https://disk.yandex.ru/client/disk",
                        wait_until="domcontentloaded",
                        timeout=60000,
                    )
                except Exception as e:
                    print(f"[i] Предупреждение при загрузке страницы: {e}", flush=True)

                deadline = time.time() + TIMEOUT_SEC
                i = 0
                while time.time() < deadline:
                    if page.is_closed():
                        write_status("cancelled", "Окно браузера закрыто до входа")
                        print("[!] Окно закрыто, отменяю", flush=True)
                        return 2
                    try:
                        cookies = context.cookies()
                    except Exception:
                        write_status("cancelled", "Браузер закрыт до завершения входа")
                        return 2

                    m = {c["name"]: c["value"] for c in cookies if c.get("value")}
                    missing = [n for n in REQUIRED if not m.get(n)]
                    if not missing:
                        saved = save_cookies(cookies)
                        write_status("done", f"Куки обновлены ({saved} шт)", saved_count=saved)
                        print(f"[+] Готово: сохранено {saved} кук Яндекса", flush=True)
                        return 0

                    i += 1
                    if i % 5 == 1:  # обновляем статус раз в ~10 сек
                        write_status("running", f"Жду входа (нет: {', '.join(missing)})")
                    time.sleep(2)

                write_status("timeout", "Время ожидания входа истекло")
                print("[!] Таймаут: вход не выполнен", flush=True)
                return 3
            finally:
                try:
                    context.close()
                except Exception:
                    pass
                try:
                    browser.close()
                except Exception:
                    pass
    except Exception as e:
        write_status("error", f"{type(e).__name__}: {e}")
        print(f"[!] Ошибка: {type(e).__name__}: {e}", flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
