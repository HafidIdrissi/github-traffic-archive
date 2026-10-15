# Interpreting Archived Totals

All dates, times, and counts below are fictional. They illustrate the archive's
arithmetic, not actual GitHub traffic.

The CLI's view and clone counts sum all archived daily rows. GitHub Insights
shows a rolling window, so its total can cover fewer dates. Compare the same
repository and metric using the same UTC date boundaries and collection time.
An Insights page and the API are not guaranteed to refresh together; the
side-by-side readings below are a fictional example, not a promise of an exact
live match.

## A fictional daily comparison

Each UTC date runs from 00:00:00 inclusive to the next 00:00:00 exclusive. In
this example, the fresh 14-date window is **2026-10-07 through 2026-10-20**,
inclusive. The archive has an older 2026-10-01 row. Each of the twelve dates
from 2026-10-07 through 2026-10-18 explicitly has **0 views and 0 daily
uniques** in both fictional collections; the table groups those twelve rows
for readability. Dates 2026-10-02 through 2026-10-06 are absent from the
archive. Their values are unknown and are not included in any total.

An earlier collection on **2026-10-20 at 09:00 UTC** left the archive in the
state shown below. A fresh window arrives at **2026-10-20 18:00 UTC**. Compare
the fresh window and the updated archive in the two 18:00 columns; each cell
is **views / daily uniques**.

| UTC date | Archive at 09:00 | Fresh window at 18:00 | Archive after merge at 18:00 |
|:---|---:|---:|---:|
| 2026-10-01 | 10 / 7 | Outside window | 10 / 7 |
| 2026-10-07 through 2026-10-18 (each of 12 dates) | 0 / 0 | 0 / 0 | 0 / 0 |
| 2026-10-19 | 20 / 12 | 20 / 12 | 20 / 12 |
| 2026-10-20 | 8 / 4 | 6 / 3 | 6 / 3 |
| **Totals** | **38 / 23 (15 dates)** | **26 / 15 (14 dates)** | **36 / 22 (15 dates)** |

The fresh 2026-10-20 row **replaces** the stored row; it is not added to it.
The newest reading wins even though this fictional correction goes down from
8 to 6 views and from 4 to 3 daily uniques. The older 2026-10-01 row remains
in the archive because it is outside the fresh window. At 18:00, its 10 views
explain the difference between the 36 archived views and the 26 fresh-window
views. The same daily merge rule applies to clones. The CLI reports the
merged view count; daily uniques are included here to explain the data, not
as another CLI total.

The archive's **22 daily uniques** are 7 + 12 + 3, with zeros on the other
shown dates. This sum does not identify 22 distinct people across the archive:
one person visiting on multiple dates can contribute to each date's uniques.
Neither this sum nor the fresh window's 15 daily uniques is a distinct-visitor
total, and the aggregate rows cannot reveal that total.

## Referrers and paths are snapshots

Referrers and popular paths each report a top ten for a trailing window, not
daily measurements. For the same fictional UTC window, suppose a referrer row
has a count of 3 in a **2026-10-20 09:00 UTC** collection and 6 in a
**2026-10-20 18:00 UTC** collection. The second run replaces that date's
snapshot, leaving the count of 6 for 2026-10-20; it does not store 3 + 6.
Popular paths follow the same rule. The snapshot's date says when it was
collected, not which day generated its traffic. Trailing windows overlap, and
top-ten rows are not a complete daily series: do not sum snapshots across
dates or add them to daily view or clone totals.
