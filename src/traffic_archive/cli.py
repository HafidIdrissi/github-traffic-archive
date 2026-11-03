"""Command line entry point."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import stat
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

from . import __version__, api, doctor
from ._archive_access import _report_cleanup_error, create_windows_temp, preserve_access
from .merge import append_snapshot, merge_timeseries, to_csv_rows, totals


def parse_repo_arg(repo_str: str) -> list[str]:
    """Validate comma-separated --repos entries match 'owner/name' format."""
    raw_entries = repo_str.split(",")
    if not raw_entries or all(not r.strip() for r in raw_entries):
        raise argparse.ArgumentTypeError("Must provide at least one repository in 'owner/name' format.")

    repos: list[str] = []
    seen: set[str] = set()
    for raw in raw_entries:
        repo = raw.strip()
        parts = repo.split("/")
        if len(parts) != 2 or not parts[0] or not parts[1]:
            raise argparse.ArgumentTypeError(
                f"Invalid repository format '{repo}'. Expected 'owner/name' (e.g. 'owner/repo')."
            )
        if repo not in seen:
            seen.add(repo)
            repos.append(repo)
    return repos


def _slug(repo: str) -> str:
    return repo.replace("/", "__")


def _read_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as err:
        raise SystemExit(f"{path} is not valid JSON ({err}). Refusing to overwrite it.")


@contextmanager
def _atomic_text_writer(path: Path, *, newline: str | None = None):
    """Publish one file after preserving supported access and closing its stream."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        previous = path.lstat()
    except FileNotFoundError:
        previous = None
    if previous is not None and not stat.S_ISREG(previous.st_mode):
        raise OSError(f"Cannot replace a non-regular archive file: {path}")

    if os.name == "nt" and previous is not None:
        fd, temp_path = create_windows_temp(path.parent)
    else:
        fd, name = tempfile.mkstemp(dir=path.parent, prefix=".traffic-archive-", suffix=".tmp")
        temp_path = Path(name)
    stream = None
    try:
        stream = os.fdopen(fd, "w", encoding="utf-8", newline=newline)
        if previous is not None:
            preserve_access(path, temp_path, previous, target_fd=fd)
        yield stream
        stream.close()
        os.replace(temp_path, path)
    except BaseException:
        if stream is None or not stream.closed:
            try:
                if stream is None:
                    os.close(fd)
                else:
                    stream.close()
            except OSError as error:
                _report_cleanup_error(temp_path, error)
        try:
            temp_path.unlink(missing_ok=True)
        except OSError as error:
            _report_cleanup_error(temp_path, error)
        raise


def _write_json(path: Path, payload) -> None:
    with _atomic_text_writer(path) as fh:
        fh.write(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")


def _write_csv(path: Path, rows: list[list[str]]) -> None:
    with _atomic_text_writer(path, newline="") as fh:
        csv.writer(fh).writerows(rows)


def archive_repo(repo: str, token: str, out: Path, today: str) -> dict[str, int]:
    """Fetch one repository's traffic and merge it into the archive."""
    base = out / _slug(repo)

    # A failed fetch must leave this repository's archive unchanged.
    collected = {
        "views": api.views(repo, token),
        "clones": api.clones(repo, token),
        "referrers": api.referrers(repo, token),
        "paths": api.paths(repo, token),
    }

    summary: dict[str, int] = {}
    for metric in ("views", "clones"):
        path = base / f"{metric}.json"
        merged = merge_timeseries(_read_json(path, []), collected[metric])
        _write_json(path, merged)
        _write_csv(base / f"{metric}.csv", to_csv_rows(merged))
        t = totals(merged)
        summary[metric] = t["count"]
        summary[f"{metric}_days"] = t["days"]

    for metric in ("referrers", "paths"):
        path = base / f"{metric}.json"
        snaps = append_snapshot(_read_json(path, []), collected[metric], today)
        _write_json(path, snaps)

    return summary


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="traffic-archive",
        description="Archive GitHub traffic data before the 14-day window discards it.",
    )
    p.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    p.add_argument("--owner", help="Archive every repository owned by this user or org.")
    p.add_argument(
        "--repos",
        type=parse_repo_arg,
        help="Comma-separated owner/name list. Overrides --owner discovery.",
    )
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

    out = Path(args.out)
    if not args.check and out.is_file():
        print(f"--out must be a directory, not a file: {out}", file=sys.stderr)
        return 1

    if args.repos:
        repos = args.repos
    else:
        repos = api.owned_repos(args.owner, args.token, args.include_forks)
        if not repos:
            print(f"No repositories found for {args.owner}.", file=sys.stderr)
            return 1

    if args.check:
        return doctor.run(repos, args.token)

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
