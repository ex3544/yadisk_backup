"""Проверка скачанных файлов: на месте ли они и совпадают ли с базой.

Зачем отдельный модуль: раньше проверка жила только в `main.py verify` — по
командной строке и только для обычного Диска. Теперь её можно запустить из
окна, она покрывает обе базы и умеет останавливаться на середине.

Проверка читает базу и диск, ничего не меняет: сбойный статус файлов не
перезаписывается — это решает человек по отчёту.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Iterable, Optional

import paths
from utils import compute_md5, file_size_safe, human_bytes, human_duration

# Как часто писать статус: читать его будет веб-панель раз в 1,5 секунды,
# чаще писать на диск незачем.
STATUS_EVERY = 25
# Сколько проблемных путей хранить в статусе и отчёте. Для разбора хватает,
# а километровый список в JSON только тормозит интерфейс.
PROBLEMS_LIMIT = 500


@dataclass
class VerifyCounts:
    total: int = 0
    checked: int = 0
    ok: int = 0
    missing: int = 0
    size: int = 0
    md5: int = 0
    errors: int = 0
    bytes_checked: int = 0
    problems: list = field(default_factory=list)
    started_at: float = 0.0
    finished_at: Optional[float] = None

    def as_dict(self, *, active: bool, scope: str, deep: bool) -> dict:
        return {
            "active": active,
            "scope": scope,
            "deep": deep,
            "total": self.total,
            "checked": self.checked,
            "ok": self.ok,
            "missing": self.missing,
            "size": self.size,
            "md5": self.md5,
            "errors": self.errors,
            "bytes_checked": self.bytes_checked,
            "problems": self.problems[:PROBLEMS_LIMIT],
            "problems_total": len(self.problems),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "updated_at": time.time(),
        }


def check_one(
    local_path: str,
    expected_size: Optional[int],
    expected_md5: Optional[str],
    *,
    deep: bool = False,
) -> tuple[str, str]:
    """Проверяет один файл. Возвращает (статус, пояснение).

    Статусы: ok | missing | size | md5 | error. Ошибка чтения — «error»,
    а не «md5»: возможно, диск отключился, и виноват не файл.
    """
    size = file_size_safe(local_path)
    if size is None:
        return "missing", "файла нет по указанному пути"
    if expected_size is not None and size != expected_size:
        return "size", f"размер {size}, в базе {expected_size}"
    if deep and expected_md5:
        try:
            actual = compute_md5(local_path)
        except OSError as e:
            return "error", f"не удалось прочитать: {e}"
        if actual.lower() != str(expected_md5).lower():
            return "md5", f"MD5 {actual}, в базе {expected_md5}"
    return "ok", ""


def _disk_rows() -> Iterable[tuple[str, str, Optional[int], Optional[str]]]:
    """(remote_path, local_path, size, md5) скачанных файлов обычного Диска."""
    if not paths.DB_FILE.exists():
        return []
    con = sqlite3.connect(f"file:{paths.DB_FILE}?mode=ro", uri=True, timeout=30)
    con.row_factory = sqlite3.Row
    try:
        rows = list(
            con.execute(
                "SELECT remote_path, local_path, size, md5 FROM files "
                "WHERE status IN ('downloaded', 'skipped') "
                "AND local_path IS NOT NULL"
            )
        )
    finally:
        con.close()
    return [
        (r["remote_path"], r["local_path"], r["size"], r["md5"]) for r in rows
    ]


def _unlim_rows() -> Iterable[tuple[str, str, Optional[int], Optional[str]]]:
    """(file_id, local_path, size, md5) скачанных файлов Безлимита."""
    if not paths.UNLIM_DB_FILE.exists():
        return []
    con = sqlite3.connect(f"file:{paths.UNLIM_DB_FILE}?mode=ro", uri=True, timeout=30)
    con.row_factory = sqlite3.Row
    try:
        rows = list(
            con.execute(
                "SELECT file_id, name, local_path, size, md5 FROM unlim_files "
                "WHERE status = 'downloaded' AND local_path IS NOT NULL"
            )
        )
    finally:
        con.close()
    return [
        (r["file_id"] or r["name"] or "", r["local_path"], r["size"], r["md5"])
        for r in rows
    ]


def _write_status(data: dict) -> None:
    try:
        path = paths.VERIFY_STATUS_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except Exception:  # noqa: BLE001 — статус не должен ронять проверку
        pass


def read_status() -> dict:
    """Последний статус проверки. Пустой словарь, если проверок не было."""
    try:
        return json.loads(paths.VERIFY_STATUS_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def run(
    scope: str = "disk",
    *,
    deep: bool = False,
    limit: Optional[int] = None,
    on_progress=None,
) -> dict:
    """Проверяет скачанные файлы выбранной базы. Возвращает итоговый статус.

    `deep=True` дополнительно пересчитывает MD5 — это читает весь объём
    с диска, поэтому по умолчанию сверяется только размер.
    """
    scope = (scope or "disk").strip().lower()
    if scope not in ("disk", "unlim", "both"):
        raise ValueError(f"неизвестная база: {scope}")

    rows: list[tuple[str, str, Optional[int], Optional[str]]] = []
    if scope in ("disk", "both"):
        rows += _disk_rows()
    if scope in ("unlim", "both"):
        rows += _unlim_rows()
    if limit:
        rows = rows[: int(limit)]

    counts = VerifyCounts(total=len(rows), started_at=time.time())
    _write_status(counts.as_dict(active=True, scope=scope, deep=deep))

    for i, (remote, local, size, md5) in enumerate(rows, start=1):
        status, detail = check_one(local, size, md5, deep=deep)
        counts.checked = i
        if status == "ok":
            counts.ok += 1
            counts.bytes_checked += int(size or 0)
        elif status == "missing":
            counts.missing += 1
        elif status == "size":
            counts.size += 1
        elif status == "md5":
            counts.md5 += 1
        else:
            counts.errors += 1
        if status != "ok":
            counts.problems.append(
                {"path": remote, "local": local, "status": status, "detail": detail}
            )
        if on_progress is not None:
            on_progress(counts)
        if i % STATUS_EVERY == 0 or i == len(rows):
            _write_status(counts.as_dict(active=True, scope=scope, deep=deep))

    counts.finished_at = time.time()
    final = counts.as_dict(active=False, scope=scope, deep=deep)
    _write_status(final)
    return final


def render_report(data: dict) -> str:
    """Читаемый отчёт для окна и файла. Без путей-ссылок и секретов."""
    lines = [
        "# Проверка файлов",
        "",
        f"- База: {data.get('scope')}",
        f"- Режим: {'размер и MD5' if data.get('deep') else 'размер'}",
        f"- Проверено: {data.get('checked', 0)} из {data.get('total', 0)}",
        f"- В порядке: {data.get('ok', 0)}",
        f"- Нет на диске: {data.get('missing', 0)}",
        f"- Размер не совпал: {data.get('size', 0)}",
        f"- MD5 не совпал: {data.get('md5', 0)}",
        f"- Не удалось прочитать: {data.get('errors', 0)}",
        f"- Проверено байт: {human_bytes(int(data.get('bytes_checked') or 0))}",
    ]
    started = data.get("started_at") or 0
    finished = data.get("finished_at")
    if started and finished:
        lines.append(f"- Время: {human_duration(finished - started)}")
    problems = data.get("problems") or []
    if problems:
        lines += ["", "## Проблемные файлы", ""]
        for p in problems:
            lines.append(f"- `{p.get('path')}` — {p.get('detail')}")
        total = int(data.get("problems_total") or 0)
        if total > len(problems):
            lines.append(f"- … и ещё {total - len(problems)}")
    lines.append("")
    lines.append("Скачанные файлы не удалены и статусы в базе не менялись.")
    return "\n".join(lines)
