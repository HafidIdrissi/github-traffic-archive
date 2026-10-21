import csv
import io

import pytest

from traffic_archive.merge import (
    append_snapshot, merge_timeseries, to_csv_rows, totals,
)


def day(ts, count, uniques):
    return {"timestamp": ts, "count": count, "uniques": uniques}


# 2**53 is the first integer a double cannot hold exactly, so a count above it
# is the sharpest probe for arithmetic that detours through floating point.
LARGE = 2**53 + 1
LARGER = 2**53 + 3


def assert_exact_int(value, expected):
    """A count no float can hold must arrive as that same integer.

    Equality alone would not notice a float that happens to be exactly
    representable (`9007199254740994.0 == 9007199254740994` is true), so the
    type is checked too: an archive of integers stays integer arithmetic.
    """
    assert isinstance(value, int) and not isinstance(value, bool)
    assert value == expected


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


def test_csv_rows_keep_counts_above_two_to_the_fifty_third_exact():
    """The archived number must be the number GitHub reported, digit for digit."""
    rows = to_csv_rows([day("2026-01-01T00:00:00Z", LARGE, LARGER)])
    assert rows[1] == ["2026-01-01", "9007199254740993", "9007199254740995"]


def test_csv_rows_write_a_zero_count_as_zero():
    """A day with no traffic is a real value, not a missing one."""
    rows = to_csv_rows([day("2026-01-01T00:00:00Z", 0, 0)])
    assert rows[1] == ["2026-01-01", "0", "0"]


def test_csv_round_trip_preserves_large_counts_and_zero_days():
    """Round-tripping the file must not change a single digit."""
    series = [
        day("2026-01-01T00:00:00Z", LARGE, 0),
        day("2026-01-02T00:00:00Z", 0, LARGER),
    ]
    buffer = io.StringIO()
    csv.writer(buffer).writerows(to_csv_rows(series))
    text = buffer.getvalue()

    assert "e+" not in text.lower(), "a count was written in exponent form"
    rows = list(csv.reader(io.StringIO(text)))
    assert rows[0] == ["date", "count", "uniques"]
    assert rows[1] == ["2026-01-01", "9007199254740993", "0"]
    assert rows[2] == ["2026-01-02", "0", "9007199254740995"]
    assert_exact_int(int(rows[1][1]), LARGE)
    assert_exact_int(int(rows[2][2]), LARGER)


def test_totals_sum_large_counts_without_float_rounding():
    """A total above 2**53 is still an integer total, not a rounded double."""
    series = [
        day("2026-01-01T00:00:00Z", LARGE, 0),
        day("2026-01-02T00:00:00Z", 0, LARGER),
        day("2026-01-03T00:00:00Z", 2, 2),
    ]
    t = totals(series)

    assert t["days"] == 3
    assert_exact_int(t["count"], LARGER)
    assert_exact_int(t["uniques"], LARGER + 2)
    # No double holds these exactly, so a float round trip would change them.
    assert float(t["count"]) != t["count"]
    assert float(t["uniques"]) != t["uniques"]


def test_merge_keeps_large_counts_exact_and_prefers_the_fresh_day():
    """Storing and re-reading a huge count must not shift it by a single unit."""
    stored = [day("2026-01-01T00:00:00Z", LARGE, 0)]
    incoming = [
        day("2026-01-01T00:00:00Z", LARGER, 0),
        day("2026-01-02T00:00:00Z", 0, LARGE),
    ]
    merged = merge_timeseries(stored, incoming)

    assert_exact_int(merged[0]["count"], LARGER)
    assert_exact_int(merged[1]["count"], 0)
    assert_exact_int(merged[1]["uniques"], LARGE)
    assert_exact_int(totals(merged)["count"], LARGER)
