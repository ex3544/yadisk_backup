"""Перепривязка баз к новой папке и возврат структуры облака.

Зачем отдельный модуль: человек переносит скачанное на другой диск, меняет
букву диска или раскладывает файлы по полкам. База помнит старые адреса, и
проверка честно пишет «файла нет», хотя файл на месте. Здесь мы ищем файлы по
новой папке и обновляем адреса, ничего не скачивая и не удаляя.

Правила:
  * сначала пробуем точный путь «новый корень + хвост старого»;
  * если не вышло — ищем файл по имени и размеру в подпапках (это лечит
    сортировку и переименование вида `файл_1.jpg`);
  * одинаковые имена и размеры разбираем по MD5, но только их;
  * файлы на отключённом диске — не «пропали», а «диск не подключён».
"""

from __future__ import annotations

import os
import re
import shutil
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Iterable, Optional

import paths
from utils import compute_md5, resolve_local_path, volume_ready

SCOPES = ("disk", "unlim")
DB_TABLES = {"disk": "files", "unlim": "unlim_files"}
DB_KEYS = {"disk": "remote_path", "unlim": "file_id"}

# Хвост, который organize добавляет при совпадении имён: `фото_1.jpg`.
_SUFFIX_RE = re.compile(r"_\d+$")


class RelocateError(ValueError):
    """Понятная человеку причина, почему перепривязка невозможна."""


@dataclass
class Row:
    key: str
    local: str
    size: Optional[int]
    md5: Optional[str]
    name: str
    remote: str = ""


@dataclass
class Plan:
    scope: str
    old_root: str
    new_root: str
    total: int = 0
    already: int = 0
    by_path: int = 0
    by_name: int = 0
    by_md5: int = 0
    missing: int = 0
    offline: int = 0
    conflicts: int = 0
    updates: list = field(default_factory=list)
    missing_items: list = field(default_factory=list)
    offline_items: list = field(default_factory=list)
    conflict_items: list = field(default_factory=list)
    missing_keys: list = field(default_factory=list)
    offline_keys: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "scope": self.scope,
            "old_root": self.old_root,
            "new_root": self.new_root,
            "total": self.total,
            "already": self.already,
            "by_path": self.by_path,
            "by_name": self.by_name,
            "by_md5": self.by_md5,
            "missing": self.missing,
            "offline": self.offline,
            "conflicts": self.conflicts,
            "retied": self.by_path + self.by_name + self.by_md5,
            "missing_items": self.missing_items[:200],
            "offline_items": self.offline_items[:200],
            "conflict_items": self.conflict_items[:200],
            "missing_keys": self.missing_keys,
            "offline_keys": self.offline_keys,
        }


def relative_under(path: str, root: str) -> Optional[str]:
    """Путь относительно root или None, если path лежит вне root."""
    if not path or not root:
        return None
    try:
        rel = os.path.relpath(path, root)
    except (OSError, ValueError):
        return None
    if rel == os.curdir:
        return ""
    if rel == os.pardir or rel.startswith(os.pardir + os.sep):
        return None
    return rel


def _norm(path: str) -> str:
    return os.path.normcase(os.path.normpath(str(path)))


def _name_key(name: str) -> str:
    base, ext = os.path.splitext(name.casefold())
    return (_SUFFIX_RE.sub("", base) + ext).casefold()


def _common_root(paths_list: Iterable[str]) -> str:
    items = [os.path.dirname(p) for p in paths_list if p]
    if not items:
        return ""
    try:
        return os.path.commonpath(items)
    except (OSError, ValueError):
        return ""


def _old_root_candidates(rows: list[Row], hint: str = "") -> list[str]:
    out: list[str] = []
    for cand in (hint, _common_root([r.local for r in rows])):
        if cand and cand not in out:
            out.append(cand)
    # Смешанные корни: общий путь мог схлопнуться до диска. Добавим корни по
    # каждой букве диска — их немного, а точная проверка отсеет лишнее.
    by_drive: dict[str, list[str]] = defaultdict(list)
    for r in rows:
        if r.local:
            drive, _ = os.path.splitdrive(os.path.abspath(r.local))
            by_drive[drive.casefold()].append(r.local)
    for group in by_drive.values():
        root = _common_root(group)
        if root and root not in out:
            out.append(root)
    return out[:6]


def _score_root(rows: list[Row], old_root: str, new_root: str) -> int:
    """Сколько файлов найдётся по точному пути от этого старого корня."""
    if not old_root:
        return -1
    score = 0
    for row in rows:
        rel = relative_under(row.local, old_root)
        if rel is None:
            continue
        cand = os.path.join(new_root, rel) if rel else new_root
        if os.path.isfile(cand):
            score += 1
    return score


def detect_old_root(rows: list[Row], new_root: str, hint: str = "") -> str:
    candidates = _old_root_candidates(rows, hint)
    best, best_score = (hint or ""), -1
    for cand in candidates:
        score = _score_root(rows, cand, new_root)
        if score > best_score:
            best, best_score = cand, score
    return best


def load_rows(scope: str) -> list[Row]:
    """Скачанные файлы выбранной базы с непустым локальным путём."""
    scope = (scope or "").strip().lower()
    if scope not in SCOPES:
        raise RelocateError("Неизвестная база: ожидается disk или unlim")
    db_file = paths.DB_FILE if scope == "disk" else paths.UNLIM_DB_FILE
    if not db_file.exists():
        return []
    con = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True, timeout=30)
    con.row_factory = sqlite3.Row
    try:
        if scope == "disk":
            rows = list(con.execute(
                "SELECT remote_path, local_path, size, md5 FROM files "
                "WHERE status IN ('downloaded', 'skipped') "
                "AND local_path IS NOT NULL AND local_path <> ''"
            ))
            return [
                Row(key=r["remote_path"], remote=r["remote_path"],
                    local=r["local_path"], size=r["size"], md5=r["md5"],
                    name=os.path.basename(str(r["local_path"]).replace("/", os.sep)))
                for r in rows
            ]
        rows = list(con.execute(
            "SELECT file_id, name, local_path, size, md5 FROM unlim_files "
            "WHERE status = 'downloaded' "
            "AND local_path IS NOT NULL AND local_path <> ''"
        ))
        return [
            Row(key=r["file_id"], remote=r["name"] or r["file_id"],
                local=r["local_path"], size=r["size"], md5=r["md5"],
                name=r["name"] or os.path.basename(str(r["local_path"]).replace("/", os.sep)))
            for r in rows
        ]
    finally:
        con.close()


class _Index:
    """Быстрый поиск файлов по имени и размеру внутри новой папки."""

    def __init__(self, root: str):
        self.root = root
        self.by_name: dict[tuple[str, Optional[int]], list[str]] = defaultdict(list)
        self.by_norm: dict[tuple[str, Optional[int]], list[str]] = defaultdict(list)
        self._md5: dict[str, Optional[str]] = {}
        self._built = False

    def build(self) -> None:
        if self._built:
            return
        self._built = True
        for dirpath, _dirs, files in os.walk(self.root):
            for name in files:
                full = os.path.join(dirpath, name)
                try:
                    size = os.path.getsize(full)
                except OSError:
                    continue
                self.by_name[(name.casefold(), size)].append(full)
                self.by_norm[(_name_key(name), size)].append(full)

    def find(self, name: str, size: Optional[int],
             md5: Optional[str]) -> tuple[Optional[str], str]:
        self.build()
        key = (name.casefold(), size)
        cands = self.by_name.get(key) or []
        if not cands:
            cands = self.by_norm.get((_name_key(name), size)) or []
        if len(cands) == 1:
            return cands[0], "name"
        if len(cands) > 1 and md5:
            wanted = str(md5).lower()
            matches = []
            for cand in cands:
                if cand not in self._md5:
                    try:
                        self._md5[cand] = compute_md5(cand).lower()
                    except OSError:
                        self._md5[cand] = None
                if self._md5[cand] == wanted:
                    matches.append(cand)
            if len(matches) == 1:
                return matches[0], "md5"
        if len(cands) > 1:
            return None, "conflict"
        return None, ""


def plan_relocate(
    scope: str,
    new_root: str,
    old_root_hint: str = "",
    only_under: str = "",
) -> Plan:
    """Предпросмотр: что будет привязано к новой папке. Ничего не меняет.

    `only_under` — фильтр «Старая папка»: если задан, в план попадают только
    строки, чей текущий `local_path` лежит внутри этой папки. Так переезд
    одного диска не трогает строки других дисков и старые Linux-пути.
    """
    scope = (scope or "").strip().lower()
    if scope not in SCOPES:
        raise RelocateError("Неизвестная база: ожидается disk или unlim")
    new_root = os.path.abspath((new_root or "").strip())
    if not os.path.isdir(new_root):
        raise RelocateError(
            "Новая папка не найдена или диск не подключён: " + new_root
        )
    rows = load_rows(scope)
    only_under = os.path.abspath((only_under or "").strip()) if only_under else ""
    if only_under:
        rows = [r for r in rows if relative_under(r.local, only_under) is not None]
    plan = Plan(scope=scope, old_root="", new_root=new_root, total=len(rows))
    if not rows:
        return plan
    old_root = only_under or detect_old_root(rows, new_root, old_root_hint)
    plan.old_root = old_root
    index: Optional[_Index] = None

    for row in rows:
        local = row.local
        # Файл уже на месте — не трогаем, даже если лежит на старом диске.
        if os.path.isfile(local):
            plan.already += 1
            continue

        exact = None
        rel = relative_under(local, old_root) if old_root else None
        if rel is not None:
            cand = os.path.join(new_root, rel) if rel else new_root
            if os.path.isfile(cand):
                exact = cand
        if exact:
            plan.by_path += 1
            plan.updates.append((row.key, exact))
            continue

        if index is None:
            index = _Index(new_root)
        found, how = index.find(row.name, row.size, row.md5)
        if found:
            if how == "md5":
                plan.by_md5 += 1
            else:
                plan.by_name += 1
            plan.updates.append((row.key, found))
            continue

        item = {"path": row.remote, "local": local,
                "size": row.size, "name": row.name}
        if how == "conflict":
            plan.conflicts += 1
            plan.conflict_items.append(item)
        elif volume_ready(local):
            plan.missing += 1
            plan.missing_items.append(item)
            plan.missing_keys.append(row.key)
        else:
            plan.offline += 1
            plan.offline_items.append(item)
            plan.offline_keys.append(row.key)
    return plan


def apply_relocate(plan: Plan) -> int:
    """Записывает найденные пути в базу. Возвращает число обновлённых строк."""
    if not plan.updates:
        return 0
    db_file = paths.DB_FILE if plan.scope == "disk" else paths.UNLIM_DB_FILE
    table = DB_TABLES[plan.scope]
    key_col = DB_KEYS[plan.scope]
    con = sqlite3.connect(db_file, timeout=30)
    try:
        with con:
            con.executemany(
                f"UPDATE {table} SET local_path = ? WHERE {key_col} = ?",
                [(new, key) for key, new in plan.updates],
            )
        return len(plan.updates)
    finally:
        con.close()


def requeue_missing(scope: str, keys: Iterable[str]) -> int:
    """Возвращает отсутствующие файлы в очередь: pending и без локального пути."""
    scope = (scope or "").strip().lower()
    if scope not in SCOPES:
        raise RelocateError("Неизвестная база: ожидается disk или unlim")
    keys = [str(k) for k in keys if k]
    if not keys:
        return 0
    db_file = paths.DB_FILE if scope == "disk" else paths.UNLIM_DB_FILE
    table = DB_TABLES[scope]
    key_col = DB_KEYS[scope]
    if not db_file.exists():
        return 0
    con = sqlite3.connect(db_file, timeout=30)
    try:
        updated = 0
        with con:
            for start in range(0, len(keys), 400):
                chunk = keys[start:start + 400]
                marks = ",".join("?" * len(chunk))
                if scope == "disk":
                    cur = con.execute(
                        f"UPDATE {table} SET status = 'pending', local_path = NULL, "
                        f"error = NULL, downloaded_at = NULL, attempts = 0, "
                        f"retry_after = NULL, server_fails = 0 "
                        f"WHERE {key_col} IN ({marks})",
                        chunk,
                    )
                else:
                    cur = con.execute(
                        f"UPDATE {table} SET status = 'pending', local_path = NULL, "
                        f"error = NULL, downloaded_at = NULL, attempts = 0, "
                        f"worker_id = NULL, assigned_at = NULL "
                        f"WHERE {key_col} IN ({marks})",
                        chunk,
                    )
                updated += max(0, cur.rowcount)
        return updated
    finally:
        con.close()


# ── Возврат структуры облака (обратная операция organize) ───────────────────


@dataclass
class RestorePlan:
    scope: str
    root: str
    total: int = 0
    already: int = 0
    moves: int = 0
    missing: int = 0
    offline: int = 0
    conflicts: int = 0
    updates: list = field(default_factory=list)      # (key, src, dst)
    missing_items: list = field(default_factory=list)
    offline_items: list = field(default_factory=list)
    conflict_items: list = field(default_factory=list)
    missing_keys: list = field(default_factory=list)
    offline_keys: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "scope": self.scope,
            "root": self.root,
            "total": self.total,
            "already": self.already,
            "moves": self.moves,
            "missing": self.missing,
            "offline": self.offline,
            "conflicts": self.conflicts,
            "missing_items": self.missing_items[:200],
            "offline_items": self.offline_items[:200],
            "conflict_items": self.conflict_items[:200],
        }


def _disk_target(root: str, remote_path: str) -> str:
    return resolve_local_path(root, remote_path)


def plan_restore_cloud(scope: str = "disk", root: str = "") -> RestorePlan:
    """План возврата файлов Диска в папки, как они называются в облаке."""
    scope = (scope or "disk").strip().lower()
    if scope != "disk":
        raise RelocateError("Вернуть структуру облака можно только для Диска")
    if not root:
        root = _read_dirs().get("disk_dir") or str(paths.DEFAULT_DISK_DIR)
    root = os.path.abspath(root)
    plan = RestorePlan(scope=scope, root=root)
    if not os.path.isdir(root):
        raise RelocateError("Папка Диска не найдена или диск не подключён: " + root)
    rows = load_rows("disk")
    plan.total = len(rows)
    if not rows:
        return plan
    index = _Index(root)
    planned: set[str] = set()

    for row in rows:
        target = _disk_target(root, row.remote)
        nt = _norm(target)
        if os.path.isfile(row.local) and _norm(row.local) == nt:
            plan.already += 1
            continue
        src = row.local if os.path.isfile(row.local) else ""
        if not src:
            found, _how = index.find(row.name, row.size, row.md5)
            src = found or ""
        if not src:
            item = {"path": row.remote, "local": row.local,
                    "size": row.size, "name": row.name}
            if volume_ready(row.local):
                plan.missing += 1
                plan.missing_items.append(item)
            else:
                plan.offline += 1
                plan.offline_items.append(item)
            continue
        if _norm(src) == nt:
            plan.already += 1
            continue
        if nt in planned or os.path.exists(target):
            # В облаке два пути после очистки имён совпали, либо цель занята.
            # Ничего не перезаписываем: такие случаи человек разберёт по списку.
            plan.conflicts += 1
            plan.conflict_items.append(
                {"path": row.remote, "local": src, "target": target,
                 "size": row.size, "name": row.name}
            )
            continue
        planned.add(nt)
        plan.moves += 1
        plan.updates.append((row.key, src, target))
    return plan


def apply_restore_cloud(plan: RestorePlan) -> tuple[int, list[str]]:
    """Переносит файлы по плану и обновляет базу. Возвращает (сколько, ошибки)."""
    if not plan.updates:
        return 0, []
    errors: list[str] = []
    done: list[tuple[str, str]] = []
    for key, src, dst in plan.updates:
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            if os.path.exists(dst):
                raise OSError("цель уже существует")
            shutil.move(src, dst)
            done.append((key, dst))
        except OSError as exc:
            errors.append(f"{os.path.basename(src)}: {exc}")
    if done:
        con = sqlite3.connect(paths.DB_FILE, timeout=30)
        try:
            with con:
                con.executemany(
                    "UPDATE files SET local_path = ? WHERE remote_path = ?",
                    [(dst, key) for key, dst in done],
                )
        finally:
            con.close()
        _cleanup_empty_dirs(plan.root)
    return len(done), errors


def _cleanup_empty_dirs(root: str) -> int:
    removed = 0
    for dirpath, _dirs, _files in os.walk(root, topdown=False):
        if _norm(dirpath) == _norm(root):
            continue
        try:
            if not os.listdir(dirpath):
                os.rmdir(dirpath)
                removed += 1
        except OSError:
            pass
    return removed


def _read_dirs() -> dict:
    """Папки выгрузки из .env, без импорта веб-слоя."""
    out = {}
    try:
        for line in paths.ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key.strip() in ("DISK_DIR", "DOWNLOAD_DIR", "UNLIM_DIR"):
                out[key.strip().lower()] = value.strip().strip('"').strip("'")
    except OSError:
        pass
    disk = out.get("disk_dir") or out.get("download_dir")
    return {"disk_dir": disk, "unlim_dir": out.get("unlim_dir")}
