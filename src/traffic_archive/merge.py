"""Merging rules for GitHub traffic data.

The traffic API returns a rolling fourteen-day window. Archiving it correctly
turns on one question: when the same date appears in both the stored history
and a fresh response, which one wins?

The fresh one, always. A day's count keeps growing until that day closes in
UTC, so a value fetched at 08:00 is a lower bound on the same day's value
fetched at 23:00. Keeping the older number would permanently understate every
day the archive was written more than once.

Referrers and paths are not a time series. The API reports the top ten over the
trailing fourteen days with no per-day breakdown, so those are stored as dated
snapshots and never merged.
"""

from __future__ import annotations

from typing import Any, Iterable

# Keys the traffic API uses for the daily series.
TIMESTAMP = "timestamp"
COUNT = "count"
UNIQUES = "uniques"


def merge_timeseries(
    stored: Iterable[dict[str, Any]],
    incoming: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Combine stored history with a fresh window, newest value winning.

    Returns entries sorted by timestamp ascending. Input order is irrelevant.

    >>> old = [{"timestamp": "2026-01-01T00:00:00Z", "count": 3, "uniques": 2}]
    >>> new = [{"timestamp": "2026-01-01T00:00:00Z", "count": 9, "uniques": 4}]
    >>> merge_timeseries(old, new)[0]["count"]
    9
    """
    by_day: dict[str, dict[str, Any]] = {}
    for entry in stored:
        ts = entry.get(TIMESTAMP)
        if ts:
            by_day[ts] = dict(entry)
    for entry in incoming:
        ts = entry.get(TIMESTAMP)
        if ts:
            by_day[ts] = dict(entry)
    return [by_day[ts] for ts in sorted(by_day)]


def totals(series: Iterable[dict[str, Any]]) -> dict[str, int]:
    """Sum a merged series.

    `uniques` is summed per day, which is what the API reports. It is not a
    count of distinct people across the whole period: someone who visits on
    three days is counted three times. Treat it as daily reach, not audience.
    """
    days = list(series)
    return {
        "days": len(days),
        "count": sum(int(d.get(COUNT, 0)) for d in days),
        "uniques": sum(int(d.get(UNIQUES, 0)) for d in days),
    }


def append_snapshot(
    stored: Iterable[dict[str, Any]],
    rows: list[dict[str, Any]],
    taken_on: str,
) -> list[dict[str, Any]]:
    """Record a dated snapshot of referrers or paths, replacing same-day runs.

    Re-running on a date overwrites that date's snapshot rather than appending
    a near-duplicate, so an hourly schedule does not inflate the file.
    """
    kept = [s for s in stored if s.get("taken_on") != taken_on]
    kept.append({"taken_on": taken_on, "rows": rows})
    return sorted(kept, key=lambda s: s["taken_on"])


def to_csv_rows(series: Iterable[dict[str, Any]]) -> list[list[str]]:
    """Flatten a merged series into CSV rows, header first."""
    out = [["date", "count", "uniques"]]
    for day in series:
        ts = str(day.get(TIMESTAMP, ""))
        out.append([ts[:10], str(day.get(COUNT, 0)), str(day.get(UNIQUES, 0))])
    return out
