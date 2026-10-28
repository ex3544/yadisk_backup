"""Оконная обёртка приложения: локальный сервер, окно WebView2 и трей.

Это точка входа собранного exe. В обычном режиме launcher:

1. поднимает FastAPI/uvicorn на `127.0.0.1` (порт 8000 или следующий свободный);
2. открывает окно pywebview с Edge WebView2;
3. держит значок в системном трее;
4. при закрытии окна сворачивается в трей, а не завершает работу.

Тот же exe работает как воркер: `<exe> main.py scan` и т.п. Разбор argv
делает `webui.dispatch_worker()` ещё до загрузки GUI-библиотек.
"""

from __future__ import annotations

import ctypes
import logging
import os
import socket
import sys
import threading
import time
import webbrowser

import paths


def _setup_console_streams() -> None:
    """В windowed-сборке sys.stdout/stderr = None.

    Некоторые библиотеки пишут в них при импорте и падают. Направляем вывод
    в файл, чтобы приложение не умирало молча. В дочернем воркере stdout —
    это pipe, поэтому ничего не подменяем.
    """
    if sys.stdout is not None and sys.stderr is not None:
        return
    try:
        paths.LOG_DIR.mkdir(parents=True, exist_ok=True)
        stream = open(paths.LAUNCHER_OUTPUT_LOG, "a", encoding="utf-8", buffering=1)
    except Exception:
        return
    if sys.stdout is None:
        sys.stdout = stream
    if sys.stderr is None:
        sys.stderr = stream


_setup_console_streams()

import requests  # noqa: E402  (после подмены потоков)

import version  # noqa: E402
import webui  # noqa: E402

APP_TITLE = f"{version.APP_NAME} {version.APP_VERSION}"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
PORT_TRIES = 12
WINDOW_WIDTH = 1280
WINDOW_HEIGHT = 880
WINDOW_MIN_SIZE = (900, 600)

log = logging.getLogger("launcher")


# ── Настройки и сеть ────────────────────────────────────────────────────────


def _env_host() -> str:
    host = (os.getenv("WEBUI_HOST") or "").strip()
    return host or DEFAULT_HOST


def _env_port() -> int:
    raw = (os.getenv("WEBUI_PORT") or "").strip()
    if not raw:
        return DEFAULT_PORT
    try:
        port = int(raw)
    except ValueError:
        return DEFAULT_PORT
    return port if 0 < port < 65536 else DEFAULT_PORT


def _url_host(host: str) -> str:
    """Хост, по которому к серверу подключается браузер/окно."""
    host = (host or "").strip()
    if host in ("", "0.0.0.0", "*"):
        return "127.0.0.1"
    if host == "::":
        return "[::1]"
    if ":" in host and not host.startswith("["):
        return f"[{host}]"
    return host


def _bind_host(host: str) -> str:
    host = (host or "").strip().strip("[]")
    return host or "127.0.0.1"


def base_url(host: str, port: int) -> str:
    return f"http://{_url_host(host)}:{port}"


def is_app_running(url: str, timeout: float = 0.8) -> bool:
    """True, если по адресу уже отвечает именно наш /api/version."""
    try:
        response = requests.get(f"{url}/api/version", timeout=timeout)
        data = response.json()
    except Exception:
        return False
    return response.status_code == 200 and data.get("name") == version.APP_NAME


def port_free(host: str, port: int) -> bool:
    """Проверяет возможность занять порт (без гонки с уже запущенным сервером)."""
    try:
        infos = socket.getaddrinfo(
            _bind_host(host), port, type=socket.SOCK_STREAM
        )
    except OSError:
        return False
    bound = 0
    for family, socktype, proto, _canon, sockaddr in infos:
        try:
            with socket.socket(family, socktype, proto) as sock:
                sock.bind(sockaddr)
            bound += 1
        except OSError:
            # Хотя бы один адрес занят — порт нельзя считать свободным.
            return False
    return bound > 0


def choose_port(host: str, start: int, tries: int = PORT_TRIES) -> tuple[int, str | None]:
    """Возвращает (порт, url_уже_запущенного_приложения_или_None)."""
    for port in range(max(1, start), max(1, start) + tries):
        url = base_url(host, port)
        if is_app_running(url):
            return port, url
        if port_free(host, port):
            return port, None
    raise RuntimeError(f"нет свободного порта в диапазоне {start}–{start + tries - 1}")


# ── Сервер ──────────────────────────────────────────────────────────────────


class ServerHandle:
    """uvicorn в фоновом потоке без перехвата Ctrl+C главного потока."""

    def __init__(self, host: str, port: int):
        import uvicorn

        config = uvicorn.Config(
            webui.app,
            host=host,
            port=port,
            log_level="warning",
            log_config=None,  # в windowed-сборке нет консоли для логов uvicorn
            access_log=False,
        )
        self.server = uvicorn.Server(config)
        # Signal handlers можно ставить только в главном потоке.
        self.server.install_signal_handlers = lambda: None
        self.thread = threading.Thread(
            target=self.server.run, name="webui-server", daemon=True
        )
        self.url = base_url(host, port)

    def start(self) -> None:
        self.thread.start()

    def wait_ready(self, timeout: float = 40.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.server.started and is_app_running(self.url, timeout=0.5):
                return True
            if not self.thread.is_alive():
                return False
            time.sleep(0.15)
        return False

    def stop(self) -> None:
        self.server.should_exit = True
        if self.thread.is_alive():
            self.thread.join(timeout=10)


# ── Окно и трей ─────────────────────────────────────────────────────────────


def _message_box(text: str, flags: int = 0x40) -> None:
    """Информационное/ошибочное окно Windows. На других ОС — stderr."""
    log.info("message: %s", text)
    if os.name == "nt":
        try:
            ctypes.windll.user32.MessageBoxW(None, text, APP_TITLE, flags)
            return
        except Exception:
            pass
    try:
        print(text, file=sys.stderr)
    except Exception:
        pass


def _draw_tray_image(size: int = 64):
    """Запасная иконка, если assets/tray.png не попал в сборку."""
    from PIL import Image, ImageDraw

    scale = 4
    s = size * scale
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    for y in range(s):
        t = y / max(1, s - 1)
        color = (
            int(37 + (29 - 37) * t),
            int(99 + (78 - 99) * t),
            int(235 + (216 - 235) * t),
            255,
        )
        draw.line([(0, y), (s, y)], fill=color)
    mask = Image.new("L", (s, s), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, s - 1, s - 1], radius=int(s * 0.22), fill=255
    )
    img.putalpha(mask)
    # Белая «туча» и жёлтая стрелка вниз — читаемо даже в 16 px.
    white = (255, 255, 255, 255)
    for cx, cy, r in ((0.36, 0.44, 0.15), (0.50, 0.37, 0.20), (0.64, 0.45, 0.15)):
        draw.ellipse(
            [s * (cx - r), s * (cy - r), s * (cx + r), s * (cy + r)], fill=white
        )
    draw.rounded_rectangle(
        [s * 0.22, s * 0.44, s * 0.78, s * 0.58], radius=int(s * 0.05), fill=white
    )
    yellow = (255, 204, 0, 255)
    draw.rectangle([s * 0.45, s * 0.58, s * 0.55, s * 0.74], fill=yellow)
    draw.polygon(
        [(s * 0.36, s * 0.70), (s * 0.64, s * 0.70), (s * 0.50, s * 0.86)],
        fill=yellow,
    )
    return img.resize((size, size), Image.LANCZOS)


def tray_image():
    try:
        from PIL import Image
    except Exception:
        return None
    for name in ("tray.png", "app.png", "app.ico"):
        path = paths.ASSETS_DIR / name
        if not path.exists():
            continue
        try:
            with Image.open(path) as img:
                return img.convert("RGBA").copy()
        except Exception:
            continue
    try:
        return _draw_tray_image(64)
    except Exception:
        return None


def _patch_edgechromium_context_menu(edgechromium) -> bool:
    """Включает стандартное контекстное меню WebView2.

    pywebview по умолчанию отключает его в не-debug режиме, из-за чего в
    полях ввода не работают «Копировать/Вставить» по правой кнопке.
    Патч точечный: включаем только меню, devtools и горячие клавиши браузера
    остаются выключенными.
    """
    original = edgechromium.EdgeChrome.on_webview_ready
    if getattr(original, "_yadisk_context_menu_patch", False):
        return True

    def on_webview_ready(self, sender, args):
        original(self, sender, args)
        if getattr(args, "IsSuccess", False):
            try:
                sender.CoreWebView2.Settings.AreDefaultContextMenusEnabled = True
                log.info("Контекстное меню WebView2 включено")
            except Exception:
                log.debug("Не удалось включить контекстное меню WebView2", exc_info=True)

    on_webview_ready._yadisk_context_menu_patch = True
    edgechromium.EdgeChrome.on_webview_ready = on_webview_ready
    return True


def enable_webview_context_menu() -> bool:
    """Применяет патч контекстного меню, если доступен Edge WebView2."""
    if os.name != "nt":
        return False
    try:
        from webview.platforms import edgechromium
    except Exception:
        log.debug("Edge WebView2 недоступен — контекстное меню не включено", exc_info=True)
        return False
    return _patch_edgechromium_context_menu(edgechromium)


# ── Оформление окна ─────────────────────────────────────────────────────────

# Заголовок окна: pywebview красит его по теме Windows, и на светлой теме над
# чёрным интерфейсом висит белая полоса. Перекрашиваем принудительно.
_DWMWA_USE_IMMERSIVE_DARK_MODE = 20
_DWMWA_USE_IMMERSIVE_DARK_MODE_OLD = 19
_DWMWA_BORDER_COLOR = 34
_DWMWA_CAPTION_COLOR = 35
_CAPTION_COLOR = 0x000A0A0D  # COLORREF 0x00BBGGRR — фон приложения #0a0a0d
_BORDER_COLOR = 0x002C2C2E  # цвет карточек #2c2c2e


def _darken_window_frame(window) -> None:
    """Тёмный заголовок и рамка окна, в цвет интерфейса.

    Вызывается по событию before_show, пока окно ещё не показано: если сделать
    это позже, Windows 10 вызов принимает (возвращает 0), но заголовок так и
    остаётся белым. Windows 10 умеет только чёрный заголовок, точный цвет
    задать нельзя; Windows 11 понимает и цвет фона, и цвет рамки.
    """
    if os.name != "nt":
        return
    try:
        native = getattr(window, "native", None)
        hwnd = int(native.Handle.ToInt32()) if native is not None else 0
        if not hwnd:
            return
        dwm = ctypes.windll.dwmapi
        dwm.DwmSetWindowAttribute.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint,
            ctypes.c_void_p,
            ctypes.c_uint,
        ]
        handle = ctypes.c_void_p(hwnd)
        on = ctypes.c_int(1)
        res = dwm.DwmSetWindowAttribute(
            handle, _DWMWA_USE_IMMERSIVE_DARK_MODE, ctypes.byref(on), ctypes.sizeof(on)
        )
        if res != 0:  # Windows 10 до 20H1 знает только старый номер атрибута
            dwm.DwmSetWindowAttribute(
                handle,
                _DWMWA_USE_IMMERSIVE_DARK_MODE_OLD,
                ctypes.byref(on),
                ctypes.sizeof(on),
            )
        caption = ctypes.c_uint(_CAPTION_COLOR)
        dwm.DwmSetWindowAttribute(
            handle, _DWMWA_CAPTION_COLOR, ctypes.byref(caption), ctypes.sizeof(caption)
        )
        border = ctypes.c_uint(_BORDER_COLOR)
        dwm.DwmSetWindowAttribute(
            handle, _DWMWA_BORDER_COLOR, ctypes.byref(border), ctypes.sizeof(border)
        )
    except Exception:
        log.debug("Не удалось перекрасить заголовок окна", exc_info=True)


class Application:
    """Связывает окно, трей и сервер; владеет корректным завершением."""

    def __init__(self, host: str, port: int, server: ServerHandle):
        self.host = host
        self.port = port
        self.server = server
        self.url = base_url(host, port)
        self.window = None
        self.icon = None
        self.tray_ok = False
        self._quitting = False
        self._shutdown_done = False
        self._shutdown_lock = threading.Lock()
        self._tray_hint_shown = False
        # Кнопка «Обновить» ставит обновление после закрытия приложения.
        webui.register_shutdown_hook(self.quit_app)

    # -- запуск ------------------------------------------------------------

    def run(self) -> None:
        self._start_tray()
        try:
            self._run_window()
        except Exception:
            log.exception("Окно WebView2 недоступно")
            self._run_browser_fallback()
        finally:
            self._shutdown()

    def _run_window(self) -> None:
        import webview

        enable_webview_context_menu()
        self.window = webview.create_window(
            APP_TITLE,
            self.url,
            width=WINDOW_WIDTH,
            height=WINDOW_HEIGHT,
            min_size=WINDOW_MIN_SIZE,
            background_color="#0b1220",
            text_select=True,
        )
        self.window.events.closing += self._on_closing
        # Тёмный заголовок окна: своя тема pywebview включается только при
        # тёмной теме Windows, а интерфейс у нас чёрный всегда. Именно
        # before_show — после показа заголовок перекрасить уже нельзя.
        self.window.events.before_show += lambda: _darken_window_frame(self.window)
        icon_path = paths.ASSETS_DIR / "app.ico"
        webview.start(
            gui="edgechromium" if os.name == "nt" else None,
            private_mode=False,
            storage_path=str(paths.DATA_DIR / "webview"),
            icon=str(icon_path) if icon_path.exists() else None,
        )

    def _run_browser_fallback(self) -> None:
        """Если WebView2 не поднялся — работаем в браузере, трей остаётся."""
        if not self.tray_ok:
            _message_box(
                "Не удалось открыть окно приложения и значок в трее.\n"
                "Запустите приложение ещё раз или сообщите разработчику.",
                flags=0x10,
            )
            return
        _message_box(
            "Не удалось открыть встроенное окно.\n"
            "Приложение открыто в браузере, управление — через значок в трее."
        )
        webbrowser.open(self.url)
        while not self._quitting and self.server.thread.is_alive():
            time.sleep(0.4)

    # -- трей --------------------------------------------------------------

    def _start_tray(self) -> None:
        try:
            import pystray

            image = tray_image()
            if image is None:
                raise RuntimeError("нет изображения для трея")
            menu = pystray.Menu(
                pystray.MenuItem("Открыть окно", self.show_window, default=True),
                pystray.MenuItem("Открыть в браузере", self.open_browser),
                pystray.MenuItem("Папка данных", self.open_data_dir),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Выход", self.quit_app),
            )
            self.icon = pystray.Icon(
                "yadisk_backup", image, APP_TITLE, menu=menu
            )
            self.icon.run_detached()
            self.tray_ok = True
            log.info("Трей запущен")
        except Exception:
            self.tray_ok = False
            self.icon = None
            log.exception("Трей недоступен")

    def _notify(self, title: str, message: str) -> None:
        if not self.icon:
            return
        try:
            self.icon.notify(message, title)
        except Exception:
            pass

    def show_window(self, icon=None, item=None) -> None:
        if not self.window:
            return
        try:
            self.window.restore()
        except Exception:
            pass
        try:
            self.window.show()
        except Exception:
            log.exception("Не удалось показать окно")
            return
        try:
            self.window.on_top = True
            self.window.on_top = False
        except Exception:
            pass

    def open_browser(self, icon=None, item=None) -> None:
        webbrowser.open(self.url)

    def open_data_dir(self, icon=None, item=None) -> None:
        try:
            if os.name == "nt":
                os.startfile(str(paths.DATA_DIR))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                import subprocess

                subprocess.Popen(["open", str(paths.DATA_DIR)])
            else:
                import subprocess

                subprocess.Popen(["xdg-open", str(paths.DATA_DIR)])
        except Exception:
            log.exception("Не удалось открыть папку данных")

    def quit_app(self, icon=None, item=None) -> None:
        threading.Thread(
            target=self._shutdown, name="app-shutdown", daemon=True
        ).start()

    # -- завершение --------------------------------------------------------

    def _on_closing(self):
        """Закрытие окна сворачивает в трей; без трея — завершает работу."""
        if self._quitting:
            return True
        if not self.tray_ok:
            return True
        try:
            self.window.hide()
        except Exception:
            log.exception("Не удалось скрыть окно")
            return True
        if not self._tray_hint_shown:
            self._tray_hint_shown = True
            self._notify(
                APP_TITLE,
                "Приложение продолжает работать. Выход — через значок в трее.",
            )
        return False

    def _shutdown(self) -> None:
        with self._shutdown_lock:
            if self._shutdown_done:
                return
            self._shutdown_done = True
        self._quitting = True
        log.info("Завершение приложения")
        try:
            stopped = webui.tm.stop_all()
            if stopped:
                log.info("Остановлено задач: %s", stopped)
        except Exception:
            log.exception("Ошибка остановки задач")
        try:
            self.server.stop()
        except Exception:
            log.exception("Ошибка остановки сервера")
        if self.icon is not None:
            try:
                self.icon.stop()
            except Exception:
                pass
            self.icon = None
        if self.window is not None:
            try:
                self.window.destroy()
            except Exception:
                pass
            self.window = None


# ── Точка входа ─────────────────────────────────────────────────────────────


def _setup_logging() -> None:
    try:
        paths.LOG_DIR.mkdir(parents=True, exist_ok=True)
        logging.basicConfig(
            filename=str(paths.LAUNCHER_LOG),
            filemode="a",
            encoding="utf-8",
            level=logging.INFO,
            format="%(asctime)s  %(levelname)s  %(name)s  %(message)s",
            force=True,
        )
    except Exception:
        logging.basicConfig(level=logging.INFO, force=True)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)

    # Multi-call: воркеры запускаются до загрузки pywebview/pystray.
    if args and args[0] in webui.WORKER_SCRIPTS:
        # Необработанное исключение здесь раньше показывало пользователю окно
        # «Unhandled exception in script» с простынёй трейсбека. Теперь причина
        # уходит в журнал задачи (её видно в панели), а код возврата — 1.
        try:
            webui.dispatch_worker(args)
        except SystemExit:
            raise
        except Exception as exc:  # noqa: BLE001 — любой сбой задачи
            _setup_logging()
            log.exception("Ошибка фоновой задачи %s", args[0])
            print(f"[ОШИБКА] {exc}", file=sys.stderr, flush=True)
            raise SystemExit(1) from None

    _setup_logging()
    host = _env_host()
    start_port = _env_port()
    log.info(
        "Запуск %s %s (host=%s, port=%s, frozen=%s)",
        version.APP_NAME,
        version.APP_VERSION,
        host,
        start_port,
        paths.IS_FROZEN,
    )

    try:
        port, existing_url = choose_port(host, start_port)
    except Exception as exc:
        log.exception("Не удалось выбрать порт")
        _message_box(f"Не удалось запустить приложение:\n{exc}", flags=0x10)
        return 1

    if existing_url:
        log.info("Приложение уже запущено: %s", existing_url)
        _message_box(
            "Приложение уже запущено. Открываю его в браузере.\n"
            "Если окно не появилось, найдите значок в трее."
        )
        webbrowser.open(existing_url)
        return 0

    server = ServerHandle(host, port)
    try:
        server.start()
        if not server.wait_ready():
            raise RuntimeError("веб-сервер не ответил за 40 секунд")
    except Exception as exc:
        log.exception("Не удалось запустить сервер")
        try:
            server.stop()
        except Exception:
            pass
        _message_box(f"Не удалось запустить веб-сервер:\n{exc}", flags=0x10)
        return 1

    app = Application(host, port, server)
    try:
        app.run()
    except Exception as exc:
        log.exception("Критическая ошибка")
        _message_box(f"Критическая ошибка:\n{exc}", flags=0x10)
        return 1
    finally:
        try:
            server.stop()
        except Exception:
            pass
    log.info("Приложение завершено")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
