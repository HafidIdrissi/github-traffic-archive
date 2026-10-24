import csv
import io

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


def test_to_csv_rows_preserves_large_integers_and_zeros():
    """Counts and uniques exceeding 2**53 and zero values remain exact decimal strings."""
    large = 2**53 + 1  # 9007199254740993
    larger = 2**53 + 3  # 9007199254740995
    series = [
        day("2026-01-01T00:00:00Z", large, 0),
        day("2026-01-02T00:00:00Z", 0, larger),
    ]
    rows = to_csv_rows(series)
    assert rows == [
        ["date", "count", "uniques"],
        ["2026-01-01", "9007199254740993", "0"],
        ["2026-01-02", "0", "9007199254740995"],
    ]


def test_csv_serialization_round_trip_preserves_large_integers():
    """Writing to CSV and reading back preserves exact decimal representations without float truncation."""
    large_count = 2**53 + 1  # 9007199254740993
    large_uniques = 2**53 + 3  # 9007199254740995
    series = [
        day("2026-01-01T00:00:00Z", large_count, 100),
        day("2026-01-02T00:00:00Z", 0, 0),
        day("2026-01-03T00:00:00Z", 50, large_uniques),
    ]
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerows(to_csv_rows(series))

    buffer.seek(0)
    reader = list(csv.reader(buffer))

    assert reader == [
        ["date", "count", "uniques"],
        ["2026-01-01", "9007199254740993", "100"],
        ["2026-01-02", "0", "0"],
        ["2026-01-03", "50", "9007199254740995"],
    ]


def test_totals_preserves_exact_large_integer_sums():
    """totals() preserves exact integer sums and types without floating-point precision loss."""
    large_a = 2**53 + 1  # 9007199254740993
    large_b = 2**53 + 3  # 9007199254740995
    series = [
        day("2026-01-01T00:00:00Z", large_a, 0),
        day("2026-01-02T00:00:00Z", 1, large_b),
        day("2026-01-03T00:00:00Z", 0, 2),
    ]
    result = totals(series)
    assert result["days"] == 3
    assert result["count"] == 9007199254740994  # (2**53 + 1) + 1
    assert result["uniques"] == 9007199254740997  # (2**53 + 3) + 2
    assert type(result["days"]) is int
    assert type(result["count"]) is int
    assert type(result["uniques"]) is int


def test_merge_timeseries_preserves_large_integers_and_fresh_values():
    """Merging keeps large integers exact and lets fresh values win for repeated dates."""
    large_stored = 2**53 + 1  # 9007199254740993
    large_incoming = 2**53 + 3  # 9007199254740995

    stored = [
        day("2026-01-01T00:00:00Z", large_stored, 10),
        day("2026-01-02T00:00:00Z", large_stored, 5),
    ]
    incoming = [
        day("2026-01-02T00:00:00Z", large_incoming, 0),
        day("2026-01-03T00:00:00Z", 0, large_incoming),
    ]

    merged = merge_timeseries(stored, incoming)

    assert len(merged) == 3
    assert merged[0] == {
        "timestamp": "2026-01-01T00:00:00Z",
        "count": 9007199254740993,
        "uniques": 10,
    }
    assert merged[1] == {
        "timestamp": "2026-01-02T00:00:00Z",
        "count": 9007199254740995,
        "uniques": 0,
    }
    assert merged[2] == {
        "timestamp": "2026-01-03T00:00:00Z",
        "count": 0,
        "uniques": 9007199254740995,
    }

    for entry in merged:
        assert type(entry["count"]) is int
        assert type(entry["uniques"]) is int

    merged_totals = totals(merged)
    assert merged_totals["days"] == 3
    assert merged_totals["count"] == 9007199254740993 + 9007199254740995 + 0
    assert merged_totals["uniques"] == 10 + 0 + 9007199254740995
    assert type(merged_totals["count"]) is int
    assert type(merged_totals["uniques"]) is int
