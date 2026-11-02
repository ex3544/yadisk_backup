"""Полуавтоматическое получение OAuth-токена Яндекс.Диска через Playwright.

Открывает видимое окно браузера на странице «Полигон» Яндекс.Диска, ждёт, пока
пользователь войдёт в аккаунт, нажимает «Получить OAuth-токен» и забирает
готовый токен со страницы. Токен проверяется через официальное API и
сохраняется в `.env` как `YA_DISK_TOKEN`.

Запускается из веб-интерфейса (webui.py) как фоновый процесс. Пишет свой статус
в logs/token_fetch_status.json. Сам токен в файл статуса не попадает.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time

import paths

ENV_FILE = paths.ENV_FILE
STATUS_FILE = paths.TOKEN_FETCH_STATUS

REQUIRED = ("Session_id", "yandexuid", "sessionid2")
TOKEN_RE = re.compile(r"y0_[A-Za-z0-9_-]{30,}")
GET_BUTTON_RE = re.compile(r"Получить\s+OAuth-?токен|Получить\s+токен", re.I)
TIMEOUT_SEC = 15 * 60  # сколько ждём входа и нажатия кнопки


def write_status(state: str, msg: str = "", **extra) -> None:
    STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    data = {"state": state, "msg": msg, "updated_at": time.time(), **extra}
    tmp = STATUS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, STATUS_FILE)


def save_token(token: str) -> None:
    """Сохраняет токен в .env (атомарно), не трогая остальные строки."""
    from dotenv import set_key

    ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not ENV_FILE.exists():
        ENV_FILE.write_text("", encoding="utf-8")
    set_key(str(ENV_FILE), "YA_DISK_TOKEN", token, quote_mode="never")


def check_token(token: str) -> dict:
    """Проверяет токен через API и возвращает логин аккаунта."""
    try:
        import requests
        response = requests.get(
            "https://cloud-api.yandex.net/v1/disk/",
            headers={"Authorization": f"OAuth {token}"},
            timeout=(10, 30),
        )
        if response.status_code != 200:
            return {"ok": False, "msg": f"Яндекс ответил кодом {response.status_code}"}
        data = response.json()
        login = ((data.get("user") or {}).get("login") or "").strip()
        return {"ok": True, "msg": "OK", "login": login}
    except Exception as e:  # noqa: BLE001 - показываем причину в статусе
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


def _found_in_text(text: str) -> str:
    match = TOKEN_RE.search(text or "")
    return match.group(0) if match else ""


def _scan_page(page) -> str:
    """Ищет токен в полях, тексте страницы, адресе и хранилищах браузера."""
    try:
        if page.is_closed():
            return ""
    except Exception:
        return ""

    try:
        for element in page.query_selector_all("input, textarea"):
            try:
                value = element.input_value()
            except Exception:
                continue
            found = _found_in_text(value)
            if found:
                return found
    except Exception:
        pass

    for getter in (
        lambda: page.url,
        lambda: page.content(),
        lambda: page.evaluate(
            "() => [...Object.values(localStorage), ...Object.values(sessionStorage)].join('\\n')"
        ),
    ):
        try:
            found = _found_in_text(getter() or "")
        except Exception:
            found = ""
        if found:
            return found
    return ""


def _click_get_button(page) -> bool:
    """Нажимает «Получить OAuth-токен». Возвращает False, если кнопки нет."""
    try:
        locator = page.get_by_role("button", name=GET_BUTTON_RE)
        if locator.count():
            locator.first.click(timeout=5000)
            return True
    except Exception:
        pass
    try:
        locator = page.get_by_text(GET_BUTTON_RE)
        if locator.count():
            locator.first.click(timeout=5000)
            return True
    except Exception:
        pass
    # Последняя попытка: ищем кнопку/ссылку с нужным текстом вручную.
    try:
        for element in page.query_selector_all("button, a"):
            try:
                text = (element.inner_text() or "").strip()
            except Exception:
                continue
            if GET_BUTTON_RE.search(text):
                element.click(timeout=5000)
                return True
    except Exception:
        pass
    return False


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
            try:
                browser = p.chromium.launch(channel="chrome", **launch_args)
            except Exception as e:
                print(f"[i] Установленный Chrome не найден ({e}), беру встроенный Chromium", flush=True)
                browser = p.chromium.launch(**launch_args)

            try:
                context = browser.new_context()
                page = context.new_page()
                write_status("running", "Открываю страницу «Полигон»…")
                print("[*] Открываю yandex.ru/dev/disk/poligon…", flush=True)
                try:
                    page.goto(
                        "https://yandex.ru/dev/disk/poligon/",
                        wait_until="domcontentloaded",
                        timeout=60000,
                    )
                except Exception as e:
                    print(f"[i] Предупреждение при загрузке страницы: {e}", flush=True)

                deadline = time.time() + TIMEOUT_SEC
                tick = 0
                clicked = False
                reloaded = False
                while time.time() < deadline:
                    pages = [pg for pg in context.pages if not pg.is_closed()]
                    if not pages:
                        write_status("cancelled", "Окно браузера закрыто")
                        print("[!] Окно закрыто, отменяю", flush=True)
                        return 2

                    for pg in pages:
                        found = _scan_page(pg)
                        if found:
                            break
                    if found:
                        write_status("running", "Токен получен, проверяю…")
                        check = check_token(found)
                        if not check.get("ok"):
                            write_status("error", check.get("msg") or "Токен не принят")
                            print(f"[!] Токен не принят: {check.get('msg')}", flush=True)
                            return 1
                        try:
                            save_token(found)
                        except Exception as e:  # noqa: BLE001
                            write_status("error", f"Не удалось сохранить токен: {e}")
                            print(f"[!] Не удалось сохранить токен: {e}", flush=True)
                            return 1
                        login = check.get("login") or ""
                        msg = f"Токен получен и сохранён{f' ({login})' if login else ''}"
                        write_status("done", msg, login=login)
                        print(f"[+] {msg}", flush=True)
                        return 0

                    try:
                        cookies = context.cookies()
                    except Exception:
                        write_status("cancelled", "Браузер закрыт до завершения входа")
                        return 2
                    names = {c.get("name") for c in cookies if c.get("value")}
                    logged_in = all(name in names for name in REQUIRED)

                    if logged_in and not reloaded:
                        # После входа страница «Полигона» могла остаться на форме
                        # логина — перезагружаем один раз, чтобы появилась кнопка.
                        reloaded = True
                        try:
                            page.reload(wait_until="domcontentloaded", timeout=60000)
                        except Exception:
                            pass

                    if logged_in and not clicked:
                        if _click_get_button(page):
                            clicked = True
                            write_status("running", "Нажал «Получить OAuth-токен», жду токен…")
                            print("[*] Нажал «Получить OAuth-токен»", flush=True)

                    tick += 1
                    if tick % 5 == 1:  # раз в ~10 секунд
                        if not logged_in:
                            write_status("running", "Жду входа в аккаунт Яндекса…")
                        elif not clicked:
                            write_status(
                                "running",
                                "Вход выполнен. Нажмите «Получить OAuth-токен» в окне браузера.",
                            )
                        else:
                            write_status("running", "Жду, пока страница покажет токен…")
                    time.sleep(2)

                write_status("timeout", "Время ожидания истекло")
                print("[!] Таймаут: токен не получен", flush=True)
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
