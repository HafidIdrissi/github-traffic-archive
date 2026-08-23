# Changelog

## 1.1.0

- `--check` diagnoses a token without writing anything, and surfaces GitHub's
  own `X-Accepted-GitHub-Permissions` header so nobody has to guess which
  permission the traffic endpoint wants
- README no longer names a fine-grained permission it could not verify, and
  leads with the classic `repo` scope, which is confirmed working

## 1.0.0

First release.

- Archives views, clones, referrers and popular paths
- Merges each fresh 14-day window into accumulated history, newest value
  winning for a repeated day
- Stores referrers and paths as dated snapshots, since the API gives them no
  per-day breakdown
- Writes JSON and CSV side by side
- Discovers every non-fork repository for an owner, or takes an explicit list
- Continues past repositories the token cannot read, and reports them
- Standard library only
