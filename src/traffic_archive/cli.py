"""Command line entry point."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import errno
import json
import os
import stat
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

from . import __version__, api, doctor
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


_POSIX_ACL = "system.posix_acl_access"


def _read_posix_acl(file) -> bytes | None:
    try:
        return os.getxattr(file, _POSIX_ACL)
    except OSError as error:
        if error.errno == errno.ENODATA:
            return None
        # Includes ENOTSUP: the filesystem may use another ACL model (NFSv4, CIFS).
        raise


def _copy_linux_access(fd: int, path: Path, previous: os.stat_result) -> None:
    """Give the empty staging file the archive's owner, group, mode and POSIX ACL."""
    expected = (previous.st_uid, previous.st_gid, stat.S_IMODE(previous.st_mode))
    try:
        acl = _read_posix_acl(path)
        staged = os.fstat(fd)
        if (staged.st_uid, staged.st_gid) != expected[:2]:
            os.fchown(fd, previous.st_uid, previous.st_gid)
        if acl is not None:
            os.setxattr(fd, _POSIX_ACL, acl)
        elif _read_posix_acl(fd) is not None:
            os.removexattr(fd, _POSIX_ACL)  # inherited from a default ACL on the directory
        # After chown, which can clear set-id bits.
        os.fchmod(fd, expected[2])
        staged = os.fstat(fd)
        copied = (staged.st_uid, staged.st_gid, stat.S_IMODE(staged.st_mode)) == expected
        copied = copied and _read_posix_acl(fd) == acl
    except OSError as error:
        raise PermissionError(
            f"Cannot preserve the access policy of {path}; left it unchanged: {error}"
        ) from error
    if not copied:
        raise PermissionError(f"Access policy of {path} did not copy exactly; left it unchanged.")


def _report_cleanup_error(path: Path, error: OSError) -> None:
    try:
        print(f"Could not remove temporary archive file {path}: {error}", file=sys.stderr)
    except (OSError, ValueError):
        pass  # The error that caused the cleanup matters more than this message.


@contextmanager
def _replace_atomically(path: Path, newline: str | None = None):
    """Stage beside the archive and publish it only after the write and close succeed (Linux)."""
    target = Path(os.path.realpath(path))  # Keep following a symlinked archive, as before.
    try:
        previous = target.stat()
    except FileNotFoundError:
        previous = None
    if previous is not None and not stat.S_ISREG(previous.st_mode):
        raise OSError(f"Cannot replace {target}: not a regular file.")
    if previous is not None and not os.access(target, os.W_OK):
        raise PermissionError(errno.EACCES, "Archive is not writable", str(target))

    # mkstemp creates the file owner-only, so new archives stay owner-only.
    fd, name = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
    temp = Path(name)
    stream = None
    try:
        if previous is not None:
            _copy_linux_access(fd, target, previous)
        stream = os.fdopen(fd, "w", encoding="utf-8", newline=newline)
        yield stream
        stream.close()
        os.replace(temp, target)
    except BaseException:
        try:
            if stream is None:
                os.close(fd)
            else:
                stream.close()
        except OSError:
            pass  # The staging file is being discarded either way.
        try:
            temp.unlink()
        except FileNotFoundError:
            pass
        except OSError as error:
            _report_cleanup_error(temp, error)
        raise


# Only Linux replaces atomically for now (#14). Windows and macOS keep updating in
# place until their access policies can be preserved on a new file.
def _write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    if sys.platform == "linux":
        with _replace_atomically(path) as fh:
            fh.write(text)
    else:
        path.write_text(text, encoding="utf-8")


def _write_csv(path: Path, rows: list[list[str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform == "linux":
        opened = _replace_atomically(path, newline="")
    else:
        opened = path.open("w", encoding="utf-8", newline="")
    with opened as fh:
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
