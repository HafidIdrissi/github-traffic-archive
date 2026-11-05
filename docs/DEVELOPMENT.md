# Development guide

Start with the existing [setup instructions](../CONTRIBUTING.md#getting-set-up).
This guide shows where to read the code and which tests to run for a focused change.

## Module map

| File | Responsibility |
| --- | --- |
| [action.yml](../action.yml) | Composite action entry point. Passes the token through `GITHUB_TOKEN`, sets `PYTHONPATH` to the action's `src` directory, and translates action inputs into `python -m traffic_archive.cli` arguments. |
| [cli.py](../src/traffic_archive/cli.py) | Parses and validates arguments, selects repositories, dispatches diagnostics or archiving, and reads and writes archive files. `main()` coordinates the run; `archive_repo()` handles one repository. |
| [api.py](../src/traffic_archive/api.py) | Uses `urllib` to make authenticated requests, decode JSON, retry selected rate-limit and network failures, and report HTTP errors. Provides repository discovery and the four traffic helpers, which validate response containers. |
| [doctor.py](../src/traffic_archive/doctor.py) | Implements `--check`. Probes token identity, repository visibility, and daily views, then reports access and permission diagnostics without writing an archive. |
| [merge.py](../src/traffic_archive/merge.py) | Transforms data without network or file access: merges daily series, calculates totals, records snapshots, and produces CSV rows. |

## One archive invocation

Consider a normal archive run with `--repos example/project --out traffic` and a token supplied through `GITHUB_TOKEN`.
The action reaches the same Python entry point as the installed `traffic-archive` command.

1. `cli.main()` parses the arguments. `parse_repo_arg()` validates `owner/name` entries and removes duplicates while preserving their order. An explicit repository list takes precedence over `--owner`; otherwise `api.owned_repos()` discovers repositories and excludes forks unless requested. Missing token or repository-selection arguments are rejected.
2. `--check` would call `doctor.run()` and return before archiving. For this normal run, `main()` computes today's UTC date once and calls `archive_repo()` for each selected repository.
3. `archive_repo()` maps `example/project` to `traffic/example__project/`. It collects `api.views()`, `api.clones()`, `api.referrers()`, and `api.paths()` in that order before writing any files. Views and clones use daily aggregation by default. A fetch raising `TrafficError` leaves that repository's archive unchanged; `main()` reports the failure and continues to other repositories.
4. For views, `_read_json()` loads `views.json`, using an empty list if it does not exist. Invalid JSON raises `SystemExit` rather than overwriting that file. `merge_timeseries()` indexes stored and incoming rows by timestamp, lets the incoming row replace an overlapping timestamp, retains older history, and returns timestamp-sorted rows. The fresh reading wins even if its count is lower.
5. `_write_json()` writes the merged views to `views.json`. `to_csv_rows()` supplies a `date,count,uniques` header and rows to `_write_csv()` for `views.csv`. `totals()` calculates the summary from the merged history. The same sequence then produces `clones.json` and `clones.csv`.
6. Referrers and paths are trailing-window top-ten snapshots, with no daily breakdown. `append_snapshot()` stores each response under `taken_on` and `rows`, replacing a snapshot taken on the same UTC date. These produce `referrers.json` and `paths.json`, with no CSV equivalents. `archive_repo()` returns the views/clones summary for `main()` to print.

Writes currently go directly to destination files: JSON uses `Path.write_text()`, and CSV uses `Path.open("w")`.
Files are updated sequentially, so a later read or write failure can leave earlier files updated.
Temporary-file replacement and atomic writes are pending work in [#14](https://github.com/HafidIdrissi/github-traffic-archive/issues/14).

## Tests to start with

| Test file | What it covers |
| --- | --- |
| [tests/test_cli.py](../tests/test_cli.py) | Argument validation, diagnostic dispatch, archive JSON/CSV output, preservation of history, and fetch-failure handling. Start with `test_archive_writes_json_and_csv_per_metric` and `test_fetch_failure_preserves_existing_archive`. |
| [tests/test_api.py](../tests/test_api.py) | Request paths, aggregation options, response containers, HTTP errors, retries, and repository discovery with fake responses. |
| [tests/test_doctor.py](../tests/test_doctor.py) | Diagnostic probe order, response validation, header casing, and permission advice with mocked probes or `urlopen` responses. |
| [tests/test_merge.py](../tests/test_merge.py) | Fresh-value replacement, retained history, sorting, totals, snapshot replacement, and CSV conversion. |

After following the setup instructions, run this focused check from the repository root:

```bash
python -m pytest tests/test_cli.py
```

No real token is needed for this test file.
Its `fake_api` fixture replaces the four traffic helpers with synthetic responses; other tests replace discovery or diagnostics, or reject invalid arguments before requests.
Tests pass dummy token strings where needed and write through pytest's temporary directories.
The test run does not call the live GitHub API.
