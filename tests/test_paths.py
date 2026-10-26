"""Тесты выбора каталога данных: настройки, исходники и установленная версия."""

import paths


def test_env_override_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("YADISK_DATA_DIR", str(tmp_path / "custom"))
    assert paths._resolve_data_dir() == (tmp_path / "custom").resolve()


def test_source_mode_keeps_existing_data_folder(tmp_path, monkeypatch):
    monkeypatch.delenv("YADISK_DATA_DIR", raising=False)
    monkeypatch.setattr(paths, "IS_FROZEN", False)
    monkeypatch.setattr(paths, "APP_DIR", tmp_path)
    (tmp_path / ".env").write_text("", encoding="utf-8")
    assert paths._resolve_data_dir() == tmp_path


def test_source_mode_without_data_uses_default(tmp_path, monkeypatch):
    monkeypatch.delenv("YADISK_DATA_DIR", raising=False)
    monkeypatch.setattr(paths, "IS_FROZEN", False)
    monkeypatch.setattr(paths, "APP_DIR", tmp_path / "src")
    monkeypatch.setattr(paths, "_default_data_dir", lambda: tmp_path / "data")
    assert paths._resolve_data_dir() == tmp_path / "data"


def test_frozen_mode_ignores_project_files(tmp_path, monkeypatch):
    monkeypatch.delenv("YADISK_DATA_DIR", raising=False)
    monkeypatch.setattr(paths, "IS_FROZEN", True)
    monkeypatch.setattr(paths, "APP_DIR", tmp_path)
    (tmp_path / ".env").write_text("", encoding="utf-8")
    monkeypatch.setattr(paths, "_default_data_dir", lambda: tmp_path / "localappdata")
    assert paths._resolve_data_dir() == tmp_path / "localappdata"


def test_ensure_dirs_creates_all(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(paths, "LOG_DIR", tmp_path / "data" / "logs")
    monkeypatch.setattr(paths, "REPORT_DIR", tmp_path / "data" / "reports")
    paths.ensure_dirs()
    assert (tmp_path / "data" / "logs").is_dir()
    assert (tmp_path / "data" / "reports").is_dir()


def test_describe_contains_key_paths():
    info = paths.describe()
    assert {"app_dir", "data_dir", "state_db", "unlim_db", "logs"} <= set(info)
