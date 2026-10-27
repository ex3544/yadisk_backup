"""Проверки оконной обёртки: диспетчер воркеров, порты, CSRF, иконки."""

from __future__ import annotations

import socket
from types import SimpleNamespace

import pytest

import launcher
import webui


def test_worker_scripts_cover_all_subprocesses():
    # Эти скрипты WebUI запускает через TaskManager.
    for name in (
        "main.py",
        "unlim_backup.py",
        "unlim_download.py",
        "unlim_worker.py",
        "organize.py",
        "dedupe.py",
        "fetch_cookies.py",
        "watch.py",
    ):
        assert name in webui.WORKER_SCRIPTS
        assert webui.WORKER_SCRIPTS[name]


def test_dispatch_worker_returns_false_for_gui():
    assert webui.dispatch_worker([]) is False
    assert webui.dispatch_worker(["webui.py"]) is False
    assert webui.dispatch_worker(["--help"]) is False


def test_env_port_defaults_when_unset(monkeypatch):
    # Регрессия: без WEBUI_PORT приложение падало на int.strip().
    monkeypatch.delenv("WEBUI_PORT", raising=False)
    assert launcher._env_port() == launcher.DEFAULT_PORT


def test_env_port_reads_and_validation(monkeypatch):
    monkeypatch.setenv("WEBUI_PORT", " 8123 ")
    assert launcher._env_port() == 8123
    for bad in ("", "abc", "0", "70000", "-1"):
        monkeypatch.setenv("WEBUI_PORT", bad)
        assert launcher._env_port() == launcher.DEFAULT_PORT


def test_env_host_defaults_when_unset(monkeypatch):
    monkeypatch.delenv("WEBUI_HOST", raising=False)
    assert launcher._env_host() == launcher.DEFAULT_HOST
    monkeypatch.setenv("WEBUI_HOST", "  ")
    assert launcher._env_host() == launcher.DEFAULT_HOST


def test_base_url_maps_wildcard_and_ipv6():
    assert launcher.base_url("0.0.0.0", 8000) == "http://127.0.0.1:8000"
    assert launcher.base_url("", 8001) == "http://127.0.0.1:8001"
    assert launcher.base_url("::1", 8002) == "http://[::1]:8002"
    assert launcher.base_url("192.0.2.10", 8000) == "http://192.0.2.10:8000"


def test_origin_allowed_accepts_same_origin_on_any_port():
    assert webui.origin_allowed("http://127.0.0.1:8001", "127.0.0.1:8001")
    assert webui.origin_allowed("http://localhost:8010", "localhost:8010")
    assert webui.origin_allowed("http://127.0.0.1:8000", "127.0.0.1:8001")
    assert webui.origin_allowed("", "127.0.0.1:8001")
    # Чужие сайты и подделки под localhost по-прежнему блокируются.
    assert not webui.origin_allowed("http://evil.example", "127.0.0.1:8001")
    assert not webui.origin_allowed("http://localhost:8000.evil.example", "127.0.0.1:8001")
    assert not webui.origin_allowed("null", "127.0.0.1:8001")


def test_port_free_detects_bound_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        port = sock.getsockname()[1]
        assert launcher.port_free("127.0.0.1", port) is False
    assert launcher.port_free("127.0.0.1", port) is True


def test_choose_port_uses_existing_instance(monkeypatch):
    monkeypatch.setattr(
        launcher, "is_app_running", lambda url, timeout=0.8: url.endswith(":8000")
    )
    monkeypatch.setattr(launcher, "port_free", lambda host, port: False)
    assert launcher.choose_port("127.0.0.1", 8000) == (
        8000,
        "http://127.0.0.1:8000",
    )


def test_choose_port_skips_busy_ports(monkeypatch):
    monkeypatch.setattr(launcher, "is_app_running", lambda url, timeout=0.8: False)
    monkeypatch.setattr(
        launcher, "port_free", lambda host, port: port == 8002
    )
    assert launcher.choose_port("127.0.0.1", 8000) == (8002, None)


def test_choose_port_raises_when_no_ports(monkeypatch):
    monkeypatch.setattr(launcher, "is_app_running", lambda url, timeout=0.8: False)
    monkeypatch.setattr(launcher, "port_free", lambda host, port: False)
    with pytest.raises(RuntimeError):
        launcher.choose_port("127.0.0.1", 8000, tries=3)


def test_tray_image_available():
    image = launcher.tray_image()
    assert image is not None
    assert image.size[0] > 0
    assert image.mode == "RGBA"


def test_tray_image_falls_back_without_assets(monkeypatch, tmp_path):
    monkeypatch.setattr(launcher.paths, "ASSETS_DIR", tmp_path)
    image = launcher.tray_image()
    assert image is not None
    assert image.mode == "RGBA"


def test_edge_context_menu_patch_enables_default_menu():
    calls = []

    class FakeEdgeChrome:
        def on_webview_ready(self, sender, args):
            calls.append("original")

    class FakeSettings:
        def __init__(self):
            self.AreDefaultContextMenusEnabled = False

    class FakeCore:
        def __init__(self):
            self.Settings = FakeSettings()

    class FakeSender:
        def __init__(self):
            self.CoreWebView2 = FakeCore()

    class FakeArgs:
        IsSuccess = True

    module = SimpleNamespace(EdgeChrome=FakeEdgeChrome)
    assert launcher._patch_edgechromium_context_menu(module) is True
    sender = FakeSender()
    FakeEdgeChrome().on_webview_ready(sender, FakeArgs())
    assert calls == ["original"]
    assert sender.CoreWebView2.Settings.AreDefaultContextMenusEnabled is True


def test_edge_context_menu_patch_is_idempotent():
    class FakeEdgeChrome:
        def on_webview_ready(self, sender, args):
            pass

    module = SimpleNamespace(EdgeChrome=FakeEdgeChrome)
    assert launcher._patch_edgechromium_context_menu(module) is True
    patched = FakeEdgeChrome.on_webview_ready
    assert launcher._patch_edgechromium_context_menu(module) is True
    assert FakeEdgeChrome.on_webview_ready is patched


def test_edge_context_menu_patch_skips_failed_initialization():
    class FakeSettings:
        def __init__(self):
            self.AreDefaultContextMenusEnabled = False

    class FakeCore:
        def __init__(self):
            self.Settings = FakeSettings()

    class FakeSender:
        def __init__(self):
            self.CoreWebView2 = FakeCore()

    class FakeArgs:
        IsSuccess = False

    class FakeEdgeChrome:
        def on_webview_ready(self, sender, args):
            pass

    module = SimpleNamespace(EdgeChrome=FakeEdgeChrome)
    launcher._patch_edgechromium_context_menu(module)
    sender = FakeSender()
    FakeEdgeChrome().on_webview_ready(sender, FakeArgs())
    assert sender.CoreWebView2.Settings.AreDefaultContextMenusEnabled is False


class _FakeWindow:
    def __init__(self):
        self.hidden = 0
        self.destroyed = 0

    def hide(self):
        self.hidden += 1

    def destroy(self):
        self.destroyed += 1


class _FakeServer:
    def __init__(self):
        self.stopped = 0

    def stop(self):
        self.stopped += 1


class _FakeIcon:
    def __init__(self):
        self.stopped = 0

    def stop(self):
        self.stopped += 1

    def notify(self, *args, **kwargs):
        pass


def _app():
    return launcher.Application("127.0.0.1", 8000, _FakeServer())


def test_close_to_tray_hides_window(monkeypatch):
    notifications = []
    app = _app()
    app.window = _FakeWindow()
    app.icon = _FakeIcon()
    app.tray_ok = True
    monkeypatch.setattr(app, "_notify", lambda title, text: notifications.append(title))

    assert app._on_closing() is False
    assert app.window.hidden == 1
    assert notifications  # подсказка показывается один раз

    assert app._on_closing() is False
    assert app.window.hidden == 2
    assert len(notifications) == 1

    app._quitting = True
    assert app._on_closing() is True


def test_close_without_tray_quits():
    app = _app()
    app.window = _FakeWindow()
    app.tray_ok = False
    assert app._on_closing() is True
    assert app.window.hidden == 0


def test_shutdown_is_idempotent(monkeypatch):
    monkeypatch.setattr(webui.tm, "stop_all", lambda: 2)
    app = _app()
    window = _FakeWindow()
    app.window = window
    app.icon = _FakeIcon()

    app._shutdown()
    app._shutdown()

    assert app.server.stopped == 1
    assert window.destroyed == 1
    assert app.icon is None
    assert app.window is None
    assert app._quitting is True
