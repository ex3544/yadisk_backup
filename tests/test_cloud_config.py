"""C2: какие папки облака обходить и что качать из Безлимита."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import main
import paths
import utils
import webui

PROJECT_DIR = Path(__file__).resolve().parents[1]


# ── нормализация ────────────────────────────────────────────────────────────


def test_normalize_scan_roots_defaults_to_whole_disk():
    assert utils.normalize_scan_roots("") == ["/"]
    assert utils.normalize_scan_roots(None) == ["/"]
    assert utils.normalize_scan_roots([]) == ["/"]


def test_normalize_scan_roots_parses_env_string():
    assert utils.normalize_scan_roots("Фото, /Документы/, ,") == ["/Фото", "/Документы"]


def test_normalize_scan_roots_dedupes_and_fixes_slashes():
    assert utils.normalize_scan_roots(["/a", "a/", "/a"]) == ["/a"]
    assert utils.normalize_scan_roots(["Фото\\2024"]) == ["/Фото/2024"]


def test_whole_disk_wins_over_other_roots():
    assert utils.normalize_scan_roots(["/Фото", "/"]) == ["/"]


def test_normalize_unlim_media():
    assert utils.normalize_unlim_media("VIDEOs") == "videos"
    assert utils.normalize_unlim_media("all") == "all"
    assert utils.normalize_unlim_media("музыку") == "photos"
    assert utils.normalize_unlim_media("музыку", "") == ""


# ── сохранение настроек из окна ─────────────────────────────────────────────


def _clean_env(monkeypatch):
    for key in ("DISK_SCAN_ROOTS", "UNLIM_MEDIA", "PREVIEWS"):
        monkeypatch.delenv(key, raising=False)


def _env_file(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("", encoding="utf-8")
    monkeypatch.setattr(webui, "ENV_FILE", env)
    _clean_env(monkeypatch)
    return env


def test_save_scan_roots_writes_env(tmp_path, monkeypatch):
    env = _env_file(tmp_path, monkeypatch)
    res = webui.api_set_scan_roots(webui.ScanRootsPayload(roots=["/Фото", "Видео"]))
    assert res["ok"] is True
    assert res["scan_roots"] == ["/Фото", "/Видео"]
    assert "DISK_SCAN_ROOTS=/Фото,/Видео" in env.read_text(encoding="utf-8")
    assert webui.get_config()["scan_roots"] == ["/Фото", "/Видео"]


def test_save_scan_roots_empty_means_whole_disk(tmp_path, monkeypatch):
    _env_file(tmp_path, monkeypatch)
    res = webui.api_set_scan_roots(webui.ScanRootsPayload(roots=[]))
    assert res["scan_roots"] == ["/"]
    assert "весь Диск" in res["msg"]


def test_save_unlim_media(tmp_path, monkeypatch):
    env = _env_file(tmp_path, monkeypatch)
    res = webui.api_set_unlim_media(webui.UnlimMediaPayload(media="videos"))
    assert res["ok"] is True
    assert "только видео" in res["msg"]
    assert "UNLIM_MEDIA=videos" in env.read_text(encoding="utf-8")
    assert webui.get_config()["unlim_media"] == "videos"


def test_save_unlim_media_rejects_unknown(tmp_path, monkeypatch):
    _env_file(tmp_path, monkeypatch)
    res = webui.api_set_unlim_media(webui.UnlimMediaPayload(media="музыка"))
    assert res["ok"] is False


def test_config_falls_back_to_defaults(tmp_path, monkeypatch):
    _env_file(tmp_path, monkeypatch)
    cfg = webui.get_config()
    assert cfg["scan_roots"] == ["/"]
    assert cfg["unlim_media"] == "all"


def test_main_load_config_reads_cloud_settings(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text(
        "DISK_SCAN_ROOTS=/Фото,/Видео\nUNLIM_MEDIA=videos\n", encoding="utf-8"
    )
    monkeypatch.setattr(paths, "ENV_FILE", env)
    _clean_env(monkeypatch)
    try:
        cfg = main.load_config(require_token=False)
    finally:
        for key in ("DISK_SCAN_ROOTS", "UNLIM_MEDIA"):
            os.environ.pop(key, None)
    assert cfg["scan_roots"] == ["/Фото", "/Видео"]
    assert cfg["unlim_media"] == "videos"


# ── список папок облака ─────────────────────────────────────────────────────


def _item(name, path, typ="dir"):
    return SimpleNamespace(name=name, path=path, type=typ)


def test_cloud_folders_keeps_only_dirs_sorted():
    items = [
        _item("Фото", "/disk/Фото"),
        _item("a.txt", "/disk/a.txt", typ="file"),
        _item("Архив", "/disk/Архив"),
        _item("", "/disk/БезИмени"),
    ]
    folders = webui.cloud_folders_from(items)
    assert [f["name"] for f in folders] == ["Архив", "БезИмени", "Фото"]
    assert folders[0]["path"] == "/disk/Архив"


def test_disk_folders_without_token(tmp_path, monkeypatch):
    _env_file(tmp_path, monkeypatch)
    monkeypatch.delenv("YA_DISK_TOKEN", raising=False)
    assert webui.api_disk_folders("/")["ok"] is False


def test_db_top_folders_uses_first_segment(tmp_path):
    import sqlite3

    db = tmp_path / "state.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE files (remote_path TEXT PRIMARY KEY)")
    con.executemany(
        "INSERT INTO files VALUES (?)",
        [
            ("disk:/Фото/2024/a.jpg",),
            ("disk:/Фото/b.jpg",),
            ("disk:/Док/c.pdf",),
            ("disk:/top.jpg",),  # файл в корне — не папка
        ],
    )
    con.commit()
    con.close()
    assert webui.db_top_folders(db) == [
        {"name": "Док", "path": "/Док"},
        {"name": "Фото", "path": "/Фото"},
    ]


def test_disk_folders_falls_back_to_db_when_cloud_is_down(tmp_path, monkeypatch):
    import sqlite3
    import sys

    db = tmp_path / "state.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE files (remote_path TEXT PRIMARY KEY)")
    con.execute("INSERT INTO files VALUES ('disk:/Фото/a.jpg')")
    con.commit()
    con.close()

    class _BrokenClient:
        def __init__(self, token=""):
            pass

        def listdir(self, path):
            raise RuntimeError("нет сети")

        def close(self):
            pass

    monkeypatch.setitem(sys.modules, "yadisk", SimpleNamespace(Client=_BrokenClient))
    monkeypatch.setattr(webui, "get_config", lambda: {"token": "x"})
    monkeypatch.setattr(webui, "DB_FILE", db)

    res = webui.api_disk_folders("/")
    assert res["ok"] is True
    assert res["from_db"] is True
    assert res["folders"] == [{"name": "Фото", "path": "/Фото"}]
    assert "RuntimeError" in res["msg"]


# ── окно и пример настроек ──────────────────────────────────────────────────


def test_cloud_card_in_ui():
    html = (PROJECT_DIR / "webui_assets" / "index.html").read_text(encoding="utf-8")
    assert 'id="cloud-card"' in html
    assert 'id="scan-root-input"' in html
    assert 'id="unlim-media-select"' in html
    assert "function renderScanRoots(" in html
    assert "function saveScanRoots(" in html
    assert "function saveUnlimMedia(" in html
    assert "function loadCloudFolders(" in html


def test_env_example_documents_cloud_settings():
    example = (PROJECT_DIR / ".env.example").read_text(encoding="utf-8")
    assert "DISK_SCAN_ROOTS=/" in example
    assert "UNLIM_MEDIA=all" in example
