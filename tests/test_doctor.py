import pytest

from traffic_archive import doctor


def responder(monkeypatch, mapping):
    """Map a URL fragment to (status, headers, body).

    Longest fragment first: `/repos/o/r/traffic/views` contains `/repos/o/r`,
    so shortest-match order would answer every traffic probe with the
    repository response.
    """
    ordered = sorted(mapping.items(), key=lambda kv: -len(kv[0]))

    def fake(path, token):
        for frag, result in ordered:
            if frag in path:
                return result
        raise AssertionError(f"unexpected probe: {path}")
    monkeypatch.setattr(doctor, "_probe", fake)


OK_USER = (200, {"X-OAuth-Scopes": "repo, gist"}, '{"login": "someone"}')


def test_reports_success_when_traffic_is_readable(monkeypatch):
    responses = {
        "/user": OK_USER,
        "/repos/o/r": (200, {}, "{}"),
        "/repos/o/r/traffic/views?per=day": (200, {}, '{"views": [{}, {}]}'),
    }
    calls = []

    def fake_probe(path, token):
        assert token == "dummy-token"
        calls.append(path)
        return responses[path]

    monkeypatch.setattr(doctor, "_probe", fake_probe)
    ok, lines = doctor.check("o/r", "dummy-token")
    assert ok
    assert calls == ["/user", "/repos/o/r", "/repos/o/r/traffic/views?per=day"]
    assert any("Token authenticates as: someone" in l for l in lines)
    assert any("Traffic readable: yes" in l for l in lines)


def test_invalid_token_is_named_as_such(monkeypatch):
    responder(monkeypatch, {"/user": (401, {}, "{}")})
    ok, lines = doctor.check("o/r", "tok")
    assert not ok
    assert any("invalid, expired or revoked" in l for l in lines)


@pytest.mark.parametrize("status", [403, 429, 500])
def test_unexpected_user_status_stops_before_repository_probes(monkeypatch, status):
    calls = []

    def fake_probe(path, token):
        assert token == "dummy-token"
        calls.append(path)
        assert path == "/user"
        return status, {}, "private response body"

    monkeypatch.setattr(doctor, "_probe", fake_probe)
    ok, lines = doctor.check("o/r", "dummy-token")
    report = " ".join(lines)

    assert not ok
    assert calls == ["/user"]
    assert f"HTTP {status}" in report
    assert "authentication could not be confirmed" in report.lower()
    assert "Token authenticates as" not in report
    assert "Repository visible" not in report
    assert "Traffic readable: yes" not in report
    assert "dummy-token" not in report
    assert "private response body" not in report
    if status in (429, 500):
        assert "retry" in report.lower()
    else:
        assert "invalid token" not in report.lower()
        assert "permission" not in report.lower()


def test_run_fails_when_user_authentication_cannot_be_confirmed(monkeypatch, capsys):
    calls = []

    def fake_probe(path, token):
        assert token == "dummy-token"
        calls.append(path)
        assert path == "/user"
        return 500, {}, "private response body"

    monkeypatch.setattr(doctor, "_probe", fake_probe)
    assert doctor.run(["o/r"], "dummy-token") == 1
    output = capsys.readouterr().out
    assert calls == ["/user"]
    assert "HTTP 500" in output
    assert "All good" not in output
    assert "dummy-token" not in output
    assert "private response body" not in output


def test_missing_repo_access_is_distinguished_from_missing_permission(monkeypatch):
    """404 and 403 need different remedies, so they must not be conflated."""
    responder(monkeypatch, {
        "/user": OK_USER,
        "/repos/o/r": (404, {}, "{}"),
    })
    ok, lines = doctor.check("o/r", "tok")
    assert not ok
    assert any("Repository access" in l for l in lines)


def test_githubs_own_permission_header_is_surfaced(monkeypatch):
    """The whole point: GitHub names the permission, so stop guessing."""
    responder(monkeypatch, {
        "/user": (200, {}, '{"login": "someone"}'),          # no scopes -> fine-grained
        "/repos/o/r": (200, {}, "{}"),
        "/traffic/views": (403, {"X-Accepted-GitHub-Permissions": "administration=read"}, "{}"),
    })
    ok, lines = doctor.check("o/r", "tok")
    assert not ok
    assert any("administration=read" in l for l in lines)
    assert any("requires" in l for l in lines)


def test_classic_token_without_repo_scope_is_called_out(monkeypatch):
    responder(monkeypatch, {
        "/user": (200, {"X-OAuth-Scopes": "gist, read:org"}, '{"login": "someone"}'),
        "/repos/o/r": (200, {}, "{}"),
        "/traffic/views": (403, {}, "{}"),
    })
    ok, lines = doctor.check("o/r", "tok")
    assert not ok
    assert any("`repo` scope" in l for l in lines)


def test_classic_403_names_the_scope_not_the_fine_grained_advice(monkeypatch):
    """A classic token exposes its scopes, so the remedy is unambiguous."""
    responder(monkeypatch, {
        "/user": OK_USER,                       # has X-OAuth-Scopes
        "/repos/o/r": (200, {}, "{}"),
        "/traffic/views": (403, {}, "{}"),
    })
    ok, lines = doctor.check("o/r", "tok")
    joined = " ".join(lines)
    assert not ok
    assert "Classic token" in joined
    assert "Fine-grained" not in joined, "must not offer both when the type is known"


def test_fine_grained_403_without_header_admits_the_uncertainty(monkeypatch):
    """The one case nothing can resolve: say so instead of inventing a name."""
    responder(monkeypatch, {
        "/user": (200, {}, '{"login": "someone"}'),   # no scopes -> fine-grained
        "/repos/o/r": (200, {}, "{}"),
        "/traffic/views": (403, {}, "{}"),
    })
    ok, lines = doctor.check("o/r", "tok")
    joined = " ".join(lines)
    assert not ok
    assert "Fine-grained token" in joined
    assert "unverified" in joined
    assert "Classic token missing" not in joined


def test_no_token_is_reported_rather_than_crashing(monkeypatch):
    ok, lines = doctor.check("o/r", "")
    assert not ok
    assert any("No token" in l for l in lines)


def test_network_failure_is_reported_clearly(monkeypatch):
    responder(monkeypatch, {"/user": (0, {}, "connection refused")})
    ok, lines = doctor.check("o/r", "tok")
    assert not ok
    assert any("Could not reach" in l for l in lines)


def test_run_returns_nonzero_when_any_repo_fails(monkeypatch, capsys):
    monkeypatch.setattr(doctor, "check",
                        lambda repo, token: (repo != "o/bad", ["  line"]))
    assert doctor.run(["o/good", "o/bad"], "tok") == 1
    assert doctor.run(["o/good"], "tok") == 0
