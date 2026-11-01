# Development

Install and run the suite with the steps in [CONTRIBUTING.md](../CONTRIBUTING.md).
This page maps the Python modules those steps exercise and follows one archive
run from the action inputs to the files on disk.

## Module map

| Piece | File | Role |
|:---|:---|:---|
| Action | [`action.yml`](../action.yml) | Composite action. Puts the `token` input in `GITHUB_TOKEN` and runs `python -m traffic_archive.cli`. |
| CLI | [`src/traffic_archive/cli.py`](../src/traffic_archive/cli.py) | Parses arguments, archives each repository, and writes files. The `traffic-archive` command calls `cli.main`. |
| API client | [`src/traffic_archive/api.py`](../src/traffic_archive/api.py) | Standard-library client for the traffic endpoints and repository listing. |
| Doctor | [`src/traffic_archive/doctor.py`](../src/traffic_archive/doctor.py) | `--check` diagnostics. Probes the token and prints what to fix. |
| Merge | [`src/traffic_archive/merge.py`](../src/traffic_archive/merge.py) | Combines a fresh window with stored history. A fresh daily value replaces a stored one. Referrers and paths stay dated snapshots. |

[`src/traffic_archive/__init__.py`](../src/traffic_archive/__init__.py) holds the package version.

## One archive run

This is the path for `traffic-archive --repos owner/repo --out traffic`. The
action builds the same command: `action.yml` exports `PYTHONPATH` to `src`,
sets `GITHUB_TOKEN` from the `token` input, and passes `--out` plus `--repos`
when that input is set.

1. `cli.main` reads the token from `--token` or `GITHUB_TOKEN`, and the
   repository list from `--repos`. `parse_repo_arg` keeps entries shaped like
   `owner/name`. (`--owner` takes the other branch and calls
   `api.owned_repos` before the steps below. `--check` calls `doctor.run` and
   returns before any archive file is written.)
2. For each repository, `archive_repo` fetches four payloads before it writes:
   `api.views`, `api.clones`, `api.referrers`, and `api.paths`. Each helper
   calls `api._request`, which GETs `https://api.github.com` with `urllib`.
   A `TrafficError` from any of those calls skips that repository. Files
   already on disk stay as they were, and the loop continues with the next
   repository.
3. Views and clones go through `merge.merge_timeseries`, which keeps the fresh
   row for a timestamp that already exists, then `merge.to_csv_rows`.
   Referrers and paths go through `merge.append_snapshot`, which replaces a
   snapshot whose `taken_on` is today's UTC date.
4. `archive_repo` writes under `traffic/owner__repo/` (`/` in the repository
   name becomes `__`):

   - `views.json` and `views.csv`
   - `clones.json` and `clones.csv`
   - `referrers.json`
   - `paths.json`

5. `cli.main` prints that repository's view count, clone count, and number of
   archived view days. Those figures come from `merge.totals`.

## Which test to open

| Behaviour | Start here | What stands in for GitHub |
|:---|:---|:---|
| CLI, output files, and a failed repository | [`tests/test_cli.py`](../tests/test_cli.py) | The `fake_api` fixture replaces `api.views`, `clones`, `referrers`, and `paths` with synthetic rows. |
| HTTP client and response shape | [`tests/test_api.py`](../tests/test_api.py) | Tests replace `urllib.request.urlopen` or `api._request`. |
| Token diagnostics (`--check`) | [`tests/test_doctor.py`](../tests/test_doctor.py) | Tests replace `doctor._probe`. |
| Merge and snapshot rules | [`tests/test_merge.py`](../tests/test_merge.py) | Tests call `merge` directly with in-memory rows. |

## Run one test file with fake responses

From the repository root, after the setup in
[CONTRIBUTING.md](../CONTRIBUTING.md):

```bash
python -m pytest tests/test_cli.py
```

That file supplies its own traffic rows through `fake_api` and passes
`--token dummy-token` into `cli.main`. `test_missing_token_is_rejected`
clears `GITHUB_TOKEN` for that case. A real token is unused. The same command
form runs `tests/test_api.py`, `tests/test_doctor.py`, or `tests/test_merge.py`.
