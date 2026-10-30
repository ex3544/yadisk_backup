"""Одна копия программы: второй запуск не должен открывать окно."""

import os
import uuid

import pytest

import single_instance


@pytest.fixture
def unique_name():
    return "Local\\YadBackup-test-" + uuid.uuid4().hex


def test_second_copy_is_rejected(unique_name, tmp_path):
    try:
        assert single_instance.acquire(unique_name, tmp_path / "app.lock") is True
        assert single_instance.acquire(unique_name, tmp_path / "app.lock") is False
    finally:
        single_instance.release()
    # После освобождения место снова свободно — программа может перезапуститься.
    assert single_instance.acquire(unique_name, tmp_path / "app.lock") is True
    single_instance.release()


def test_windows_uses_a_mutex(unique_name):
    if os.name != "nt":
        pytest.skip("мьютекс только на Windows")
    assert single_instance.acquire(unique_name) is True
    single_instance.release()


def test_message_is_human(unique_name):
    text = single_instance.already_running_message()
    assert "уже запущен" in text
    assert "трее" in text
