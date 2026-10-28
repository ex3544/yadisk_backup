"""Проверки CLI-команды download: повтор сбойных файлов при --retry."""

from __future__ import annotations

import argparse

import main


class _FakeState:
    def __init__(self, path, failed_to_reset: int):
        self.path = path
        self.failed_to_reset = failed_to_reset
        self.resets = 0
        self.closed = False

    def get_stats(self):
        return {"pending": 0}

    def reset_failed_to_pending(self) -> int:
        self.resets += 1
        return self.failed_to_reset

    def close(self) -> None:
        self.closed = True


def _run_download(monkeypatch, tmp_path, *, retry: bool, failed_to_reset: int):
    state = _FakeState(tmp_path / "state.db", failed_to_reset)
    calls = []

    def fake_download_all(**kwargs):
        calls.append(kwargs)
        base = kwargs.get("session") or {}
        return {
            "downloaded": int(base.get("downloaded", 0)) + 3,
            "skipped": int(base.get("skipped", 0)),
            "failed": int(base.get("failed", 0)),
            "bytes": 0,
        }

    monkeypatch.setattr(main, "check_token", lambda token: (True, "OK"))
    monkeypatch.setattr(main, "State", lambda path: state)
    monkeypatch.setattr(main, "DB_FILE", tmp_path / "state.db")
    monkeypatch.setattr(main, "download_all", fake_download_all)
    monkeypatch.setattr(main, "auto_workers", lambda: 2)
    monkeypatch.setattr(main, "_print_summary", lambda *args, **kwargs: None)
    # Ранний статус пишется в файл вне tmp_path — в тестах не нужен.
    monkeypatch.setattr(main, "write_status_start", lambda: None)

    args = argparse.Namespace(workers=2, retry=retry, no_scan=True, root="/")
    rc = main.cmd_download(
        {"token": "test-token", "download_dir": str(tmp_path)}, args
    )
    return rc, state, calls


def test_retry_resets_old_failed_without_new_failures(monkeypatch, tmp_path):
    """При пустом pending галочка «повторить» должна поднять старые failed."""
    rc, state, downloads = _run_download(
        monkeypatch, tmp_path, retry=True, failed_to_reset=645
    )

    assert state.resets == 1
    assert len(downloads) == 2
    assert state.closed is True
    assert rc == 0


def test_without_retry_does_not_touch_failed(monkeypatch, tmp_path):
    rc, state, downloads = _run_download(
        monkeypatch, tmp_path, retry=False, failed_to_reset=645
    )

    assert state.resets == 0
    assert len(downloads) == 1
    assert state.closed is True
    assert rc == 0


def test_second_pass_carries_session_totals(monkeypatch, tmp_path):
    """Итог сессии — оба прохода: второй начинается с накопленных счётчиков."""
    rc, state, calls = _run_download(
        monkeypatch, tmp_path, retry=True, failed_to_reset=645
    )

    assert len(calls) == 2
    assert calls[0].get("session") is None
    # Первый проход дал 3 файла — второй должен прибавлять к ним, а не с нуля.
    assert calls[1]["session"]["downloaded"] == 3
    assert rc == 0


def test_retry_without_failed_skips_second_pass(monkeypatch, tmp_path):
    rc, state, downloads = _run_download(
        monkeypatch, tmp_path, retry=True, failed_to_reset=0
    )

    assert state.resets == 1
    assert len(downloads) == 1
    assert state.closed is True
    assert rc == 0
