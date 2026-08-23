"""Command line entry point."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import sys
from pathlib import Path

from . import api, doctor
from .merge import append_snapshot, merge_timeseries, to_csv_rows, totals


def _slug(repo: str) -> str:
    return repo.replace("/", "__")


def _read_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as err:
        raise SystemExit(f"{path} is not valid JSON ({err}). Refusing to overwrite it.")


def _write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _write_csv(path: Path, rows: list[list[str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        csv.writer(fh).writerows(rows)


def archive_repo(repo: str, token: str, out: Path, today: str) -> dict[str, int]:
    """Fetch one repository's traffic and merge it into the archive."""
    base = out / _slug(repo)

    summary: dict[str, int] = {}
    for metric, fetch in (("views", api.views), ("clones", api.clones)):
        path = base / f"{metric}.json"
        merged = merge_timeseries(_read_json(path, []), fetch(repo, token))
        _write_json(path, merged)
        _write_csv(base / f"{metric}.csv", to_csv_rows(merged))
        t = totals(merged)
        summary[metric] = t["count"]
        summary[f"{metric}_days"] = t["days"]

    for metric, fetch in (("referrers", api.referrers), ("paths", api.paths)):
        path = base / f"{metric}.json"
        snaps = append_snapshot(_read_json(path, []), fetch(repo, token), today)
        _write_json(path, snaps)

    return summary


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="traffic-archive",
        description="Archive GitHub traffic data before the 14-day window discards it.",
    )
    p.add_argument("--owner", help="Archive every repository owned by this user or org.")
    p.add_argument("--repos", help="Comma-separated owner/name list. Overrides --owner discovery.")
    p.add_argument("--out", default="traffic", help="Output directory (default: traffic).")
    p.add_argument("--include-forks", action="store_true", help="Include forks when using --owner.")
    p.add_argument("--token", default=os.environ.get("GITHUB_TOKEN", ""),
                   help="Token with push access. Defaults to $GITHUB_TOKEN.")
    p.add_argument("--check", action="store_true",
                   help="Diagnose token access and exit without writing anything.")
    args = p.parse_args(argv)

    if not args.token:
        p.error("No token. Pass --token or set GITHUB_TOKEN.")
    if not args.owner and not args.repos:
        p.error("Nothing to archive. Pass --repos or --owner.")

    if args.repos:
        repos = [r.strip() for r in args.repos.split(",") if r.strip()]
    else:
        repos = api.owned_repos(args.owner, args.token, args.include_forks)
        if not repos:
            print(f"No repositories found for {args.owner}.", file=sys.stderr)
            return 1

    if args.check:
        return doctor.run(repos, args.token)

    out = Path(args.out)
    today = dt.datetime.now(dt.timezone.utc).date().isoformat()

    failed: list[tuple[str, str]] = []
    for repo in repos:
        try:
            s = archive_repo(repo, args.token, out, today)
            print(f"  {repo}: {s['views']} views / {s['clones']} clones "
                  f"across {s['views_days']} archived days")
        except api.TrafficError as err:
            # One unreadable repository must not abandon the rest.
            failed.append((repo, str(err)))
            print(f"  {repo}: skipped — {err}", file=sys.stderr)

    print(f"\n{len(repos) - len(failed)}/{len(repos)} archived into {out}/")
    if failed:
        print(f"{len(failed)} skipped; see messages above.", file=sys.stderr)
    return 1 if failed and len(failed) == len(repos) else 0


if __name__ == "__main__":
    raise SystemExit(main())
