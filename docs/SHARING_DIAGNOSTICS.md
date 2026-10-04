# Sharing Diagnostics Safely

When sharing traffic archive data in a bug report, review the data first to make sure it does not contain information that should remain private.

## Fields to review

### Repository names

Repository names and slugs can reveal private or internal projects. Replace real private repository names with fictional examples when they are not necessary to reproduce the problem.

### Paths

Popular paths may contain project names, usernames, internal endpoints, or other information that should not be shared publicly.

Review paths before including them in an issue or attaching an archive.

### Referrers

Referrer data can reveal where traffic originated and may contain private URLs or other identifying information.

Review referrers before sharing an archive publicly.

### Page titles

Page titles may contain internal project names or other information that should not be published.

Review page titles before including traffic data in a bug report.

## Creating a minimal reproduction

When possible, create a small fictional example that demonstrates the problem without using real private data.

For referrer and path snapshots, the archive uses a `taken_on` date and a list of `rows`. A representative fictional snapshot is:

```json
{
  "taken_on": "2026-01-15",
  "rows": [
    {
      "path": "/fictional/project",
      "title": "Example Project",
      "count": 12,
      "uniques": 8
    }
  ]
}
```

Replace repository names, paths, titles, and other identifying values with fictional names while preserving the shape of the data and the values needed to reproduce the problem.

 Daily views and clones contain per-day unique counts; summing them does not give distinct visitors across the archive. Popular paths and referrers describe the API's rolling 14-day window and are stored as dated snapshots, not daily measurements. Aggregate traffic does not provide individual visitor identities.

Prefer:

- fictional repository names
- synthetic URLs
- fictional paths
- fictional page titles
- small datasets containing only the values needed to reproduce the problem

Do not include:

- GitHub tokens or other credentials
- private traffic archives
- private repository information
- unrelated production data

Related planned work includes [issue #54](https://github.com/HafidIdrissi/github-traffic-archive/issues/54) and [issue #33](https://github.com/HafidIdrissi/github-traffic-archive/issues/33). These issues are not existing artifacts; they describe future work around bug reporting and synthetic fixtures.

## Reproduction essentials

Include enough information for someone else to reproduce the problem without exposing private data.

Include:

- the project version
- the command used, with credentials replaced by placeholders
- the expected result
- the actual result
- a short sanitized error message, if applicable

For example:

```text
Version: traffic-archive 1.0.0
Command: traffic-archive --repos example-owner/example-repo --token <REDACTED>
Expected: the archive completes successfully
Actual: the archive exits with an error
Error: HTTP 403: access denied
```

Never include a real token or other credential in a bug report.

## Before posting a bug report

Check that:

- [ ] Repository names are safe to share
- [ ] Paths have been reviewed for private information
- [ ] Referrers have been reviewed
- [ ] Page titles have been reviewed
- [ ] No tokens or credentials are included
- [ ] The reproduction uses fictional data where possible
- [ ] Only the data needed to demonstrate the problem is included
- [ ] Version and reproduction command are included
- [ ] Expected and actual results are described
- [ ] Errors have been sanitized