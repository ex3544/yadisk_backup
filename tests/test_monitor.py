"""Мониторинг: сводка без секретов, алерты по порогам и дедуп раз в час."""

import json

import pytest

import diagnostics
import monitor
import paths


def _status(disk=None, unlim=None, tasks=None, cookies=None, config=None):
    return {
        "config": config or {"disk_dir": "", "unlim_dir": ""},
        "disk": {"db": disk or {"total": 0, "downloaded": 0, "pending": 0,
                                 "failed": 0}},
        "unlim": {"db": unlim or {"total": 0, "downloaded": 0, "pending": 0,
                                   "failed": 0}},
        "tasks": tasks or {},
        "cookies": cookies or {"configured": False},
    }


@pytest.fixture()
def no_network(monkeypatch):
    monkeypatch.setattr(monitor, "free_gb_for", lambda status: None)


def test_build_heartbeat_has_only_counters(monkeypatch):
    monkeypatch.setattr(diagnostics, "install_id", lambda: "0123456789abcdef")
    heartbeat = monitor.build_heartbeat(_status(
        disk={"total": 10, "downloaded": 7, "pending": 2, "failed": 1},
        config={"disk_dir": r"C:\Secret\Disk", "unlim_dir": r"D:\Secret\Unlim"},
    ))
    text = json.dumps(heartbeat, ensure_ascii=False)
    assert heartbeat["install_id"] == "0123456789abcdef"
    assert heartbeat["disk"] == {"total": 10, "downloaded": 7,
                                  "pending": 2, "failed": 1}
    # Наружу не уходят пути и имена файлов.
    assert "Secret" not in text
    assert "C:\\" not in text and "D:\\" not in text


def test_current_phase_reports_running_task():
    status = _status(tasks={"unlim": {"running": True}})
    assert monitor.current_phase(status) == "unlim"
    assert monitor.current_phase(_status()) == "idle"
    assert monitor.current_phase(
        {"full_cycle": {"running": True}, "tasks": {}}) == "full_cycle"


def test_failed_threshold_needs_both_count_and_share(no_network):
    state = monitor._default_state()
    few = _status(disk={"total": 1000, "failed": 10})
    assert monitor.evaluate(few, state, 1000.0) == []

    many_but_ok = _status(disk={"total": 1000, "failed": 20})
    assert monitor.evaluate(many_but_ok, state, 1000.0) == []

    bad = _status(disk={"total": 100, "failed": 40})
    alerts = monitor.evaluate(bad, state, 1000.0)
    assert [a["key"] for a in alerts] == ["failed:Диск"]
    assert "40" in alerts[0]["text"]


def test_alert_is_sent_at_most_once_an_hour(no_network):
    status = _status(unlim={"total": 100, "failed": 30})
    state = monitor._default_state()
    alerts = monitor.evaluate(status, state, 1000.0)
    assert len(alerts) == 1

    monitor.mark_sent(state, alerts, 1000.0)
    assert monitor.evaluate(status, state, 1000.0 + 3599) == []
    assert monitor.evaluate(status, state, 1000.0 + 3601)[0]["key"] == "failed:Безлимит"


def test_low_space_alert(monkeypatch):
    monkeypatch.setattr(monitor, "free_gb_for", lambda status: 1.2)
    alerts = monitor.evaluate(_status(), monitor._default_state(), 10.0)
    keys = [a["key"] for a in alerts]
    assert "space:low" in keys

    monkeypatch.setattr(monitor, "free_gb_for", lambda status: 50.0)
    assert monitor.evaluate(_status(), monitor._default_state(), 10.0) == []


def test_failed_task_and_expired_cookies_are_alerted(no_network):
    status = _status(
        tasks={"disk": {"running": False, "exit_code": 2}},
        cookies={"configured": True, "ok": False},
    )
    keys = {a["key"] for a in monitor.evaluate(status, monitor._default_state(), 5.0)}
    assert "task:disk:2" in keys
    assert "cookies:expired" in keys

    # Код 1 — штатное «есть сбойные файлы», о нём не будим.
    ok_status = _status(tasks={"disk": {"running": False, "exit_code": 1}})
    assert monitor.evaluate(ok_status, monitor._default_state(), 5.0) == []


def test_state_roundtrip(tmp_path):
    path = tmp_path / ".monitor_state.json"
    state = monitor._default_state()
    state["sent"]["failed:Диск"] = 123.0
    state["last_ok"] = 456.0
    monitor.save_state(state, path)
    loaded = monitor.load_state(path)
    assert loaded["sent"]["failed:Диск"] == 123.0
    assert loaded["last_ok"] == 456.0


def test_load_state_tolerates_garbage(tmp_path):
    path = tmp_path / ".monitor_state.json"
    path.write_text("не json", encoding="utf-8")
    assert monitor.load_state(path) == monitor._default_state()


def test_run_once_sends_alerts_and_records_them(tmp_path, monkeypatch):
    sent = {}

    def fake_send(payload, timeout=20):
        sent["payload"] = payload
        return {"ok": True, "stored": True, "alerts_sent": len(payload["alerts"])}

    monkeypatch.setattr(monitor, "send_heartbeat", fake_send)
    monkeypatch.setattr(monitor, "free_gb_for", lambda status: None)
    state_path = tmp_path / ".monitor_state.json"
    status = _status(disk={"total": 100, "failed": 30})

    result = monitor.run_once(lambda: status, now=1000.0, state_path=state_path)
    assert result["ok"] is True
    assert result["alerts"] == 1
    assert sent["payload"]["alerts"][0]["text"]

    # Повторный цикл в пределах часа — без алертов, но heartbeat уходит.
    monitor.run_once(lambda: status, now=1100.0, state_path=state_path)
    assert sent["payload"]["alerts"] == []


def test_send_without_key_is_noop(monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "REPORT_KEY_FILE", tmp_path / "key.txt")
    monkeypatch.setattr(paths, "BUNDLED_REPORT_KEY_FILE", tmp_path / "bundled.txt")
    monkeypatch.delenv("REPORT_KEY", raising=False)
    assert monitor.send_heartbeat({"install_id": "0123456789abcdef"}) == {
        "ok": False, "error": "no_key"}
