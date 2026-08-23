# github-traffic-archive

**GitHub keeps your traffic data for 14 days, then deletes it. This keeps it.**

[![Tests](https://img.shields.io/github/actions/workflow/status/HafidIdrissi/github-traffic-archive/tests.yml?branch=main&label=tests)](https://github.com/HafidIdrissi/github-traffic-archive/actions/workflows/tests.yml)
[![License](https://img.shields.io/github/license/HafidIdrissi/github-traffic-archive?color=6366F1)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.9%2B-3776AB?logo=python&logoColor=white)](pyproject.toml)
[![No dependencies](https://img.shields.io/badge/dependencies-none-3DDC97)](pyproject.toml)

Views, clones, referrers and popular paths are only available for a rolling
two-week window. Whatever you don't capture is gone permanently — there is no
export, no archive, and no way to ask for it later. Run this on a schedule and
your history accumulates in your own repository as JSON and CSV.

No third-party service, no tracking pixel, no account. The data never leaves
GitHub and your repo.

## Quick start

Add `.github/workflows/traffic.yml`:

```yaml
name: Archive traffic

on:
  schedule:
    - cron: "17 3 * * *"   # daily, before the oldest day falls out of the window
  workflow_dispatch:

permissions:
  contents: write   # to commit the archive back

jobs:
  archive:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - uses: HafidIdrissi/github-traffic-archive@v1
        with:
          token: ${{ secrets.TRAFFIC_TOKEN }}
          owner: ${{ github.repository_owner }}

      - name: Commit if anything changed
        run: |
          git config user.name  "github-actions[bot]"
          git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
          git add traffic
          git diff --staged --quiet || git commit -m "chore: archive traffic $(date -u +%F)"
          git push
```

## The token

**This is the part that trips people up. You need a personal access token, and
the built-in `GITHUB_TOKEN` will not work — not even for the repository the
workflow is running in.**

That is not a permissions mistake you can configure around. The traffic
endpoint requires *administration* access, and `administration` is not a
grantable key in the Actions `permissions:` block; adding it makes GitHub
reject the workflow with a `422`. `contents: write` grants commit access, not
traffic access, and the endpoint answers `403`.

Both of those were verified by this repository's own archive workflow failing
that way, first with `403` and then with `422`.

So: create a token, store it as a secret, pass it in.

| Token type | What to grant |
|---|---|
| Classic PAT | the `repo` scope |
| Fine-grained PAT | **Administration: read** on the repositories you want, and nothing else |

The fine-grained option is the narrower of the two and is what I would use.

## What you get

```
traffic/
└── owner__repo/
    ├── views.json       merged daily series, oldest to newest
    ├── views.csv        same, as date,count,uniques
    ├── clones.json
    ├── clones.csv
    ├── referrers.json   dated snapshots — see below
    └── paths.json
```

## How merging works

When the same date appears in both your archive and a fresh response, **the
fresh value wins**. A day's count keeps growing until that day closes in UTC, so
a number read at 08:00 is a lower bound on the same day read at 23:00. Keeping
the older value would permanently understate every day you archived more than
once.

Referrers and paths are **not** a time series — the API reports the top ten over
the trailing fourteen days with no per-day breakdown. Merging them would invent
data, so they are stored as dated snapshots instead. Re-running on the same date
replaces that date's snapshot rather than appending a near-duplicate.

## Reading `uniques` honestly

`uniques` is summed per day, which is what GitHub reports. It is **not** a count
of distinct people over the period: someone who visits on three days is counted
three times. Treat it as daily reach, not audience size. No tool can give you
the latter from this API, and any that claims to is guessing.

## Command line

Works without Actions:

```bash
export GITHUB_TOKEN=ghp_...

# one or more repositories
traffic-archive --repos owner/repo,owner/other

# or everything you own, forks excluded
traffic-archive --owner your-username --out traffic
```

Standard library only — nothing to install beyond the package itself, and no
transitive dependency can break your scheduled run.

## Limits worth knowing before you rely on it

- **It cannot backfill.** History starts the day you first run it. Everything
  before that is already deleted.
- **It cannot tell you who visited.** GitHub exposes no identities, and neither
  does this. If you want that, no tool can honestly provide it.
- **Repository traffic is not profile traffic.** Views of
  `github.com/you/you` are not views of `github.com/you`. GitHub publishes no
  metric for the latter; any "profile views" badge is a third-party pixel
  counting proxy fetches.
- **A gap in your schedule is a gap in your data.** If the workflow is broken
  for more than fourteen days, those days are unrecoverable.

## Contributing

Issues and pull requests welcome — bug reports, documentation, and support for
other output formats are all good places to start. `CONTRIBUTING.md` has the
details; the test suite runs with `python -m pytest`.

## Licence

MIT. See [LICENSE](LICENSE).
