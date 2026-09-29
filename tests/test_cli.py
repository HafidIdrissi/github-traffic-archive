import argparse
import json

import pytest

from traffic_archive import __version__, api, cli


@pytest.fixture
def fake_api(monkeypatch):
    """Stand in for the traffic endpoints so the tests never touch the network."""
    state = {
        "views": [{"timestamp": "2026-01-02T00:00:00Z", "count": 5, "uniques": 3}],
        "clones": [{"timestamp": "2026-01-02T00:00:00Z", "count": 2, "uniques": 1}],
        "referrers": [{"referrer": "google.com", "count": 4, "uniques": 2}],
        "paths": [{"path": "/x", "title": "x", "count": 4, "uniques": 2}],
    }
    monkeypatch.setattr(api, "views", lambda r, t, per="day": state["views"])
    monkeypatch.setattr(api, "clones", lambda r, t, per="day": state["clones"])
    monkeypatch.setattr(api, "referrers", lambda r, t: state["referrers"])
    monkeypatch.setattr(api, "paths", lambda r, t: state["paths"])
    return state


def test_cli_version_flag(capsys):
    """Verify --version outputs program name and version, exiting with status 0 without requiring tokens."""
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["--version"])

    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    assert captured.out == f"traffic-archive {__version__}\n"


def test_archive_writes_json_and_csv_per_metric(tmp_path, fake_api):
    cli.archive_repo("owner/repo", "tok", tmp_path, "2026-01-02")
    base = tmp_path / "owner__repo"
    for name in ("views.json", "views.csv", "clones.json", "clones.csv",
                 "referrers.json", "paths.json"):
        assert (base / name).exists(), name


def test_second_run_preserves_days_the_api_no_longer_returns(tmp_path, fake_api):
    """The failure this tool exists to prevent."""
    base = tmp_path / "owner__repo"
    base.mkdir(parents=True)
    (base / "views.json").write_text(json.dumps(
        [{"timestamp": "2025-06-01T00:00:00Z", "count": 99, "uniques": 40}]), encoding="utf-8")

    cli.archive_repo("owner/repo", "tok", tmp_path, "2026-01-02")

    stored = json.loads((base / "views.json").read_text(encoding="utf-8"))
    stamps = [s["timestamp"] for s in stored]
    assert "2025-06-01T00:00:00Z" in stamps, "history outside the window was lost"
    assert "2026-01-02T00:00:00Z" in stamps


def test_corrupt_archive_is_not_silently_overwritten(tmp_path, fake_api):
    base = tmp_path / "owner__repo"
    base.mkdir(parents=True)
    (base / "views.json").write_text("{ not json", encoding="utf-8")
    with pytest.raises(SystemExit):
        cli.archive_repo("owner/repo", "tok", tmp_path, "2026-01-02")


def test_one_unreadable_repo_does_not_abandon_the_others(tmp_path, monkeypatch, fake_api, capsys):
    real_views = api.views

    def selective(repo, token, per="day"):
        if repo == "owner/private":
            raise api.TrafficError("403 no push access")
        return real_views(repo, token, per)

    monkeypatch.setattr(api, "views", selective)
    code = cli.main(["--repos", "owner/private,owner/repo",
                     "--token", "tok", "--out", str(tmp_path)])
    assert code == 0
    assert (tmp_path / "owner__repo" / "views.json").exists()
    assert "skipped" in capsys.readouterr().err


def test_exit_code_is_nonzero_when_everything_failed(tmp_path, monkeypatch, fake_api):
    monkeypatch.setattr(api, "views",
                        lambda r, t, per="day": (_ for _ in ()).throw(api.TrafficError("boom")))
    code = cli.main(["--repos", "owner/a", "--token", "tok", "--out", str(tmp_path)])
    assert code == 1


def test_missing_token_is_rejected(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    with pytest.raises(SystemExit):
        cli.main(["--repos", "owner/a", "--out", str(tmp_path)])


def test_nothing_to_archive_is_rejected(tmp_path):
    with pytest.raises(SystemExit):
        cli.main(["--token", "tok", "--out", str(tmp_path)])


def test_valid_repos_parsing():
    parsed = cli.parse_repo_arg(" owner/repo1 , owner/repo2 ")
    assert parsed == ["owner/repo1", "owner/repo2"]


def test_parse_repo_arg_deduplicates_preserving_first_occurrence_order():
    parsed = cli.parse_repo_arg("owner/a, owner/a , owner/b,owner/a, owner/b ")
    assert parsed == ["owner/a", "owner/b"]


def test_cli_repos_deduplicates_and_archives_each_repo_once(tmp_path, monkeypatch, fake_api, capsys):
    fetched_views: list[str] = []
    real_views = api.views

    def tracking_views(repo, token, per="day"):
        fetched_views.append(repo)
        return real_views(repo, token, per)

    monkeypatch.setattr(api, "views", tracking_views)

    code = cli.main([
        "--repos", "owner/a, owner/a ,owner/b,owner/a",
        "--token", "tok",
        "--out", str(tmp_path),
    ])

    assert code == 0
    assert fetched_views == ["owner/a", "owner/b"]
    captured = capsys.readouterr()
    assert "2/2 archived into" in captured.out


def test_cli_rejects_invalid_entry_after_duplicate_before_any_api_request(tmp_path, monkeypatch, fake_api):
    called = False

    def fail_if_called(repo, token, per="day"):
        nonlocal called
        called = True
        return []

    monkeypatch.setattr(api, "views", fail_if_called)

    with pytest.raises(SystemExit) as exc_info:
        cli.main([
            "--repos", "owner/a,owner/a,invalid-repo",
            "--token", "tok",
            "--out", str(tmp_path),
        ])

    assert exc_info.value.code != 0
    assert not called


@pytest.mark.parametrize(
    "invalid_input",
    [
        "owner-only",
        "/repo",
        "owner/",
        "owner/repo/extra",
        "",
        "owner/repo, bad_repo",
        "owner/repo,",
        "owner/repo,,owner/repo2",
        " , owner/repo",
    ],
)
def test_invalid_repos_format_rejected(invalid_input):
    with pytest.raises(argparse.ArgumentTypeError) as exc_info:
        cli.parse_repo_arg(invalid_input)
    assert "'owner/name'" in str(exc_info.value)


def test_cli_rejects_invalid_repo_and_exits(capsys):
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["--repos", "invalid-format", "--token", "tok", "--out", "traffic"])

    assert exc_info.value.code != 0
    captured = capsys.readouterr()
    assert "Expected 'owner/name'" in captured.err

