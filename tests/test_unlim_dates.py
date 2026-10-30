"""Календарь дат Безлимита: кластеры, диапазоны дней, выборка и счётчики."""

import datetime as dt
import sqlite3

import pytest

import unlim_dates


def _ms(year, month, day, hour=10, minute=0, second=0):
    return int(dt.datetime(year, month, day, hour, minute, second).timestamp() * 1000)


def cid(first: dt.datetime, last: dt.datetime) -> str:
    return f"{int(first.timestamp() * 1000):019d}_{int(last.timestamp() * 1000):019d}"


@pytest.fixture()
def db(tmp_path):
    path = tmp_path / "unlim_state.db"
    con = sqlite3.connect(path)
    con.execute(
        """
        CREATE TABLE unlim_files (
            file_id TEXT PRIMARY KEY, cluster_id TEXT, name TEXT, size INTEGER,
            status TEXT DEFAULT 'pending'
        )
        """
    )
    may1 = cid(dt.datetime(2024, 5, 1, 10), dt.datetime(2024, 5, 1, 12))
    # Кластер через полночь: считается днём начала, 2 мая.
    night = cid(dt.datetime(2024, 5, 2, 23, 50), dt.datetime(2024, 5, 3, 0, 20))
    may2 = cid(dt.datetime(2024, 5, 2, 12), dt.datetime(2024, 5, 2, 13))
    june = cid(dt.datetime(2024, 6, 1, 9), dt.datetime(2024, 6, 1, 9))
    con.executemany(
        "INSERT INTO unlim_files (file_id, cluster_id, name, size, status) VALUES (?,?,?,?,?)",
        [
            ("1", may1, "a.jpg", 10, "downloaded"),
            ("2", may1, "b.jpg", 20, "pending"),
            ("3", night, "c.jpg", 30, "pending"),
            ("4", may2, "d.jpg", 40, "downloaded"),
            ("5", june, "e.jpg", 50, "skipped"),
            ("6", None, "no-date.jpg", 60, "pending"),
        ],
    )
    con.commit()
    con.close()
    return path


# ── формат cluster_id ───────────────────────────────────────────────────────


def test_cluster_id_is_parsed_and_validated():
    text = "0000001105196016000_0000001105198719000"
    assert unlim_dates.parse_cluster_id(text) == (1105196016000, 1105198719000)
    assert unlim_dates.cluster_start_ms(text) == 1105196016000
    for bad in ("", None, "короткий", "0" * 19 + "-" + "0" * 19,
                "x" * 19 + "_" + "0" * 19):
        assert unlim_dates.parse_cluster_id(bad) is None


def test_cluster_id_order_is_chronological():
    old = cid(dt.datetime(2020, 1, 1), dt.datetime(2020, 1, 2))
    new = cid(dt.datetime(2024, 1, 1), dt.datetime(2024, 1, 2))
    assert old < new


# ── диапазоны дней ──────────────────────────────────────────────────────────


def test_merge_dates_joins_neighbours_and_drops_junk():
    assert unlim_dates.merge_dates(
        ["2024-05-03", "2024-05-01", "2024-05-02", "мусор", "2024-13-01", "2024-05-01"]
    ) == [["2024-05-01", "2024-05-03"]]


def test_expand_and_clean_ranges_roundtrip():
    days = ["2024-05-01", "2024-05-02", "2024-05-05"]
    ranges = unlim_dates.merge_dates(days)
    assert ranges == [["2024-05-01", "2024-05-02"], ["2024-05-05", "2024-05-05"]]
    assert unlim_dates.expand_ranges(ranges) == days
    assert unlim_dates.clean_ranges([["2024-05-05", "2024-05-01"], "плохо", ranges[0]]) \
        == [["2024-05-01", "2024-05-02"]]


def test_month_helpers():
    assert unlim_dates.normalize_month("2024-05") == "2024-05"
    assert unlim_dates.normalize_month("2024-13") == dt.date.today().strftime("%Y-%m")
    assert unlim_dates.shift_month("2024-01", -1) == "2023-12"
    assert unlim_dates.shift_month("2024-12", 1) == "2025-01"


def test_empty_selection_matches_nothing():
    clause, params = unlim_dates.range_clause([])
    assert clause == "1 = 0"
    assert params == []


# ── выборка по дням ─────────────────────────────────────────────────────────


def test_sql_filter_takes_only_selected_days(db):
    con = sqlite3.connect(db)
    clause, params = unlim_dates.range_clause([["2024-05-02", "2024-05-02"]])
    rows = con.execute(
        f"SELECT file_id FROM unlim_files WHERE {clause} ORDER BY file_id", params
    ).fetchall()
    con.close()
    # 3 — кластер, начавшийся 2 мая в 23:50; 4 — днём 2 мая.
    assert [r[0] for r in rows] == ["3", "4"]


def test_midnight_start_belongs_to_its_own_day(db):
    con = sqlite3.connect(db)
    clause, params = unlim_dates.range_clause([["2024-05-03", "2024-05-03"]])
    rows = con.execute(f"SELECT file_id FROM unlim_files WHERE {clause}", params).fetchall()
    con.close()
    # Кластер, начавшийся 2 мая, в третий день не попадает.
    assert rows == []


# ── календарь и счётчики ────────────────────────────────────────────────────


def test_calendar_counts_days_and_statuses(db):
    data = unlim_dates.calendar(db, "2024-05")
    by_day = {item["date"]: item for item in data["days"]}
    assert set(by_day) == {"2024-05-01", "2024-05-02"}

    first = by_day["2024-05-01"]
    assert (first["files"], first["downloaded"], first["left"], first["clusters"]) == (2, 1, 1, 1)

    second = by_day["2024-05-02"]
    # Кластер «через полночь» и обычный дневной — два кластера, но день начала один.
    assert (second["files"], second["downloaded"], second["left"], second["clusters"]) == (2, 1, 1, 2)

    assert data["totals"]["files"] == 4
    assert data["totals"]["bytes"] == 100


def test_calendar_ignores_files_without_a_date(db):
    data = unlim_dates.calendar(db, "2024-05")
    assert all(item["files"] for item in data["days"])
    assert data["totals"]["files"] == 4       # файл без cluster_id не в календаре


def test_calendar_is_empty_for_a_quiet_month(db):
    data = unlim_dates.calendar(db, "2023-01")
    assert data["month"] == "2023-01"
    assert data["days"] == []
    assert data["totals"]["files"] == 0


def test_calendar_survives_a_missing_database(tmp_path):
    data = unlim_dates.calendar(tmp_path / "нет.db", "2024-05")
    assert data["days"] == []
    assert data["month"] == "2024-05"


def test_selection_stats_count_only_selected_days(db):
    stats = unlim_dates.selection_stats(db, [["2024-05-01", "2024-05-01"]])
    assert stats["days"] == 1
    assert (stats["files"], stats["downloaded"], stats["left"], stats["clusters"]) == (2, 1, 1, 1)
    assert stats["bytes"] == 30

    both = unlim_dates.selection_stats(db, [["2024-05-01", "2024-05-02"]])
    assert both["days"] == 2
    assert (both["files"], both["downloaded"], both["left"]) == (4, 2, 2)

    nothing = unlim_dates.selection_stats(db, [])
    assert nothing["days"] == 0 and nothing["files"] == 0
