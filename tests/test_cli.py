import argparse
import csv
import errno
import json
import os
import stat
import struct
import sys
from pathlib import Path

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


linux_only = pytest.mark.skipif(sys.platform != "linux", reason="atomic replacement is Linux-only (#14)")


def _write_sample(path):
    if path.suffix == ".json":
        cli._write_json(path, [{"label": "café"}])
    else:
        cli._write_csv(path, [["label", "count"], ['café, "quoted"\nline', "1"]])


def _set_acl(path, entries, attr="system.posix_acl_access"):
    """Install a Linux POSIX ACL (v2 xattr format) from (tag, perm, id) entries."""
    acl = struct.pack("<I", 2) + b"".join(struct.pack("<HHi", *entry) for entry in entries)
    try:
        os.setxattr(path, attr, acl)
    except OSError as error:
        if error.errno == errno.ENOTSUP:
            pytest.skip("filesystem does not support POSIX ACLs")
        raise
    return os.getxattr(path, attr)


# Owner rw-, named user 12345 r--, owning group ---, mask r--, other ---.
NAMED_USER_ACL = [(0x01, 6, -1), (0x02, 4, 12345), (0x04, 0, -1), (0x10, 4, -1), (0x20, 0, -1)]


@pytest.mark.parametrize("suffix", [".json", ".csv"])
@pytest.mark.parametrize("existing", [False, True])
def test_writer_keeps_exact_format(tmp_path, suffix, existing):
    path = tmp_path / "archive" / ("sample" + suffix)
    if existing:
        path.parent.mkdir()
        path.write_bytes(b"old archive")
        inode = path.stat().st_ino

    _write_sample(path)

    if suffix == ".json":
        expected = '[\n  {\n    "label": "café"\n  }\n]\n'.replace("\n", os.linesep)
    else:
        expected = 'label,count\r\n"café, ""quoted""\nline",1\r\n'
    assert path.read_bytes() == expected.encode("utf-8")
    assert list(path.parent.iterdir()) == [path]
    if existing and sys.platform == "linux":
        assert path.stat().st_ino != inode, "Linux publishes a new file"


@pytest.mark.parametrize("suffix", [".json", ".csv"])
@pytest.mark.parametrize("platform", ["win32", "darwin"])
def test_other_platforms_keep_updating_in_place(tmp_path, monkeypatch, suffix, platform):
    path = tmp_path / ("sample" + suffix)
    path.write_bytes(b"old archive")
    inode = path.stat().st_ino

    def unexpected_staging(*args, **kwargs):
        pytest.fail("only Linux stages a replacement file")

    monkeypatch.setattr(cli.sys, "platform", platform)
    monkeypatch.setattr(cli.tempfile, "mkstemp", unexpected_staging)
    _write_sample(path)
    assert path.stat().st_ino == inode
    assert path.read_bytes() != b"old archive"


@pytest.mark.parametrize("failure", [TypeError("not serializable"), KeyboardInterrupt(), SystemExit(2)])
def test_serialization_failure_keeps_destination(tmp_path, monkeypatch, failure):
    path = tmp_path / "views.json"
    path.write_bytes(b"old archive")

    def reject_payload(*args, **kwargs):
        raise failure

    monkeypatch.setattr(cli.json, "dumps", reject_payload)
    with pytest.raises(type(failure)) as exc:
        cli._write_json(path, [])
    assert exc.value is failure
    assert path.read_bytes() == b"old archive"
    assert list(tmp_path.iterdir()) == [path]


@linux_only
@pytest.mark.parametrize("suffix", [".json", ".csv"])
@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("stage,failure", [
    ("write", OSError("simulated write failure")),
    ("write", KeyboardInterrupt()),
    ("close", OSError("simulated close failure")),
    ("replace", OSError("simulated replace failure")),
])
def test_failed_write_keeps_destination(tmp_path, monkeypatch, suffix, existing, stage, failure):
    path = tmp_path / ("sample" + suffix)
    if existing:
        path.write_bytes(b"old archive")
    open_stream = os.fdopen
    streams = []

    class FailingStream:
        def __init__(self, stream):
            self.stream = stream

        @property
        def closed(self):
            return self.stream.closed

        def write(self, value):
            if stage == "write":
                self.stream.write(value[:3])
                raise failure
            return self.stream.write(value)

        def close(self):
            self.stream.close()
            if stage == "close":
                raise failure

    def open_failing_stream(*args, **kwargs):
        streams.append(FailingStream(open_stream(*args, **kwargs)))
        return streams[-1]

    def reject_replace(source, target):
        assert stage == "replace" and streams[0].closed, "replace only after a clean close"
        assert Path(source).parent == Path(target).parent == tmp_path
        raise failure

    monkeypatch.setattr(cli.os, "fdopen", open_failing_stream)
    monkeypatch.setattr(cli.os, "replace", reject_replace)
    with pytest.raises(type(failure)) as exc:
        _write_sample(path)

    assert exc.value is failure
    assert len(streams) == 1 and streams[0].closed
    assert list(tmp_path.iterdir()) == ([path] if existing else [])
    if existing:
        assert path.read_bytes() == b"old archive"


@linux_only
def test_failed_cleanup_is_reported_without_masking_the_error(tmp_path, monkeypatch, capsys):
    path = tmp_path / "views.json"
    path.write_bytes(b"old archive")
    failure = OSError("replacement failed")

    def reject_replace(*args):
        raise failure

    def reject_unlink(*args, **kwargs):
        raise PermissionError("cleanup denied")

    with monkeypatch.context() as patch:
        patch.setattr(cli.os, "replace", reject_replace)
        patch.setattr(Path, "unlink", reject_unlink)
        with pytest.raises(OSError) as exc:
            _write_sample(path)
    assert exc.value is failure
    assert path.read_bytes() == b"old archive"
    [leftover] = set(tmp_path.iterdir()) - {path}
    diagnostic = capsys.readouterr().err
    assert str(leftover) in diagnostic and "cleanup denied" in diagnostic


@linux_only
def test_failed_stream_creation_closes_and_removes_staging(tmp_path, monkeypatch):
    path = tmp_path / "views.json"
    path.write_bytes(b"old archive")
    failure = OSError("cannot wrap descriptor")
    descriptors = []

    def reject_stream(fd, *args, **kwargs):
        descriptors.append(fd)
        raise failure

    monkeypatch.setattr(cli.os, "fdopen", reject_stream)
    with pytest.raises(OSError) as exc:
        _write_sample(path)
    assert exc.value is failure
    with pytest.raises(OSError):
        os.fstat(descriptors[0])
    assert path.read_bytes() == b"old archive"
    assert list(tmp_path.iterdir()) == [path]


@linux_only
@pytest.mark.parametrize("suffix", [".json", ".csv"])
def test_new_archive_is_owner_only(tmp_path, suffix):
    path = tmp_path / ("sample" + suffix)
    _write_sample(path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@linux_only
@pytest.mark.parametrize("mode", [0o600, 0o640, 0o644, 0o2660])
def test_existing_owner_group_and_mode_are_preserved(tmp_path, mode):
    path = tmp_path / "views.json"
    path.write_bytes(b"old archive")
    path.chmod(mode)
    previous = path.stat()
    _write_sample(path)
    current = path.stat()
    assert (current.st_uid, current.st_gid, current.st_mode) == (
        previous.st_uid, previous.st_gid, previous.st_mode,
    )


@linux_only
def test_existing_posix_acl_is_preserved(tmp_path):
    path = tmp_path / "views.json"
    path.write_bytes(b"old archive")
    acl = _set_acl(path, NAMED_USER_ACL)
    mode = path.stat().st_mode
    _write_sample(path)
    assert os.getxattr(path, "system.posix_acl_access") == acl
    assert path.stat().st_mode == mode
    assert json.loads(path.read_text(encoding="utf-8")) == [{"label": "café"}]
    assert list(tmp_path.iterdir()) == [path]


@linux_only
def test_directory_default_acl_is_not_added_to_an_archive_without_one(tmp_path):
    path = tmp_path / "views.json"
    path.write_bytes(b"old archive")
    path.chmod(0o640)
    _set_acl(tmp_path, [(0x01, 7, -1), (0x02, 5, 12345), (0x04, 0, -1), (0x10, 5, -1), (0x20, 0, -1)],
             attr="system.posix_acl_default")
    _write_sample(path)
    with pytest.raises(OSError) as exc:
        os.getxattr(path, "system.posix_acl_access")
    assert exc.value.errno == errno.ENODATA
    assert stat.S_IMODE(path.stat().st_mode) == 0o640

    # A new archive inherits the named entry, but the owner-only create mode masks it out.
    fresh = tmp_path / "clones.json"
    _write_sample(fresh)
    assert stat.S_IMODE(fresh.stat().st_mode) & 0o077 == 0


@linux_only
@pytest.mark.parametrize("call,error", [
    ("getxattr", errno.EACCES),
    ("getxattr", errno.ENOTSUP),
    ("getxattr", errno.EIO),
    ("fchmod", errno.EPERM),
])
def test_unreadable_or_unsupported_access_refuses_before_payload(tmp_path, monkeypatch, call, error):
    path = tmp_path / "views.json"
    path.write_bytes(b"old archive")
    path.chmod(0o640)

    def fail(*args, **kwargs):
        raise OSError(error, os.strerror(error))

    def unexpected_payload(*args, **kwargs):
        pytest.fail("no payload may be written when access cannot be preserved")

    monkeypatch.setattr(cli.os, call, fail)
    monkeypatch.setattr(cli.os, "fdopen", unexpected_payload)
    with pytest.raises(PermissionError, match="Cannot preserve the access policy") as exc:
        _write_sample(path)
    assert exc.value.__cause__.errno == error
    assert path.read_bytes() == b"old archive"
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert list(tmp_path.iterdir()) == [path]


@linux_only
def test_access_that_does_not_copy_exactly_is_refused(tmp_path, monkeypatch):
    path = tmp_path / "views.json"
    path.write_bytes(b"old archive")
    path.chmod(0o640)
    monkeypatch.setattr(cli.os, "fchmod", lambda fd, mode: None)
    with pytest.raises(PermissionError, match="did not copy exactly"):
        _write_sample(path)
    assert path.read_bytes() == b"old archive"
    assert list(tmp_path.iterdir()) == [path]


@linux_only
def test_read_only_archive_is_not_replaced(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root can write to read-only files")
    path = tmp_path / "views.json"
    path.write_bytes(b"old archive")
    path.chmod(0o444)
    with pytest.raises(PermissionError):
        _write_sample(path)
    assert path.read_bytes() == b"old archive"
    assert list(tmp_path.iterdir()) == [path]


@linux_only
def test_archive_in_read_only_directory_is_not_replaced(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root can create files in read-only directories")
    path = tmp_path / "archive" / "views.json"
    path.parent.mkdir()
    path.write_bytes(b"old archive")
    path.parent.chmod(0o555)
    try:
        with pytest.raises(PermissionError):
            _write_sample(path)
        assert path.read_bytes() == b"old archive"
        assert list(path.parent.iterdir()) == [path]
    finally:
        path.parent.chmod(0o755)


@linux_only
def test_symlinked_archive_updates_its_target(tmp_path):
    real = tmp_path / "store" / "views.json"
    real.parent.mkdir()
    real.write_bytes(b"old archive")
    real.chmod(0o640)
    link = tmp_path / "archive" / "views.json"
    link.parent.mkdir()
    link.symlink_to(real)

    _write_sample(link)

    assert link.is_symlink() and os.readlink(link) == str(real)
    assert json.loads(real.read_text(encoding="utf-8")) == [{"label": "café"}]
    assert stat.S_IMODE(real.stat().st_mode) == 0o640
    assert list(real.parent.iterdir()) == [real]
    assert list(link.parent.iterdir()) == [link]


def test_archive_saves_each_collected_response(tmp_path, fake_api):
    """Each response from fake_api belongs in its own metric's JSON file."""
    today = "2026-01-02"
    expected_files = {
        "views.json": fake_api["views"],
        "clones.json": fake_api["clones"],
        "referrers.json": [{"taken_on": today, "rows": fake_api["referrers"]}],
        "paths.json": [{"taken_on": today, "rows": fake_api["paths"]}],
    }

    cli.archive_repo("fictional/repo", "tok", tmp_path, today)

    archive = tmp_path / "fictional__repo"
    for filename, expected in expected_files.items():
        saved = json.loads((archive / filename).read_text(encoding="utf-8"))
        assert saved == expected


@pytest.mark.parametrize("failing_endpoint", ["views", "clones", "referrers", "paths"])
def test_fetch_failure_preserves_existing_archive(
    tmp_path, monkeypatch, fake_api, failing_endpoint,
):
    """A failed refresh must preserve the saved filenames and exact bytes."""
    # Yesterday's archive; fake_api returns newer data for January 2.
    daily_json = b'[{"timestamp":"2026-01-01T00:00:00Z","count":1,"uniques":1}]\n'
    daily_csv = b"date,count,uniques\r\n2026-01-01,1,1\r\n"
    snapshot_json = b'[{"taken_on":"2026-01-01","rows":[]}]\n'
    original_files = {
        "views.json": daily_json,
        "views.csv": daily_csv,
        "clones.json": daily_json,
        "clones.csv": daily_csv,
        "referrers.json": snapshot_json,
        "paths.json": snapshot_json,
    }
    archive = tmp_path / "fictional__repo"
    archive.mkdir()
    for filename, contents in original_files.items():
        (archive / filename).write_bytes(contents)

    def fail(repo, token):
        raise api.TrafficError("synthetic fetch failure")

    monkeypatch.setattr(api, failing_endpoint, fail)

    with pytest.raises(api.TrafficError):
        cli.archive_repo("fictional/repo", "tok", tmp_path, "2026-01-02")

    assert {path.name for path in archive.iterdir()} == set(original_files)
    for filename, original_bytes in original_files.items():
        assert (archive / filename).read_bytes() == original_bytes


@pytest.mark.parametrize("failing_endpoint", ["views", "clones", "referrers", "paths"])
def test_fetch_failure_creates_no_archive(tmp_path, monkeypatch, fake_api, failing_endpoint):
    """A failed first collection must not create output directories."""
    out = tmp_path / "archive"

    def fail(repo, token):
        raise api.TrafficError("synthetic fetch failure")

    monkeypatch.setattr(api, failing_endpoint, fail)

    with pytest.raises(api.TrafficError):
        cli.archive_repo("fictional/repo", "tok", out, "2026-01-02")

    assert not out.exists()


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


@pytest.mark.parametrize("failing_endpoint", ["views", "clones", "referrers", "paths"])
def test_one_unreadable_repo_does_not_abandon_the_others(
    tmp_path, monkeypatch, fake_api, capsys, failing_endpoint,
):
    real_fetch = getattr(api, failing_endpoint)

    def selective(repo, token):
        if repo == "owner/private":
            raise api.TrafficError("403 no push access")
        return real_fetch(repo, token)

    monkeypatch.setattr(api, failing_endpoint, selective)
    code = cli.main(["--repos", "owner/private,owner/repo",
                     "--token", "tok", "--out", str(tmp_path)])
    assert code == 0
    assert (tmp_path / "owner__repo" / "views.json").exists()
    assert (tmp_path / "owner__repo" / "paths.json").exists()
    assert not (tmp_path / "owner__private").exists()
    captured = capsys.readouterr()
    assert "1/2 archived into" in captured.out
    assert "owner/private: skipped" in captured.err


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


def test_archive_repo_serializes_large_integers_and_zeros_exact(tmp_path, fake_api):
    """archive_repo preserves exact large integers and zeros in JSON, CSV, and summary totals."""
    large = 2**53 + 1  # 9007199254740993
    larger = 2**53 + 3  # 9007199254740995
    fake_api["views"] = [
        {"timestamp": "2026-01-01T00:00:00Z", "count": large, "uniques": 0},
        {"timestamp": "2026-01-02T00:00:00Z", "count": 0, "uniques": larger},
        {"timestamp": "2026-01-03T00:00:00Z", "count": 5, "uniques": 2},
    ]

    summary = cli.archive_repo("owner/repo", "tok", tmp_path, "2026-01-03")

    expected_count = large + 0 + 5  # 9007199254740998
    assert summary["views"] == expected_count
    assert summary["views_days"] == 3
    assert type(summary["views"]) is int
    assert type(summary["views_days"]) is int

    base = tmp_path / "owner__repo"

    # Verify JSON
    views_json_path = base / "views.json"
    assert views_json_path.exists()
    payload = json.loads(views_json_path.read_text(encoding="utf-8"))
    assert len(payload) == 3
    assert payload[0] == {"timestamp": "2026-01-01T00:00:00Z", "count": 9007199254740993, "uniques": 0}
    assert payload[1] == {"timestamp": "2026-01-02T00:00:00Z", "count": 0, "uniques": 9007199254740995}
    assert payload[2] == {"timestamp": "2026-01-03T00:00:00Z", "count": 5, "uniques": 2}

    for item in payload:
        assert type(item["count"]) is int
        assert type(item["uniques"]) is int

    # Verify CSV
    views_csv_path = base / "views.csv"
    assert views_csv_path.exists()
    with views_csv_path.open("r", encoding="utf-8", newline="") as fh:
        rows = list(csv.reader(fh))

    assert rows == [
        ["date", "count", "uniques"],
        ["2026-01-01", "9007199254740993", "0"],
        ["2026-01-02", "0", "9007199254740995"],
        ["2026-01-03", "5", "2"],
    ]


def test_repeated_archive_preserves_large_integers(tmp_path, fake_api):
    """A subsequent archive run preserves historical large integer counts outside the window."""
    base = tmp_path / "owner__repo"
    base.mkdir(parents=True)
    historical_count = 2**53 + 1  # 9007199254740993
    historical_uniques = 2**53 + 3  # 9007199254740995
    historical_data = [
        {"timestamp": "2025-06-01T00:00:00Z", "count": historical_count, "uniques": historical_uniques}
    ]
    (base / "views.json").write_text(json.dumps(historical_data), encoding="utf-8")

    fresh_count = 42
    fresh_uniques = 10
    fake_api["views"] = [
        {"timestamp": "2026-01-02T00:00:00Z", "count": fresh_count, "uniques": fresh_uniques}
    ]

    summary = cli.archive_repo("owner/repo", "tok", tmp_path, "2026-01-02")

    stored = json.loads((base / "views.json").read_text(encoding="utf-8"))
    assert len(stored) == 2
    assert stored[0] == {
        "timestamp": "2025-06-01T00:00:00Z",
        "count": 9007199254740993,
        "uniques": 9007199254740995,
    }
    assert stored[1] == {
        "timestamp": "2026-01-02T00:00:00Z",
        "count": 42,
        "uniques": 10,
    }
    assert type(stored[0]["count"]) is int
    assert type(stored[0]["uniques"]) is int

    with (base / "views.csv").open("r", encoding="utf-8", newline="") as fh:
        csv_rows = list(csv.reader(fh))
    assert csv_rows == [
        ["date", "count", "uniques"],
        ["2025-06-01", "9007199254740993", "9007199254740995"],
        ["2026-01-02", "42", "10"],
    ]

    assert summary["views"] == historical_count + fresh_count
    assert summary["views_days"] == 2
    assert type(summary["views"]) is int
    assert type(summary["views_days"]) is int


def test_cli_main_reports_large_count_summary(tmp_path, fake_api, capsys):
    """cli.main prints exact decimal large integer totals in the stdout summary."""
    large_count = 2**53 + 1  # 9007199254740993
    fake_api["views"] = [
        {"timestamp": "2026-01-02T00:00:00Z", "count": large_count, "uniques": 5}
    ]

    code = cli.main(["--repos", "owner/repo", "--token", "tok", "--out", str(tmp_path)])

    assert code == 0
    captured = capsys.readouterr()
    assert "9007199254740993 views" in captured.out
