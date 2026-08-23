# Security

## Reporting a vulnerability

Please report privately to **idrissihafez@gmail.com** rather than opening a
public issue. I will acknowledge within a few days.

## Things worth knowing

This action handles a token with **push access** to your repositories. That is
a requirement of GitHub's traffic API, not a choice made here — read-only
tokens are rejected by the endpoint with `403`.

Consequences to weigh before installing it:

- Store the token as a repository or organisation **secret**. Never inline it.
- A classic PAT with the `repo` scope is the configuration currently verified
  to work. A fine-grained PAT is narrower, but GitHub does not consistently
  expose the required permission name. Run `traffic-archive --check` against
  each target repository before storing a fine-grained token as a secret; when
  GitHub supplies `X-Accepted-GitHub-Permissions`, the command reports it.
- The token is passed to the action as an input and used only as a `Bearer`
  header against `api.github.com`. It is never written to the archive, logged,
  or sent anywhere else.
- Pull requests from forks do not receive your secrets, so a fork cannot exfil
  the token through this workflow.

The archived output contains only aggregate counts. It holds no identities, IP
addresses, or anything else that could identify a visitor — GitHub does not
expose that data in the first place.
