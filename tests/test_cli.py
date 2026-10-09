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


@pytest.mark.parametrize("repo_flag,repo_value", [
    ("--repos", "fictional/repo"),
    ("--owner", "fictional"),
])
def test_file_output_is_rejected_before_api_calls(
    tmp_path, monkeypatch, capsys, repo_flag, repo_value,
):
    output_file = tmp_path / "archive.txt"
    sentinel = b"existing archive sentinel\n"
    output_file.write_bytes(sentinel)
    calls = []

    def discover(*args):
        calls.append("discovery")
        return ["fictional/repo"]

    def archive(*args):
        calls.append("archive")
        return {"views": 0, "clones": 0, "views_days": 0}

    def traffic(*args, **kwargs):
        calls.append("traffic")
        return []

    monkeypatch.setattr(api, "owned_repos", discover)
    monkeypatch.setattr(cli, "archive_repo", archive)
    for name in ("views", "clones", "referrers", "paths"):
        monkeypatch.setattr(api, name, traffic)

    code = cli.main([repo_flag, repo_value, "--token", "dummy-token", "--out", str(output_file)])

    assert code != 0
    assert calls == []
    assert output_file.read_bytes() == sentinel
    error = capsys.readouterr().err
    assert "--out" in error
    assert "file" in error.lower()


@pytest.mark.parametrize("existing", [False, True])
def test_directory_output_roots_remain_valid(tmp_path, fake_api, existing):
    out = tmp_path / "archive"
    if existing:
        out.mkdir()

    code = cli.main(["--repos", "fictional/repo", "--token", "dummy-token", "--out", str(out)])

    assert code == 0
    assert (out / "fictional__repo" / "views.json").exists()


def test_check_ignores_file_output_and_propagates_doctor_result(tmp_path, monkeypatch):
    output_file = tmp_path / "archive.txt"
    sentinel = b"existing archive sentinel\n"
    output_file.write_bytes(sentinel)
    calls = []

    def diagnose(repos, token):
        calls.append((repos, token))
        return 7

    monkeypatch.setattr(cli.doctor, "run", diagnose)
    monkeypatch.setattr(api, "owned_repos", lambda *args: pytest.fail("unexpected discovery"))
    monkeypatch.setattr(cli, "archive_repo", lambda *args: pytest.fail("unexpected archive"))

    code = cli.main([
        "--repos", "fictional/repo", "--token", "dummy-token",
        "--check", "--out", str(output_file),
    ])

    assert code == 7
    assert calls == [(["fictional/repo"], "dummy-token")]
    assert output_file.read_bytes() == sentinel


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




def test_quiet_flag_suppresses_stdout_on_success(tmp_path, fake_api, capsys):
    code = cli.main([
        "--repos", "fictional/repo",
        "--token", "dummy-token",
        "--out", str(tmp_path),
        "--quiet",
    ])
    assert code == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_quiet_flag_preserves_stderr_on_partial_failure(tmp_path, monkeypatch, capsys):
    state = {
        "views": [{"timestamp": "2026-01-02T00:00:00Z", "count": 5, "uniques": 3}],
        "clones": [{"timestamp": "2026-01-02T00:00:00Z", "count": 2, "uniques": 1}],
        "referrers": [{"referrer": "google.com", "count": 4, "uniques": 2}],
        "paths": [{"path": "/x", "title": "x", "count": 4, "uniques": 2}],
    }

    def fail_views(repo, token, per="day"):
        if repo == "fictional/bad":
            raise api.TrafficError("403 Forbidden")
        return state["views"]

    monkeypatch.setattr(api, "views", fail_views)
    monkeypatch.setattr(api, "clones", lambda r, t, per="day": state["clones"])
    monkeypatch.setattr(api, "referrers", lambda r, t: state["referrers"])
    monkeypatch.setattr(api, "paths", lambda r, t: state["paths"])

    code = cli.main([
        "--repos", "fictional/good,fictional/bad",
        "--token", "dummy-token",
        "--out", str(tmp_path),
        "--quiet",
    ])
    assert code == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "fictional/bad: skipped — 403 Forbidden" in captured.err
    assert "1 skipped; see messages above." in captured.err


def test_quiet_flag_preserves_stderr_on_all_failures_and_exits_nonzero(tmp_path, monkeypatch, capsys):
    def fail_views(repo, token, per="day"):
        raise api.TrafficError("404 Not Found")

    monkeypatch.setattr(api, "views", fail_views)

    code = cli.main([
        "--repos", "fictional/bad1,fictional/bad2",
        "--token", "dummy-token",
        "--out", str(tmp_path),
        "--quiet",
    ])
    assert code != 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "fictional/bad1: skipped — 404 Not Found" in captured.err
    assert "fictional/bad2: skipped — 404 Not Found" in captured.err
    assert "2 skipped; see messages above." in captured.err


def test_quiet_flag_with_check_leaves_diagnostics_unchanged(monkeypatch, capsys):
    calls = []

    def diagnose(repos, token):
        calls.append((repos, token))
        print("Checking token access to the traffic API")
        print("fictional/repo\n  Traffic readable: yes (14 days returned)\nAll good — archiving will work with this token.")
        return 0

    monkeypatch.setattr(cli.doctor, "run", diagnose)

    code = cli.main([
        "--repos", "fictional/repo",
        "--token", "dummy-token",
        "--check",
        "--quiet",
    ])
    assert code == 0
    assert calls == [([ "fictional/repo" ], "dummy-token")]
    captured = capsys.readouterr()
    assert "Checking token access" in captured.out
