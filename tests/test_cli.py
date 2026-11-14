import argparse
import ctypes
import csv
import errno
import json
import os
import stat
import struct
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

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


def _write_sample(path):
    if path.suffix == ".json":
        cli._write_json(path, [{"label": "café"}])
    else:
        cli._write_csv(path, [["label", "count"], ['café, "quoted"\nline', "1"]])


@pytest.mark.parametrize("suffix", [".json", ".csv"])
@pytest.mark.parametrize("existing", [False, True])
def test_atomic_writer_preserves_format(tmp_path, suffix, existing):
    path = tmp_path / "archive" / ("sample" + suffix)
    if existing:
        path.parent.mkdir()
        path.write_bytes(b"old archive")

    _write_sample(path)

    if suffix == ".json":
        expected = '[\n  {\n    "label": "café"\n  }\n]\n'.replace("\n", os.linesep)
    else:
        expected = 'label,count\r\n"café, ""quoted""\nline",1\r\n'
    assert path.read_bytes() == expected.encode("utf-8")
    assert list(path.parent.iterdir()) == [path]


@pytest.mark.parametrize("suffix", [".json", ".csv"])
@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("stage", ["write", "close", "replace"])
def test_atomic_writer_failure_keeps_destination(tmp_path, monkeypatch, suffix, existing, stage):
    path = tmp_path / ("sample" + suffix)
    original = b"old archive bytes"
    if existing:
        path.write_bytes(original)
    failure = OSError(f"simulated {stage} failure")
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
        stream = FailingStream(open_stream(*args, **kwargs))
        streams.append(stream)
        return stream

    def reject_replace(source, target):
        assert stage == "replace"
        assert streams[0].closed
        assert source.parent == target.parent == tmp_path
        raise failure

    monkeypatch.setattr(cli.os, "fdopen", open_failing_stream)
    monkeypatch.setattr(cli.os, "replace", reject_replace)
    with pytest.raises(OSError) as exc:
        _write_sample(path)

    assert exc.value is failure
    assert len(streams) == 1 and streams[0].closed
    if existing:
        assert path.read_bytes() == original
    assert list(tmp_path.iterdir()) == ([path] if existing else [])


@pytest.mark.parametrize("failure", [TypeError("serialization failed"), KeyboardInterrupt(), SystemExit(2)])
def test_atomic_writer_cleans_up_on_serialization_error(tmp_path, monkeypatch, failure):
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


def test_atomic_writer_reports_failed_cleanup(tmp_path, monkeypatch, capsys):
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
    assert str(leftover) in diagnostic
    assert "cleanup denied" in diagnostic


def test_atomic_writer_cleans_up_if_stream_creation_fails(tmp_path, monkeypatch):
    path = tmp_path / "views.json"
    path.write_bytes(b"old archive")
    failure = OSError("cannot wrap temporary descriptor")
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


@pytest.mark.skipif(os.name != "nt", reason="Windows staging access boundary")
def test_atomic_writer_protects_windows_temp_from_creation(tmp_path, monkeypatch):
    shared = tmp_path / "shared"
    shared.mkdir()
    subprocess.run(
        ["icacls", str(shared), "/grant", "*S-1-5-11:(OI)(CI)(RX)"],
        check=True, capture_output=True,
    )
    path = shared / "views.json"
    path.write_bytes(b"old archive")
    account = subprocess.check_output(["whoami", "/user", "/fo", "csv", "/nh"], text=True)
    sid = next(csv.reader(account.strip().splitlines()))[1]
    subprocess.run(
        ["icacls", str(path), "/grant:r", f"*{sid}:(F)", "/inheritance:r"],
        check=True, capture_output=True,
    )
    acl_file = tmp_path / "acl.txt"
    preserve = cli.preserve_access
    observed = []

    def inspect_before_policy(source, target, previous, **kwargs):
        # Pause at the first point after native creation, before any policy
        # copying or payload. An independent process must not retain a reader.
        reader = subprocess.run([
            sys.executable, "-c",
            "import sys\ntry: open(sys.argv[1], 'rb')\n"
            "except PermissionError: sys.exit(0)\nsys.exit(1)", str(target),
        ], capture_output=True)
        assert reader.returncode == 0, reader.stderr
        subprocess.run(
            ["icacls", str(target), "/save", str(acl_file)],
            check=True, capture_output=True,
        )
        # Check the denial is a protected empty DACL, not merely a sharing
        # restriction that would still allow inherited WRITE_DAC handles.
        assert acl_file.read_text(encoding="utf-16-le").splitlines()[1] == "D:P"
        observed.append(target)
        preserve(source, target, previous, **kwargs)

    monkeypatch.setattr(cli, "preserve_access", inspect_before_policy)
    _write_sample(path)
    assert len(observed) == 1
    assert json.loads(path.read_text(encoding="utf-8")) == [{"label": "café"}]
    assert list(shared.iterdir()) == [path]


@pytest.mark.skipif(os.name != "nt", reason="Windows encrypted attribute refusal")
@pytest.mark.parametrize("encrypted", ["source", "staged"])
def test_atomic_writer_refuses_windows_encryption(tmp_path, monkeypatch, encrypted):
    path = tmp_path / "views.json"
    path.write_bytes(b"old archive")
    inspect = Path.stat

    def attributes(file, *args, **kwargs):
        result = inspect(file, *args, **kwargs)
        if (file == path) == (encrypted == "source"):
            return SimpleNamespace(
                st_mode=result.st_mode,
                st_file_attributes=result.st_file_attributes | stat.FILE_ATTRIBUTE_ENCRYPTED,
            )
        return result

    monkeypatch.setattr(Path, "stat", attributes)
    # Python 3.9's lstat bypasses Path.stat; cover both inspection paths.
    monkeypatch.setattr(Path, "lstat", lambda file: attributes(file))
    with pytest.raises(PermissionError, match="encrypted archives"):
        _write_sample(path)
    assert path.read_bytes() == b"old archive"
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.skipif(os.name != "nt", reason="Windows discretionary ACL test")
@pytest.mark.parametrize("shared", [False, True])
def test_atomic_writer_preserves_windows_access(tmp_path, shared):
    path = tmp_path / "views.json"
    path.write_bytes(b"old archive")
    # Give the current user an explicit ACE and remove inherited grants.
    account = subprocess.check_output(
        ["whoami", "/user", "/fo", "csv", "/nh"], text=True,
    )
    sid = next(csv.reader(account.strip().splitlines()))[1]
    subprocess.run(
        ["icacls", str(path), "/grant:r", f"*{sid}:(F)", "/inheritance:r"],
        check=True, capture_output=True,
    )
    if shared:
        subprocess.run(
            ["icacls", str(path), "/grant", "*S-1-5-11:(R)"],
            check=True, capture_output=True,
        )
    acl_file = tmp_path / "acl.txt"

    def get_acl():
        subprocess.run(
            ["icacls", str(path), "/save", str(acl_file)],
            check=True, capture_output=True,
        )
        return acl_file.read_bytes()

    previous = get_acl()
    _write_sample(path)
    assert get_acl() == previous


@pytest.mark.skipif(os.name != "nt", reason="Windows integrity label test")
def test_atomic_writer_refuses_windows_integrity_label(tmp_path):
    path = tmp_path / "views.json"
    path.write_bytes(b"old archive")
    subprocess.run(
        ["icacls", str(path), "/setintegritylevel", "L"],
        check=True, capture_output=True,
    )
    before = subprocess.check_output(["icacls", str(path)])
    with pytest.raises(PermissionError, match="beyond the DACL"):
        _write_sample(path)
    assert path.read_bytes() == b"old archive"
    assert subprocess.check_output(["icacls", str(path)]) == before
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode test")
def test_atomic_writer_creates_owner_only_file(tmp_path):
    path = tmp_path / "views.json"
    _write_sample(path)
    assert stat.S_IMODE(path.stat().st_mode) & 0o077 == 0


@pytest.mark.skipif(sys.platform != "linux", reason="Linux access policy test")
@pytest.mark.parametrize("mode", [0o600, 0o640])
def test_atomic_writer_preserves_linux_owner_group_and_mode(tmp_path, mode):
    path = tmp_path / "views.json"
    path.write_bytes(b"old archive")
    path.chmod(mode)
    previous = path.stat()
    _write_sample(path)
    current = path.stat()
    assert (current.st_uid, current.st_gid, current.st_mode) == (
        previous.st_uid, previous.st_gid, previous.st_mode,
    )


@pytest.mark.skipif(sys.platform != "linux", reason="Linux POSIX ACL xattr test")
def test_atomic_writer_refuses_extended_linux_acl(tmp_path):
    path = tmp_path / "views.json"
    path.write_bytes(b"old archive")
    attr = "system.posix_acl_access"
    # Linux POSIX ACL v2: owner, named user, group, mask, other.
    entries = [(1, 6, -1), (2, 4, 12345), (4, 0, -1), (16, 4, -1), (32, 0, -1)]
    acl = struct.pack("<I", 2) + b"".join(struct.pack("<HHi", *entry) for entry in entries)
    os.setxattr(path, attr, acl)
    previous_acl = os.getxattr(path, attr)
    with pytest.raises(PermissionError, match="extended POSIX ACL"):
        _write_sample(path)
    assert path.read_bytes() == b"old archive"
    assert os.getxattr(path, attr) == previous_acl
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize("case", [
    "no_acl", "acl", "short_record", "unsupported", "not_native", "not_valid",
    "denied_volume", "denied_acl",
])
def test_macos_acl_query_requires_complete_supported_evidence(monkeypatch, case):
    from traffic_archive import _archive_access

    acl_attribute = 0x00400000
    calls = []

    class Query:
        def __call__(self, fd, attributes, result, size, options):
            assert fd == 41 and options == 4
            fields = struct.unpack("=HH5I", attributes.raw[:24])
            assert fields[:2] == (5, 0)
            calls.append(fields[2:])
            if fields[3]:
                assert fields[2:] == (0, 0xC0000000, 0, 0, 0)
                if case == "denied_volume":
                    ctypes.set_errno(errno.EACCES)
                    return -1
                valid = acl_attribute if case not in ("unsupported", "not_valid") else 0
                native = acl_attribute if case not in ("unsupported", "not_native") else 0
                data = struct.pack("=11I", 44, valid, 0, 0, 0, 0, native, 0, 0, 0, 0)
            else:
                assert fields[2:] == (acl_attribute, 0, 0, 0, 0)
                if case == "denied_acl":
                    ctypes.set_errno(errno.EACCES)
                    return -1
                length = 8 if case == "short_record" else (56 if case == "acl" else 12)
                data = struct.pack("=IiI", length, 8, 44 if case == "acl" else 0)
            assert len(data) == size
            ctypes.memmove(result, data, len(data))
            return 0

    class Library:
        fgetattrlist = Query()

    monkeypatch.setattr(ctypes, "CDLL", lambda *args, **kwargs: Library())
    if case == "no_acl":
        _archive_access._require_macos_no_acl(41)
        assert len(calls) == 2
    else:
        with pytest.raises(OSError):
            _archive_access._require_macos_no_acl(41)
        assert len(calls) == (1 if case in ("unsupported", "not_native", "not_valid", "denied_volume") else 2)


@pytest.mark.skipif(sys.platform != "darwin", reason="native macOS create/update test")
@pytest.mark.parametrize("mode", [0o600, 0o640])
@pytest.mark.parametrize("suffix", [".json", ".csv"])
def test_atomic_writer_updates_ordinary_macos_archive(tmp_path, mode, suffix):
    subprocess.run(["chmod", "-N", str(tmp_path)], check=True, capture_output=True)
    path = tmp_path / ("archive" + suffix)
    _write_sample(path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    path.chmod(mode)
    path.write_bytes(b"old archive")
    previous = path.stat()

    _write_sample(path)

    current = path.stat()
    assert (current.st_uid, current.st_gid, current.st_mode) == (
        previous.st_uid, previous.st_gid, previous.st_mode,
    )
    assert path.read_bytes() != b"old archive"
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.skipif(sys.platform != "darwin", reason="native macOS ACL refusal test")
@pytest.mark.parametrize("inherited", [False, True])
def test_atomic_writer_refuses_macos_acl_before_payload(tmp_path, monkeypatch, inherited):
    shared = tmp_path / "archive"
    shared.mkdir()
    subprocess.run(["chmod", "-N", str(shared)], check=True, capture_output=True)
    path = shared / "views.json"
    path.write_bytes(b"old archive")
    policy_path = shared if inherited else path
    policy = "everyone allow read,file_inherit" if inherited else "everyone allow read"
    subprocess.run(["chmod", "+a", policy, str(policy_path)], check=True, capture_output=True)
    previous = subprocess.check_output(["ls", "-le", str(path)])

    def reject_payload(*args, **kwargs):
        pytest.fail("payload must not be serialized for an unsupported access policy")

    monkeypatch.setattr(cli.json, "dumps", reject_payload)
    with pytest.raises(PermissionError, match="macOS ACL"):
        _write_sample(path)
    assert path.read_bytes() == b"old archive"
    assert subprocess.check_output(["ls", "-le", str(path)]) == previous
    assert list(shared.iterdir()) == [path]


@pytest.mark.parametrize("error", [errno.EACCES, errno.ENOTSUP])
def test_atomic_writer_refuses_unreadable_or_unsupported_access(tmp_path, monkeypatch, error):
    from traffic_archive import _archive_access

    path = tmp_path / "views.json"
    path.write_bytes(b"old archive")
    previous_mode = path.stat().st_mode

    def deny(*args, **kwargs):
        raise OSError(error, "cannot inspect archive access")

    if sys.platform == "linux":
        monkeypatch.setattr(os, "getxattr", deny)
    elif sys.platform == "darwin":
        monkeypatch.setattr(_archive_access, "_require_macos_no_acl", deny)
    else:
        monkeypatch.setattr(_archive_access, "_copy_windows_access", deny)
    with pytest.raises(PermissionError, match="Cannot preserve archive access") as exc:
        _write_sample(path)
    assert isinstance(exc.value.__cause__, OSError)
    assert path.read_bytes() == b"old archive"
    assert path.stat().st_mode == previous_mode
    assert list(tmp_path.iterdir()) == [path]


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
