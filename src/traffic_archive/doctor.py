"""Diagnose a token before you trust a schedule to it.

The traffic endpoint fails in several ways that all look alike from the outside,
and the remedies are completely different. This asks GitHub directly and says
which one you have.

The useful part is `X-Accepted-GitHub-Permissions`: on a 403, GitHub names the
exact permission it wanted. That beats reading documentation, which describes
the requirement only as "write access" without naming the fine-grained
permission.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from .api import API, UA


def _probe(path: str, token: str) -> tuple[int, dict[str, str], str]:
    """Return status, headers and body without raising, so we can read a 403."""
    req = urllib.request.Request(
        f"{API}{path}",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": UA,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, dict(r.headers), r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read().decode("utf-8", "replace")
    except urllib.error.URLError as e:
        return 0, {}, str(e)


def check(repo: str, token: str) -> tuple[bool, list[str]]:
    """Diagnose access to one repository. Returns (ok, lines to print)."""
    out: list[str] = []

    if not token:
        return False, ["  No token supplied. Pass --token or set GITHUB_TOKEN."]

    # 1. Is the token valid at all, and what kind is it?
    status, headers, body = _probe("/user", token)
    if status == 0:
        return False, [f"  Could not reach {API}: {body}"]
    if status == 401:
        return False, ["  The token is invalid, expired or revoked (401 on /user).",
                       "  Create a new one and update your secret."]
    login = json.loads(body).get("login", "?") if status == 200 else "?"
    scopes = (headers.get("X-OAuth-Scopes") or "").strip()
    kind = "classic or OAuth" if scopes else "fine-grained (or scopeless)"
    out.append(f"  Token authenticates as: {login}")
    out.append(f"  Token type: {kind}")
    if scopes:
        out.append(f"  Scopes: {scopes}")
        if "repo" not in [s.strip() for s in scopes.split(",")]:
            out.append("  -> A classic token needs the `repo` scope for traffic. Yours lacks it.")

    # 2. Can it see the repository at all?
    status, _, _ = _probe(f"/repos/{repo}", token)
    if status == 404:
        out.append(f"  Cannot see {repo} (404).")
        out.append("  -> Fine-grained token: add this repository under Repository access.")
        out.append("  -> Classic token: check the repository name and that the account has access.")
        return False, out
    if status != 200:
        out.append(f"  Unexpected {status} reading {repo}.")
        return False, out
    out.append(f"  Repository visible: {repo}")

    # 3. The endpoint that actually matters.
    status, headers, body = _probe(f"/repos/{repo}/traffic/views?per=day", token)
    if status == 200:
        n = len(json.loads(body).get("views", []))
        out.append(f"  Traffic readable: yes ({n} days returned)")
        return True, out

    out.append(f"  Traffic readable: no ({status})")
    wanted = headers.get("X-Accepted-GitHub-Permissions")
    if wanted:
        # GitHub named the requirement. Trust this over any documentation.
        out.append(f"  GitHub says it requires: {wanted}")
        out.append("  -> Grant exactly that on the token, then re-run this check.")
    elif status == 403 and scopes:
        # Classic token: scopes are visible, so the fix is a scope.
        out.append("  -> Classic token missing the traffic permission.")
        out.append("     Tick the top-level `repo` scope and regenerate.")
    elif status == 403:
        # Fine-grained token. GitHub does not always return the header here,
        # and its documentation says only "write access" without naming the
        # fine-grained permission, so this is the one case we cannot resolve
        # definitively. Say so rather than inventing a name.
        out.append("  -> Fine-grained token: the repository is visible, so access is")
        out.append("     granted but a permission is missing. GitHub returned no")
        out.append("     X-Accepted-GitHub-Permissions header naming which one, and its")
        out.append("     docs say only \"write access\".")
        out.append("     Try granting repository Administration (read) — unverified — or")
        out.append("     use a classic token with the `repo` scope, which is confirmed")
        out.append("     working. See github.com/HafidIdrissi/github-traffic-archive/issues/2")
    else:
        out.append(f"  Response: {body[:160]}")
    return False, out


def run(repos: list[str], token: str) -> int:
    print("Checking token access to the traffic API\n")
    ok_all = True
    for repo in repos:
        print(f"{repo}")
        ok, lines = check(repo, token)
        for line in lines:
            print(line)
        print()
        ok_all = ok_all and ok
    if ok_all:
        print("All good — archiving will work with this token.")
        return 0
    print("Not ready. Fix the points above, then run --check again.")
    return 1
