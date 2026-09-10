"""Isolated unit tests for traffic_archive.api."""

import io
import urllib.error

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


def _http_error(code: int, body: str = "") -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "https://api.github.com/repos/owner/repo/traffic/views",
        code,
        "response",
        {},
        io.BytesIO(body.encode()),
    )


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (401, "invalid, expired, or malformed"),
        (403, "Administration: Read-only"),
    ],
)
def test_request_explains_authentication_and_authorisation_errors_separately(
    monkeypatch, code, expected
):
    token = "secret-token-value"

    def fail_request(*_args, **_kwargs):
        raise _http_error(code)

    monkeypatch.setattr(api.urllib.request, "urlopen", fail_request)

    with pytest.raises(api.TrafficError, match=expected) as exc_info:
        api._request("/repos/owner/repo/traffic/views", token)

    assert token not in str(exc_info.value)


def test_request_keeps_rate_limit_retries_separate_from_permission_errors(monkeypatch):
    attempts = 0

    def rate_limit_then_succeed(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise _http_error(403, "API rate limit exceeded")

        class Response:
            def read(self):
                return b'{"views": []}'

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        return Response()

    monkeypatch.setattr(api.urllib.request, "urlopen", rate_limit_then_succeed)
    monkeypatch.setattr(api.time, "sleep", lambda _delay: None)

    assert api._request("/repos/owner/repo/traffic/views", "token") == {"views": []}
    assert attempts == 2

