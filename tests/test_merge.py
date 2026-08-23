import pytest

from traffic_archive.merge import (
    append_snapshot, merge_timeseries, to_csv_rows, totals,
)


def day(ts, count, uniques):
    return {"timestamp": ts, "count": count, "uniques": uniques}


def test_fresh_value_wins_for_a_repeated_day():
    """A day's count grows until it closes in UTC, so the newer read is correct."""
    stored = [day("2026-01-01T00:00:00Z", 3, 2)]
    incoming = [day("2026-01-01T00:00:00Z", 9, 4)]
    merged = merge_timeseries(stored, incoming)
    assert len(merged) == 1
    assert merged[0]["count"] == 9
    assert merged[0]["uniques"] == 4


def test_history_older_than_the_window_survives():
    """The whole point: days the API no longer returns must not be dropped."""
    stored = [day("2025-11-01T00:00:00Z", 5, 1)]
    incoming = [day("2026-01-01T00:00:00Z", 2, 1)]
    merged = merge_timeseries(stored, incoming)
    assert [m["timestamp"] for m in merged] == [
        "2025-11-01T00:00:00Z", "2026-01-01T00:00:00Z",
    ]


def test_output_is_sorted_regardless_of_input_order():
    merged = merge_timeseries(
        [day("2026-01-03T00:00:00Z", 1, 1)],
        [day("2026-01-02T00:00:00Z", 1, 1), day("2026-01-01T00:00:00Z", 1, 1)],
    )
    assert [m["timestamp"][:10] for m in merged] == [
        "2026-01-01", "2026-01-02", "2026-01-03",
    ]


def test_entries_without_a_timestamp_are_ignored():
    merged = merge_timeseries([{"count": 1}], [day("2026-01-01T00:00:00Z", 2, 1)])
    assert len(merged) == 1


def test_merging_is_idempotent():
    incoming = [day("2026-01-01T00:00:00Z", 4, 2)]
    once = merge_timeseries([], incoming)
    twice = merge_timeseries(once, incoming)
    assert once == twice


def test_totals_sums_days_and_counts():
    t = totals([day("2026-01-01T00:00:00Z", 3, 2), day("2026-01-02T00:00:00Z", 4, 1)])
    assert t == {"days": 2, "count": 7, "uniques": 3}


def test_same_day_snapshot_replaces_rather_than_appends():
    """An hourly schedule must not inflate the referrers file."""
    first = append_snapshot([], [{"referrer": "google.com", "count": 4}], "2026-01-01")
    second = append_snapshot(first, [{"referrer": "google.com", "count": 9}], "2026-01-01")
    assert len(second) == 1
    assert second[0]["rows"][0]["count"] == 9


def test_snapshots_from_different_days_accumulate_in_order():
    snaps = append_snapshot([], [{"referrer": "a"}], "2026-01-02")
    snaps = append_snapshot(snaps, [{"referrer": "b"}], "2026-01-01")
    assert [s["taken_on"] for s in snaps] == ["2026-01-01", "2026-01-02"]


def test_csv_has_a_header_and_iso_dates():
    rows = to_csv_rows([day("2026-01-01T00:00:00Z", 3, 2)])
    assert rows[0] == ["date", "count", "uniques"]
    assert rows[1] == ["2026-01-01", "3", "2"]
