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

## Contributors wanted

New contributors are welcome. If you want a small, well-scoped place to
start, browse the [`good first issue` backlog](https://github.com/HafidIdrissi/github-traffic-archive/labels/good%20first%20issue)
and comment on an issue before beginning so that work is not duplicated.

- [Contribution guide](CONTRIBUTING.md)
- [All open issues](https://github.com/HafidIdrissi/github-traffic-archive/issues)
- [Ask a question](https://github.com/HafidIdrissi/github-traffic-archive/discussions)

Small, focused pull requests are easiest to review. Every pull request runs the
test suite on Linux and Windows with the oldest and newest supported Python
versions.

## Quick start

Add `.github/workflows/traffic.yml`:

```yaml
name: Archive traffic

on:
  schedule:
    - cron: "17 3 * * *"   # daily, before the oldest day falls out of the window
  workflow_dispatch:

permissions:
  contents: write       # to commit the archive back
  id-token: write       # optional: sign attestations (see below)
  attestations: write   # optional: store them

jobs:
  archive:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - uses: HafidIdrissi/github-traffic-archive@v1
        with:
          token: ${{ secrets.TRAFFIC_TOKEN }}
          owner: ${{ github.repository_owner }}

      # Optional but recommended: prove the archive is workflow output, not a
      # hand edit. Requires a public repository. See "What this archive proves".
      - uses: actions/attest-build-provenance@v4
        with:
          subject-path: traffic/**/*.json

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

**Classic PAT with the `repo` scope is the configuration I have verified**
returns `200` on this endpoint. If you want it working in one attempt, use that.

A fine-grained PAT is narrower and should work, but GitHub's documentation
describes the requirement only as "write access" without naming the
fine-grained permission, so I will not put a specific name here that I have not
confirmed. Instead, ask GitHub — see below.

### Ask GitHub what it wants

```bash
traffic-archive --check --repos owner/repo --token YOUR_TOKEN
```

This writes nothing. It reports whether the token is valid, whether it can see
the repository, and whether traffic is readable — and on a `403` from a
fine-grained token it prints GitHub's own `X-Accepted-GitHub-Permissions`
header, which names the exact permission required.

```
owner/repo
  Token authenticates as: someone
  Token type: classic or OAuth
  Scopes: gist, read:org, repo, workflow
  Repository visible: owner/repo
  Traffic readable: yes (14 days returned)

All good — archiving will work with this token.
```

The three failure modes look identical from the outside and need completely
different fixes — an invalid token, a repository the token cannot see, and a
missing permission. `--check` tells them apart.

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

The CLI also works without Actions. Install the latest stable tag directly
from GitHub:

```bash
python -m pip install "git+https://github.com/HafidIdrissi/github-traffic-archive.git@v1"
traffic-archive --help
```

Then provide a token through the environment or with `--token`:

```bash
# Bash
export GITHUB_TOKEN=ghp_...

# diagnose the token first — writes nothing
traffic-archive --check --repos owner/repo

# one or more repositories
traffic-archive --repos owner/repo,owner/other

# or everything you own, forks excluded
traffic-archive --owner your-username --out traffic
```

PowerShell uses `$env:GITHUB_TOKEN = "github_pat_..."`; the remaining commands
are unchanged.

Standard library only — nothing to install beyond the package itself, and no
transitive dependency can break your scheduled run.

## What this archive proves — and what it doesn't

Worth being precise about, because an archive of numbers invites the question.

**The archive is an attestation, not a proof.** The traffic API is readable
only by repository admins, so no third party can ever check your numbers
against the source — and after fourteen days GitHub deletes the source, making
the archive the only witness. A lone witness is not evidence, and a JSON file
in a repo can be edited by anyone with push access.

What the scheduled workflow does about it: on every run that changes the
archive, it signs a [build provenance attestation](https://docs.github.com/en/actions/security-for-github-actions/using-artifact-attestations)
via Sigstore **before** committing. Anyone can then verify that a given archive
file is the untouched output of this workflow, at a named commit of this code,
at a signed timestamp:

```bash
gh attestation verify traffic/owner__repo/views.json --repo owner/archive-repo
```

If the file was hand-edited after archiving, verification fails.

The precise claim this supports: *"this file was produced by that public,
auditable code, from GitHub's API response, at that time."* What it still
cannot prove is that GitHub's API returned truthful numbers — but note the
symmetry: the data comes from GitHub and the attestation infrastructure is
GitHub's too. When the source is a private, self-deleting third party, reducing
the trust base to that one party is the theoretical best. No traffic tool can
do better, and any that claims to is wrong.

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
