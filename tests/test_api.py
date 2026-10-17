"""Isolated unit tests for traffic_archive.api."""

import io
import json
import traceback
import urllib.error
from unittest.mock import Mock

import pytest

from traffic_archive import api


@pytest.mark.parametrize(("helper", "metric"), [(api.views, "views"), (api.clones, "clones")])
@pytest.mark.parametrize(
    ("per", "expected_per"),
    [(None, "day"), ("day", "day"), ("week", "week")],
    ids=["omitted", "day", "week"],
)
def test_traffic_aggregation_requests_and_extracts_data(
    monkeypatch, helper, metric, per, expected_per
):
    records = [{"timestamp": "2026-10-10T00:00:00Z", "count": 3, "uniques": 2}]
    request = Mock(return_value={metric: records})
    monkeypatch.setattr(api, "_request", request)

    if per is None:
        result = helper("example/project", "dummy-token")
    else:
        result = helper("example/project", "dummy-token", per=per)

    assert result == records
    request.assert_called_once_with(
        f"/repos/example/project/traffic/{metric}?per={expected_per}", "dummy-token"
    )


@pytest.mark.parametrize("helper", [api.views, api.clones])
@pytest.mark.parametrize("per", ["month", "", "Day", "week&extra=1"])
def test_traffic_aggregation_rejects_unsupported_values_before_request(
    monkeypatch, helper, per
):
    request = Mock()
    monkeypatch.setattr(api, "_request", request)

    with pytest.raises(ValueError):
        helper("example/project", "dummy-token", per=per)

    request.assert_not_called()


@pytest.mark.parametrize("helper", [api.views, api.clones])
def test_traffic_aggregation_rejects_missing_metric(monkeypatch, helper):
    request = Mock(return_value={})
    monkeypatch.setattr(api, "_request", request)

    with pytest.raises(api.TrafficError, match="expected an explicit"):
        helper("example/project", "dummy-token")


@pytest.mark.parametrize("helper", [api.views, api.clones])
def test_traffic_aggregation_preserves_transport_error(monkeypatch, helper):
    request = Mock(side_effect=api.TrafficError("transport failed"))
    monkeypatch.setattr(api, "_request", request)

    with pytest.raises(api.TrafficError, match="transport failed"):
        helper("example/project", "dummy-token")


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


SENTINEL_TOKEN = "synthetic-sentinel-token-not-real"


class _FakeResponse(io.BytesIO):
    """In-memory success response that records whether it was closed."""

    status = 200
    headers = {}


def _capture_request(monkeypatch):
    """Patch urlopen (not api._request) so the real Request gets built."""
    captured = []
    response = _FakeResponse(b"{}")

    def fake_urlopen(req, *args, **kwargs):
        captured.append(req)
        return response

    monkeypatch.setattr(api.urllib.request, "urlopen", fake_urlopen)
    return captured, response


def test_request_sends_documented_headers(monkeypatch):
    captured, _ = _capture_request(monkeypatch)

    api._request("/repos/owner/repo/traffic/views", SENTINEL_TOKEN)

    assert len(captured) == 1
    # urllib normalises header names, so compare case-insensitively.
    headers = {name.lower(): value for name, value in captured[0].header_items()}
    assert headers["accept"] == "application/vnd.github+json"
    assert headers["x-github-api-version"] == "2022-11-28"
    assert headers["user-agent"] == api.UA
    assert headers["authorization"] == "Bearer " + SENTINEL_TOKEN


def test_request_closes_response(monkeypatch):
    _, response = _capture_request(monkeypatch)

    api._request("/repos/owner/repo/traffic/views", SENTINEL_TOKEN)

    assert response.closed


TRAFFIC_HELPERS = [
    pytest.param(
        api.views,
        "/repos/owner/repo/traffic/views?per=day",
        "views",
        id="views",
    ),
    pytest.param(
        api.clones,
        "/repos/owner/repo/traffic/clones?per=day",
        "clones",
        id="clones",
    ),
    pytest.param(
        api.referrers,
        "/repos/owner/repo/traffic/popular/referrers",
        None,
        id="referrers",
    ),
    pytest.param(
        api.paths,
        "/repos/owner/repo/traffic/popular/paths",
        None,
        id="paths",
    ),
]


def _serve_success_body(monkeypatch, body: str):
    response = _FakeResponse(body.encode("utf-8"))
    requests = []

    def fake_urlopen(req, *args, **kwargs):
        requests.append(req)
        return response

    monkeypatch.setattr(api.urllib.request, "urlopen", fake_urlopen)
    return requests, response


def _response_payload(metric, rows):
    if metric is None:
        return json.dumps(rows)
    return json.dumps({"extra": "preserved by caller", metric: rows})


@pytest.mark.parametrize(("helper", "path", "metric"), TRAFFIC_HELPERS)
def test_traffic_helpers_reject_invalid_json_without_disclosure(
    monkeypatch, helper, path, metric
):
    body = '<html>RESPONSE_BODY_SENTINEL "token":"BODY_TOKEN_SENTINEL"</html>'
    token = "DUMMY_TOKEN_SENTINEL"
    requests, response = _serve_success_body(monkeypatch, body)

    with pytest.raises(api.TrafficError) as exc_info:
        helper("owner/repo", token)

    message = str(exc_info.value)
    displayed = "".join(
        traceback.format_exception(
            type(exc_info.value), exc_info.value, exc_info.value.__traceback__
        )
    )
    assert path in message
    assert "invalid JSON" in message
    assert "RESPONSE_BODY_SENTINEL" not in message
    assert "BODY_TOKEN_SENTINEL" not in message
    assert token not in message
    assert "RESPONSE_BODY_SENTINEL" not in displayed
    assert "BODY_TOKEN_SENTINEL" not in displayed
    assert token not in displayed
    assert response.closed
    assert len(requests) == 1
    headers = {name.lower(): value for name, value in requests[0].header_items()}
    assert headers["authorization"] == "Bearer " + token


@pytest.mark.parametrize(("helper", "path", "metric"), TRAFFIC_HELPERS)
@pytest.mark.parametrize("root", ["null", '"RESPONSE_BODY_SENTINEL"'])
def test_traffic_helpers_reject_wrong_root_containers(
    monkeypatch, helper, path, metric, root
):
    requests, response = _serve_success_body(monkeypatch, root)

    with pytest.raises(api.TrafficError) as exc_info:
        helper("owner/repo", SENTINEL_TOKEN)

    assert path in str(exc_info.value)
    assert "expected" in str(exc_info.value)
    assert "RESPONSE_BODY_SENTINEL" not in str(exc_info.value)
    assert SENTINEL_TOKEN not in str(exc_info.value)
    assert response.closed
    assert len(requests) == 1


@pytest.mark.parametrize(("helper", "path", "metric"), TRAFFIC_HELPERS[:2])
def test_daily_helpers_reject_list_root(monkeypatch, helper, path, metric):
    requests, response = _serve_success_body(monkeypatch, "[]")

    with pytest.raises(api.TrafficError) as exc_info:
        helper("owner/repo", SENTINEL_TOKEN)

    assert path in str(exc_info.value)
    assert "JSON object" in str(exc_info.value)
    assert SENTINEL_TOKEN not in str(exc_info.value)
    assert response.closed
    assert len(requests) == 1


@pytest.mark.parametrize(("helper", "path", "metric"), TRAFFIC_HELPERS[2:])
def test_snapshot_helpers_reject_object_root(monkeypatch, helper, path, metric):
    body = '{"response":"RESPONSE_BODY_SENTINEL"}'
    requests, response = _serve_success_body(monkeypatch, body)

    with pytest.raises(api.TrafficError) as exc_info:
        helper("owner/repo", SENTINEL_TOKEN)

    assert path in str(exc_info.value)
    assert "JSON list" in str(exc_info.value)
    assert "RESPONSE_BODY_SENTINEL" not in str(exc_info.value)
    assert SENTINEL_TOKEN not in str(exc_info.value)
    assert response.closed
    assert len(requests) == 1


@pytest.mark.parametrize(("helper", "path", "metric"), TRAFFIC_HELPERS[:2])
@pytest.mark.parametrize(
    "body",
    [
        "{}",
        '{"other": []}',
        '{"views": null}',
        '{"views": {}}',
        '{"views": "RESPONSE_BODY_SENTINEL"}',
    ],
)
def test_daily_helpers_reject_missing_or_non_list_metric(
    monkeypatch, helper, path, metric, body
):
    if metric == "clones":
        body = body.replace("views", "clones")
    requests, response = _serve_success_body(monkeypatch, body)

    with pytest.raises(api.TrafficError) as exc_info:
        helper("owner/repo", SENTINEL_TOKEN)

    assert path in str(exc_info.value)
    assert f"{metric}" in str(exc_info.value)
    assert "RESPONSE_BODY_SENTINEL" not in str(exc_info.value)
    assert SENTINEL_TOKEN not in str(exc_info.value)
    assert response.closed
    assert len(requests) == 1


@pytest.mark.parametrize(("helper", "path", "metric"), TRAFFIC_HELPERS)
@pytest.mark.parametrize(
    "rows",
    [[], [{"timestamp": "2026-10-10T00:00:00Z", "count": 3, "uniques": 2}]],
)
def test_traffic_helpers_accept_empty_and_nonempty_lists(
    monkeypatch, helper, path, metric, rows
):
    body = _response_payload(metric, rows)
    requests, response = _serve_success_body(monkeypatch, body)

    result = helper("owner/repo", "dummy-token")

    assert result == rows
    assert response.closed
    assert len(requests) == 1
    assert requests[0].full_url == api.API + path
