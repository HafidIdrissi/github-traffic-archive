# Contributing

Thanks for looking. Small, focused pull requests are easiest to review.

## Choose an issue

Start with the current
[`good first issue` backlog](https://github.com/HafidIdrissi/github-traffic-archive/labels/good%20first%20issue)
rather than the larger feature requests. Comment on the issue before starting
so another contributor does not work on the same change. If the expected
behaviour is unclear, ask there before writing code.

## Getting set up

```bash
git clone https://github.com/YOUR-USERNAME/github-traffic-archive.git
cd github-traffic-archive
git remote add upstream https://github.com/HafidIdrissi/github-traffic-archive.git
python -m pip install --upgrade pytest
python -m pytest
```

The package uses the standard library only. Please keep it that way: a
scheduled workflow that breaks because of a transitive release is worse than a
few extra lines of `urllib`.

Before opening a pull request, create a focused branch from the latest
`upstream/main` and run the complete test suite. Behaviour changes need a test
that fails before the fix and passes afterwards. Draft pull requests are
welcome when you want early feedback.

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

## Pull requests

Keep one problem per pull request and complete the pull request template. Do
not include generated traffic archives, tokens, editor settings, or unrelated
formatting changes. A maintainer may ask for a smaller change when a review
would otherwise cover several independent behaviours.
