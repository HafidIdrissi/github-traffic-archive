"""Minimal GitHub REST client for the traffic endpoints.

Standard library only, so the action needs no dependency install step and
cannot break because of a transitive release.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any

API = "https://api.github.com"
UA = "github-traffic-archive"


class TrafficError(RuntimeError):
    """Raised with an explanation a user can act on."""


def _request(path: str, token: str, retries: int = 3) -> Any:
    req = urllib.request.Request(
        f"{API}{path}",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": UA,
        },
    )
    last: Exception | None = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as err:
            body = err.read().decode("utf-8", "replace")[:200]
            if err.code == 403 and "rate limit" in body.lower():
                # Secondary rate limits clear on their own; back off and retry.
                last = err
                time.sleep(2 ** attempt * 5)
                continue
            if err.code in (401, 403):
                raise TrafficError(
                    f"{path} returned {err.code}. The traffic API requires push "
                    f"access to the repository. A token with only read access, "
                    f"or the default GITHUB_TOKEN in a fork, will fail here. {body}"
                ) from err
            if err.code == 404:
                raise TrafficError(
                    f"{path} returned 404. Either the repository does not exist "
                    f"or the token cannot see it."
                ) from err
            raise TrafficError(f"{path} returned {err.code}: {body}") from err
        except urllib.error.URLError as err:
            last = err
            time.sleep(2 ** attempt)
    raise TrafficError(f"{path} failed after {retries} attempts: {last}")


def views(repo: str, token: str, per: str = "day") -> list[dict[str, Any]]:
    return _request(f"/repos/{repo}/traffic/views?per={per}", token).get("views", [])


def clones(repo: str, token: str, per: str = "day") -> list[dict[str, Any]]:
    return _request(f"/repos/{repo}/traffic/clones?per={per}", token).get("clones", [])


def referrers(repo: str, token: str) -> list[dict[str, Any]]:
    return _request(f"/repos/{repo}/traffic/popular/referrers", token)


def paths(repo: str, token: str) -> list[dict[str, Any]]:
    return _request(f"/repos/{repo}/traffic/popular/paths", token)


def owned_repos(owner: str, token: str, include_forks: bool = False) -> list[str]:
    """Every repository the token can archive for this owner.

    Tries the authenticated-user endpoint first, which returns private
    repositories too, and falls back to the public listing for organisations.
    """
    found: list[str] = []
    for page in range(1, 11):
        try:
            batch = _request(f"/user/repos?per_page=100&page={page}&affiliation=owner", token)
        except TrafficError:
            batch = _request(f"/users/{owner}/repos?per_page=100&page={page}", token)
        if not batch:
            break
        for repo in batch:
            if repo["owner"]["login"].lower() != owner.lower():
                continue
            if repo.get("fork") and not include_forks:
                continue
            found.append(repo["full_name"])
        if len(batch) < 100:
            break
    return sorted(set(found))
