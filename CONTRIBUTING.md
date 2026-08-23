# Contributing

Thanks for looking. Small, focused pull requests are easiest to review.

## Getting set up

```bash
git clone https://github.com/HafidIdrissi/github-traffic-archive
cd github-traffic-archive
python -m pytest          # no dependencies beyond pytest
```

The package uses the standard library only. Please keep it that way: a
scheduled workflow that breaks because of a transitive release is worse than a
few extra lines of `urllib`.

## Good first issues

- Output formats — SQLite, Parquet, or a summary Markdown table
- A chart generator for the archived series
- Better handling of very large owners (hundreds of repositories)
- Documentation, especially on fine-grained PAT permissions

## What to keep in mind

- **Never invent data.** Referrers and paths have no per-day breakdown, so they
  are stored as dated snapshots rather than merged into a series. Any change
  that interpolates or estimates will be declined.
- **Merging rules are load-bearing.** `merge.py` decides what happens when a
  day appears twice; its tests document why the fresh value wins. Change the
  tests in the same commit as the behaviour, and say why.
- **Failures should be partial, not total.** One repository the token cannot
  read must not abandon the rest of the run.

## Commits

Explain why, not just what. If you fixed something subtle, the commit message
is the right place to record how you found it.
