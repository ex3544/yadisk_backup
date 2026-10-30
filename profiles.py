"""Профили пользователей: у каждого клиента свои данные.

Зачем
-----
Сергей принимает у знакомых куки и токен и качает их облако на своей машине.
Если всё лежит в одном каталоге данных, куки, базы, логи и папки выгрузки
перемешиваются с его собственными.

Как устроено
------------
Профиль — отдельный каталог данных: `.env`, `cookies.json`, `state.db`,
`unlim_state.db`, `logs/`, `reports/`. Всё это уже берётся из `paths`, а
`paths.DATA_DIR` выбирается по переменной `YADISK_DATA_DIR` при импорте.

Поэтому переключение профиля — это **перезапуск программы** с другим
`YADISK_DATA_DIR`; в работающем процессе пути поменять нельзя.

Реестр профилей — `BASE_DATA_DIR/profiles.json`, сами профили —
`BASE_DATA_DIR/profiles/<id>/`. «Основной» профиль — это сам
`BASE_DATA_DIR`: данные Сергея остаются на месте, переносить ничего не нужно.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
import uuid
from pathlib import Path

import paths

MAIN_ID = "main"
MAIN_NAME = "Основной"
REGISTRY_VERSION = 1
NAME_LIMIT = 40
# Символы, недопустимые в имени профиля: имя попадает в списки и файлы,
# поэтому управляющие символы и разделители путей запрещены.
_BAD_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def profiles_root() -> Path:
    return paths.BASE_DATA_DIR / "profiles"


def registry_file() -> Path:
    return paths.BASE_DATA_DIR / "profiles.json"


# ── реестр ──────────────────────────────────────────────────────────────────

def _main_profile() -> dict:
    return {
        "id": MAIN_ID,
        "name": MAIN_NAME,
        "dir": str(paths.BASE_DATA_DIR),
        "created": "",
        "main": True,
    }


def _read_registry() -> dict:
    """Читает реестр; битый файл не должен ломать окно — начинаем с пустого."""
    path = registry_file()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"version": REGISTRY_VERSION, "profiles": []}
    if not isinstance(data, dict) or not isinstance(data.get("profiles"), list):
        return {"version": REGISTRY_VERSION, "profiles": []}
    out = {"version": REGISTRY_VERSION, "profiles": []}
    for item in data["profiles"]:
        if not isinstance(item, dict):
            continue
        pid = str(item.get("id") or "").strip()
        pdir = str(item.get("dir") or "").strip()
        name = str(item.get("name") or "").strip()
        if not pid or not pdir or not name or pid == MAIN_ID:
            continue
        out["profiles"].append({
            "id": pid,
            "name": name,
            "dir": pdir,
            "created": str(item.get("created") or ""),
        })
    return out


def _write_registry(data: dict) -> None:
    path = registry_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _all_entries(data: dict) -> list[dict]:
    return [_main_profile(), *data["profiles"]]


def _find(data: dict, pid: str) -> dict | None:
    for item in _all_entries(data):
        if item["id"] == pid:
            return item
    return None


def _same_dir(a: str | Path, b: str | Path) -> bool:
    try:
        return Path(a).resolve() == Path(b).resolve()
    except OSError:
        return str(a) == str(b)


# ── имена и идентификаторы ──────────────────────────────────────────────────

def validate_name(name: str) -> str:
    """Возвращает очищенное имя или бросает ValueError с понятным текстом."""
    value = " ".join(str(name or "").split())
    if not value:
        raise ValueError("Введите имя профиля.")
    if len(value) > NAME_LIMIT:
        raise ValueError(f"Имя длиннее {NAME_LIMIT} символов.")
    if _BAD_NAME.search(value):
        raise ValueError('В имени нельзя использовать < > : " / \\ | ? *')
    return value


def _name_taken(name: str, *, skip_id: str = "") -> bool:
    low = name.casefold()
    for item in _all_entries(_read_registry()):
        if item["id"] == skip_id:
            continue
        if item["name"].casefold() == low:
            return True
    return False


def _new_id() -> str:
    return "p" + uuid.uuid4().hex[:8]


# ── чтение ──────────────────────────────────────────────────────────────────

def current_profile() -> dict:
    """Профиль, из каталога которого работает программа."""
    cur = paths.DATA_DIR
    for item in _all_entries(_read_registry()):
        if _same_dir(item["dir"], cur):
            return {**item, "active": True, "exists": True}
    # Запуск с YADISK_DATA_DIR, которого нет в реестре: это тоже «свой» путь,
    # но управлять им из окна нельзя — только показать.
    return {
        "id": "",
        "name": "Своя папка",
        "dir": str(cur),
        "created": "",
        "main": False,
        "active": True,
        "custom": True,
        "exists": True,
    }


def list_profiles() -> list[dict]:
    cur = paths.DATA_DIR
    out = []
    for item in _all_entries(_read_registry()):
        d = item["dir"]
        out.append({
            **item,
            "active": _same_dir(d, cur),
            "exists": Path(d).is_dir(),
        })
    return out


# ── изменение ───────────────────────────────────────────────────────────────

def create_profile(name: str, disk_dir: str = "", unlim_dir: str = "") -> dict:
    """Создаёт пустой профиль: своя папка данных и свои папки выгрузки."""
    clean = validate_name(name)
    if _name_taken(clean):
        raise ValueError("Профиль с таким именем уже есть.")
    pid = _new_id()
    data = _read_registry()
    target = profiles_root() / pid
    target.mkdir(parents=True, exist_ok=True)
    (target / "logs").mkdir(exist_ok=True)
    (target / "reports").mkdir(exist_ok=True)
    # Папки выгрузки обязаны быть своими: иначе файлы клиента лягут в папки
    # основного профиля. Если путь не задан — берём подпапку с именем клиента.
    base_out = paths.DEFAULT_BACKUP_ROOT / "Клиенты" / clean
    disk_value = str(disk_dir or "").strip() or str(base_out / "Disk")
    unlim_value = str(unlim_dir or "").strip() or str(base_out / "Unlim")
    lines = [f"DISK_DIR={disk_value}", f"UNLIM_DIR={unlim_value}"]
    (target / ".env").write_text("\n".join(lines) + "\n", encoding="utf-8")
    entry = {
        "id": pid,
        "name": clean,
        "dir": str(target),
        "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    data["profiles"].append(entry)
    _write_registry(data)
    return {**entry, "active": False, "exists": True}


def rename_profile(pid: str, name: str) -> dict:
    data = _read_registry()
    item = _find(data, pid)
    if item is None:
        raise ValueError("Профиль не найден.")
    if item.get("main"):
        raise ValueError("Основной профиль переименовать нельзя.")
    clean = validate_name(name)
    if _name_taken(clean, skip_id=pid):
        raise ValueError("Профиль с таким именем уже есть.")
    item["name"] = clean
    _write_registry(data)
    return item


def delete_profile(pid: str) -> dict:
    """Удаляет профиль вместе с его папкой данных.

    Основной профиль и профиль, из которого сейчас работает программа,
    удалить нельзя; каталог за пределами `profiles/` — тоже.
    """
    data = _read_registry()
    item = _find(data, pid)
    if item is None:
        raise ValueError("Профиль не найден.")
    if item.get("main"):
        raise ValueError("Основной профиль удалить нельзя.")
    if _same_dir(item["dir"], paths.DATA_DIR):
        raise ValueError("Сейчас работаете в этом профиле — сначала переключитесь.")
    root = profiles_root().resolve()
    target = Path(item["dir"]).resolve()
    if root != target and root not in target.parents:
        raise ValueError("Папка профиля лежит вне каталога профилей — удалять не буду.")
    if target.is_dir():
        shutil.rmtree(target, ignore_errors=True)
    data["profiles"] = [p for p in data["profiles"] if p["id"] != pid]
    _write_registry(data)
    return item


def profile_dir(pid: str) -> Path:
    """Каталог данных профиля для перезапуска приложения."""
    data = _read_registry()
    item = _find(data, pid)
    if item is None:
        raise ValueError("Профиль не найден.")
    target = Path(item["dir"])
    if not target.is_dir():
        # Профиль есть в реестре, а папки нет: создаём пустую, чтобы программа
        # не потеряла путь при перезапуске.
        target.mkdir(parents=True, exist_ok=True)
    return target
