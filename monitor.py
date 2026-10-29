"""Фоновый мониторинг установки: heartbeat и алерты.

Раз в 15 минут клиент отправляет на Cloudflare Worker короткую JSON-сводку
(сколько файлов скачано, сбойно, сколько места свободно). Отдельно — алерты по
порогам: они уходят в тот же `/heartbeat`, а Worker пересылает их в Telegram.

Почему не поток из `webui.py`: нужен отдельный модуль, который можно проверить
тестами без FastAPI и без сети. Функция `evaluate()` — чистая: на вход статус и
состояние, на выходе список алертов. Сеть и файлы — только в `run_once()`.

Дедуп: каждый алерт имеет ключ, повторно он не уходит раньше чем через час
(`ALERT_COOLDOWN`). Состояние лежит в `paths.MONITOR_STATE_FILE`, поэтому
перезапуск программы не превращается в лавину сообщений.

Приватность: наружу уходят только счётчики и версия. Пути, токены и имена
файлов не отправляются никогда.
"""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
from pathlib import Path
from typing import Callable, Optional

import diagnostics
import paths
import version

HEARTBEAT_INTERVAL = 15 * 60          # как часто шлём сводку
ALERT_COOLDOWN = 60 * 60              # не чаще одного сообщения на ключ в час
FIRST_DELAY = 60                      # первая отправка — через минуту после старта

# Пороги алертов.
FAILED_MIN_FILES = 25                 # меньше — не повод будить
FAILED_RATIO = 0.25                   # доля сбойных от всех файлов базы
LOW_FREE_GB = 2.0                     # свободного места на диске выгрузки

_STATE_LOCK = threading.Lock()
_thread: Optional[threading.Thread] = None
_thread_lock = threading.Lock()


# ── состояние ───────────────────────────────────────────────────────────────

def _default_state() -> dict:
    return {"sent": {}, "last_heartbeat": 0.0, "last_error": "", "last_ok": 0.0}


def load_state(path: Optional[Path] = None) -> dict:
    path = path or paths.MONITOR_STATE_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _default_state()
    if not isinstance(data, dict):
        return _default_state()
    state = _default_state()
    sent = data.get("sent")
    if isinstance(sent, dict):
        state["sent"] = {
            str(k): float(v) for k, v in sent.items()
            if isinstance(v, (int, float))
        }
    for key in ("last_heartbeat", "last_ok"):
        if isinstance(data.get(key), (int, float)):
            state[key] = float(data[key])
    if isinstance(data.get("last_error"), str):
        state["last_error"] = data["last_error"][:300]
    return state


def save_state(state: dict, path: Optional[Path] = None) -> None:
    """Атомарная запись состояния; ошибки записи не ломают мониторинг."""
    path = path or paths.MONITOR_STATE_FILE
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass


# ── сводка ──────────────────────────────────────────────────────────────────

def _db_part(db: dict | None) -> dict:
    db = db or {}
    return {
        "total": int(db.get("total", 0) or 0),
        "downloaded": int(db.get("downloaded", 0) or 0),
        "pending": int(db.get("pending", 0) or 0),
        "failed": int(db.get("failed", 0) or 0),
    }


def current_phase(status: dict) -> str:
    """Короткое имя того, что сейчас идёт (для колонки в сводке)."""
    tasks = status.get("tasks") or {}
    for slot in ("disk", "unlim", "unlim_collect", "organize", "cookies_fetch"):
        task = tasks.get(slot) or {}
        if task.get("running"):
            return slot
    cycle = status.get("full_cycle") or {}
    if cycle.get("running"):
        return "full_cycle"
    return "idle"


def free_gb_for(status: dict) -> Optional[float]:
    """Минимум свободного места среди папок выгрузки."""
    config = status.get("config") or {}
    candidates = [
        config.get("disk_dir") or config.get("download_dir"),
        config.get("unlim_dir"),
    ]
    values: list[float] = []
    for value in candidates:
        if not value:
            continue
        try:
            usage = shutil.disk_usage(str(value))
        except OSError:
            continue
        values.append(round(usage.free / (1024 ** 3), 1))
    return min(values) if values else None


def build_heartbeat(status: dict) -> dict:
    """Готовит тело запроса `/heartbeat`. Ничего лишнего наружу не уходит."""
    return {
        "install_id": diagnostics.install_id(),
        "app_version": version.APP_VERSION,
        "phase": current_phase(status),
        "disk": _db_part((status.get("disk") or {}).get("db")),
        "unlim": _db_part((status.get("unlim") or {}).get("db")),
        "free_gb": free_gb_for(status),
    }


# ── алерты ──────────────────────────────────────────────────────────────────

def _failed_alert(name: str, part: dict) -> Optional[dict]:
    failed = part.get("failed", 0)
    total = part.get("total", 0)
    if failed < FAILED_MIN_FILES or total <= 0:
        return None
    if failed / total < FAILED_RATIO:
        return None
    return {
        "key": f"failed:{name}",
        "text": (f"{name}: {failed} сбойных из {total} "
                 f"({round(failed * 100 / total)} %). "
                 "Стоит открыть отчёт в «Диагностике»."),
    }


def _space_alert(free_gb: Optional[float], status: dict) -> Optional[dict]:
    if free_gb is None or free_gb >= LOW_FREE_GB:
        return None
    config = status.get("config") or {}
    target = config.get("unlim_dir") or config.get("disk_dir") or "папка выгрузки"
    return {
        "key": "space:low",
        "text": f"Мало места: {free_gb} ГБ свободно ({target}). Выгрузка остановится.",
    }


def _task_alerts(status: dict) -> list[dict]:
    out: list[dict] = []
    tasks = status.get("tasks") or {}
    for slot, task in tasks.items():
        task = task or {}
        code = task.get("exit_code")
        if code is None or code in (0, 1):
            continue
        out.append({
            "key": f"task:{slot}:{code}",
            "text": (f"Задание «{slot}» завершилось с кодом {code}. "
                     "Подробности — в журнале и отчёте."),
        })
    return out


def _cookie_alert(status: dict) -> Optional[dict]:
    cookies = status.get("cookies") or {}
    if not cookies.get("configured"):
        return None
    if cookies.get("ok", True):
        return None
    return {
        "key": "cookies:expired",
        "text": "Куки Безлимита недействительны — фото не выгружаются. "
                "Обновите cookies в «Настройках».",
    }


def evaluate(status: dict, state: dict, now: float) -> list[dict]:
    """Возвращает алерты, которые ещё не отправлялись за последний час.

    Чистая функция: ничего не пишет и не ходит в сеть — так её удобно тестировать.
    """
    state = state or _default_state()
    sent = state.get("sent") or {}
    candidates: list[Optional[dict]] = [
        _failed_alert("Диск", _db_part((status.get("disk") or {}).get("db"))),
        _failed_alert("Безлимит", _db_part((status.get("unlim") or {}).get("db"))),
        _space_alert(free_gb_for(status), status),
        _cookie_alert(status),
    ]
    candidates.extend(_task_alerts(status))
    out: list[dict] = []
    for item in candidates:
        if not item or not item.get("text"):
            continue
        last = sent.get(item["key"])
        if last is not None and now - last < ALERT_COOLDOWN:
            continue
        out.append(item)
    return out


def mark_sent(state: dict, alerts: list[dict], now: float) -> None:
    sent = state.setdefault("sent", {})
    for alert in alerts:
        sent[alert["key"]] = now
    # Старые отметки не нужны: ключи повторяются, а копить их бессмысленно.
    cutoff = now - 7 * 24 * 3600
    for key in [k for k, v in sent.items() if v < cutoff]:
        sent.pop(key, None)


# ── отправка ────────────────────────────────────────────────────────────────

def send_heartbeat(payload: dict, timeout: float = 20) -> dict:
    """POST на Worker. Ключ не печатается и не попадает в сообщения об ошибках."""
    key = diagnostics.report_key()
    if not key:
        return {"ok": False, "error": "no_key"}
    try:
        import requests
        response = requests.post(
            f"{diagnostics.report_url()}/heartbeat",
            headers={"X-Report-Key": key},
            json=payload,
            timeout=(10, timeout),
        )
    except Exception as e:  # noqa: BLE001 - сеть бывает любой
        return {"ok": False, "error": type(e).__name__}
    try:
        data = response.json()
    except ValueError:
        data = {}
    if response.status_code == 200 and data.get("ok"):
        return {"ok": True, "stored": bool(data.get("stored")),
                "alerts_sent": int(data.get("alerts_sent", 0) or 0)}
    return {"ok": False, "error": str(data.get("error") or response.status_code)[:120]}


def run_once(status_provider: Callable[[], dict],
             now: Optional[float] = None,
             state_path: Optional[Path] = None) -> dict:
    """Один цикл: собрать сводку, добавить неотправленные алерты, отправить."""
    now = time.time() if now is None else now
    try:
        status = status_provider() or {}
    except Exception as e:  # noqa: BLE001 - статус может не собраться во время старта
        return {"ok": False, "error": f"status: {type(e).__name__}"}

    with _STATE_LOCK:
        state = load_state(state_path)
    alerts = evaluate(status, state, now)
    payload = build_heartbeat(status)
    payload["alerts"] = [{"text": a["text"]} for a in alerts]

    result = send_heartbeat(payload)
    with _STATE_LOCK:
        state = load_state(state_path)
        state["last_heartbeat"] = now
        if result.get("ok"):
            state["last_ok"] = now
            state["last_error"] = ""
            mark_sent(state, alerts, now)
        else:
            state["last_error"] = str(result.get("error", ""))[:300]
        save_state(state, state_path)
    result["alerts"] = len(alerts)
    return result


def _loop(status_provider: Callable[[], dict]) -> None:
    time.sleep(FIRST_DELAY)
    while True:
        try:
            run_once(status_provider)
        except Exception:  # noqa: BLE001 - монитор не должен ронять приложение
            pass
        time.sleep(HEARTBEAT_INTERVAL)


def start_monitor(status_provider: Callable[[], dict]) -> bool:
    """Запускает фоновый поток один раз. Возвращает True, если поток создан."""
    global _thread
    if not diagnostics.report_key_configured():
        return False
    with _thread_lock:
        if _thread is not None and _thread.is_alive():
            return False
        _thread = threading.Thread(
            target=_loop,
            args=(status_provider,),
            name="monitor",
            daemon=True,
        )
        _thread.start()
        return True


def monitor_status() -> dict:
    """Короткая сводка о самом мониторинге — для карточки «Диагностика»."""
    state = load_state()
    return {
        "running": bool(_thread is not None and _thread.is_alive()),
        "last_heartbeat": state.get("last_heartbeat") or None,
        "last_ok": state.get("last_ok") or None,
        "last_error": state.get("last_error", ""),
        "alerts_sent": len(state.get("sent") or {}),
    }
