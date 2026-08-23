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
    responder(monkeypatch, {
        "/user": OK_USER,
        "/traffic/views": (200, {}, '{"views": [{}, {}]}'),
        "/repos/o/r": (200, {}, "{}"),
    })
    ok, lines = doctor.check("o/r", "tok")
    assert ok
    assert any("Traffic readable: yes" in l for l in lines)


def test_invalid_token_is_named_as_such(monkeypatch):
    responder(monkeypatch, {"/user": (401, {}, "{}")})
    ok, lines = doctor.check("o/r", "tok")
    assert not ok
    assert any("invalid, expired or revoked" in l for l in lines)


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


def test_403_without_a_header_offers_both_remedies(monkeypatch):
    responder(monkeypatch, {
        "/user": OK_USER,
        "/repos/o/r": (200, {}, "{}"),
        "/traffic/views": (403, {}, "{}"),
    })
    ok, lines = doctor.check("o/r", "tok")
    assert not ok
    joined = " ".join(lines)
    assert "Classic token" in joined and "Fine-grained token" in joined


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
