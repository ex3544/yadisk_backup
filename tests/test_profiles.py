"""Профили пользователей: отдельные данные на каждого клиента."""

import json

import pytest

import paths
import profiles


@pytest.fixture
def base(tmp_path, monkeypatch):
    root = tmp_path / "YandexDiskBackup"
    root.mkdir()
    monkeypatch.setattr(paths, "BASE_DATA_DIR", root)
    monkeypatch.setattr(paths, "DATA_DIR", root)
    return root


def test_main_profile_is_the_existing_data_dir(base):
    items = profiles.list_profiles()
    assert [p["id"] for p in items] == [profiles.MAIN_ID]
    assert items[0]["active"] is True
    assert items[0]["main"] is True
    assert items[0]["dir"] == str(base)
    assert profiles.current_profile()["name"] == profiles.MAIN_NAME


def test_create_profile_makes_its_own_folder_and_dirs_config(base):
    created = profiles.create_profile("Клиент Пётр", r"E:\Клиент\Disk", r"E:\Клиент\Unlim")
    target = base / "profiles" / created["id"]
    assert target.is_dir()
    assert (target / "logs").is_dir()
    assert (target / "reports").is_dir()
    env = (target / ".env").read_text(encoding="utf-8")
    assert r"DISK_DIR=E:\Клиент\Disk" in env
    assert r"UNLIM_DIR=E:\Клиент\Unlim" in env
    # Без явных путей папки выгрузки всё равно свои — с именем клиента.
    plain = profiles.create_profile("Без папок")
    plain_env = (base / "profiles" / plain["id"] / ".env").read_text(encoding="utf-8")
    assert "Клиенты" in plain_env and "Без папок" in plain_env
    assert str(paths.DEFAULT_DISK_DIR) not in plain_env
    # Реестр — общий, рядом с базовым каталогом данных.
    data = json.loads((base / "profiles.json").read_text(encoding="utf-8"))
    assert data["profiles"][0]["name"] == "Клиент Пётр"
    # Чужой профиль не считается текущим.
    assert profiles.current_profile()["id"] == profiles.MAIN_ID
    assert [p["name"] for p in profiles.list_profiles()] == [
        profiles.MAIN_NAME, "Клиент Пётр", "Без папок"]


def test_names_are_validated(base):
    with pytest.raises(ValueError):
        profiles.create_profile("   ")
    with pytest.raises(ValueError):
        profiles.create_profile("a/b")
    profiles.create_profile("Клиент")
    with pytest.raises(ValueError):
        profiles.create_profile("клиент")   # регистр не важен
    with pytest.raises(ValueError):
        profiles.create_profile("Основной")


def test_rename_keeps_the_folder(base):
    created = profiles.create_profile("Старое имя")
    renamed = profiles.rename_profile(created["id"], "Новое имя")
    assert renamed["name"] == "Новое имя"
    assert renamed["dir"] == created["dir"]


def test_main_profile_can_be_renamed_but_not_deleted(base):
    """35.8.0: у основного профиля своё имя, удалить его по-прежнему нельзя."""
    renamed = profiles.rename_profile(profiles.MAIN_ID, "Сергей")
    assert renamed["name"] == "Сергей"
    assert profiles.current_profile()["name"] == "Сергей"
    # Имя переживает повторное чтение реестра.
    items = profiles.list_profiles()
    assert items[0]["id"] == profiles.MAIN_ID
    assert items[0]["name"] == "Сергей"
    with pytest.raises(ValueError):
        profiles.delete_profile(profiles.MAIN_ID)


def test_avatar_is_remembered_for_main_and_other_profiles(base):
    created = profiles.create_profile("Клиент")
    assert created["avatar"] in profiles.AVATARS

    changed = profiles.set_avatar(profiles.MAIN_ID, "av-panda")
    assert changed["avatar"] == "av-panda"
    assert profiles.current_profile()["avatar"] == "av-panda"

    profiles.set_avatar(created["id"], "av-hedgehog")
    listed = {p["id"]: p for p in profiles.list_profiles()}
    assert listed[created["id"]]["avatar"] == "av-hedgehog"
    assert listed[profiles.MAIN_ID]["avatar"] == "av-panda"

    # Неизвестная аватарка не ломает шапку — берём стандартную.
    assert profiles.set_avatar(profiles.MAIN_ID, "<script>")["avatar"] == profiles.DEFAULT_AVATAR


def test_rename_main_keeps_avatar(base):
    profiles.set_avatar(profiles.MAIN_ID, "av-owl")
    profiles.rename_profile(profiles.MAIN_ID, "Сергей")
    assert profiles.current_profile()["avatar"] == "av-owl"


def test_delete_removes_only_foreign_profiles(base):
    created = profiles.create_profile("Клиент")
    target = base / "profiles" / created["id"]
    assert target.is_dir()
    profiles.delete_profile(created["id"])
    assert not target.exists()
    assert len(profiles.list_profiles()) == 1
    with pytest.raises(ValueError):
        profiles.delete_profile(profiles.MAIN_ID)


def test_active_profile_cannot_be_deleted(base, monkeypatch):
    created = profiles.create_profile("Клиент")
    # Делаем вид, что программа сейчас работает из этого профиля.
    monkeypatch.setattr(paths, "DATA_DIR", base / "profiles" / created["id"])
    assert profiles.current_profile()["name"] == "Клиент"
    with pytest.raises(ValueError):
        profiles.delete_profile(created["id"])


def test_delete_refuses_a_folder_outside_the_profiles_root(base):
    outside = base / "elsewhere"
    outside.mkdir()
    data = {"version": 1, "profiles": [
        {"id": "p12345678", "name": "Внешний", "dir": str(outside), "created": ""},
    ]}
    (base / "profiles.json").write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError):
        profiles.delete_profile("p12345678")
    assert outside.is_dir()


def test_switching_recreates_a_missing_folder(base):
    created = profiles.create_profile("Клиент")
    target = base / "profiles" / created["id"]
    import shutil

    shutil.rmtree(target)
    assert profiles.profile_dir(created["id"]) == target
    assert target.is_dir()


def test_custom_data_dir_is_shown_read_only(tmp_path, monkeypatch, base):
    custom = tmp_path / "custom"
    custom.mkdir()
    monkeypatch.setattr(paths, "DATA_DIR", custom)
    cur = profiles.current_profile()
    assert cur["custom"] is True
    assert cur["name"] == "Своя папка"
    assert cur["active"] is True


def test_broken_registry_does_not_break_the_window(base):
    (base / "profiles.json").write_text("{не json", encoding="utf-8")
    assert [p["id"] for p in profiles.list_profiles()] == [profiles.MAIN_ID]
