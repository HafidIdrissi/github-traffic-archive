"""Isolated unit tests for traffic_archive.api."""

import pytest

from traffic_archive import api


@pytest.fixture
def fake_request(monkeypatch):
    """Replace api._request with a mock serving local repo batches."""
    data = {}

    def _fake(path: str, token: str, retries: int = 3):
        return data.get(path, [])

    monkeypatch.setattr(api, "_request", _fake)
    return data


def test_owned_repos_ignores_other_owners(fake_request):
    fake_request["/user/repos?per_page=100&page=1&affiliation=owner"] = [
        {"full_name": "target-owner/repo-a", "owner": {"login": "target-owner"}, "fork": False},
        {"full_name": "other-owner/repo-b", "owner": {"login": "other-owner"}, "fork": False},
    ]

    repos = api.owned_repos("target-owner", "tok")
    assert repos == ["target-owner/repo-a"]


def test_owned_repos_excludes_forks_by_default(fake_request):
    fake_request["/user/repos?per_page=100&page=1&affiliation=owner"] = [
        {"full_name": "myorg/source-repo", "owner": {"login": "myorg"}, "fork": False},
        {"full_name": "myorg/forked-repo", "owner": {"login": "myorg"}, "fork": True},
    ]

    repos = api.owned_repos("myorg", "tok")
    assert repos == ["myorg/source-repo"]


def test_owned_repos_includes_forks_when_requested(fake_request):
    fake_request["/user/repos?per_page=100&page=1&affiliation=owner"] = [
        {"full_name": "myorg/source-repo", "owner": {"login": "myorg"}, "fork": False},
        {"full_name": "myorg/forked-repo", "owner": {"login": "myorg"}, "fork": True},
    ]

    repos = api.owned_repos("myorg", "tok", include_forks=True)
    assert repos == ["myorg/forked-repo", "myorg/source-repo"]


def test_owned_repos_returns_sorted_and_unique_names(fake_request):
    """
    Verify that owned_repos correctly handles duplicate repository names and returns them in alphabetical order.
    The duplicate entry ('myorg/repo-a') is provided in page 1 to guarantee deduplication logic without triggering
    pagination early termination.
    """
    fake_request["/user/repos?per_page=100&page=1&affiliation=owner"] = [
        {"full_name": "myorg/repo-z", "owner": {"login": "myorg"}, "fork": False},
        {"full_name": "myorg/repo-a", "owner": {"login": "myorg"}, "fork": False},
        {"full_name": "myorg/repo-a", "owner": {"login": "myorg"}, "fork": False},
    ]

    repos = api.owned_repos("myorg", "tok")
    assert repos == ["myorg/repo-a", "myorg/repo-z"]
   