import hashlib
import subprocess
from unittest.mock import patch

import pytest

from core.git import GitLicenseInputError, fetch_snapshot

HEAD = "a" * 40
ROOT = (
    b"100644 blob "
    + b"1" * 40
    + b"\tLICENSE\0"
    + b"120000 blob "
    + b"2" * 40
    + b"\tCOPYING\0"
    + b"040000 tree "
    + b"3" * 40
    + b"\tLICENSES\0"
    + b"040000 tree "
    + b"4" * 40
    + b"\tsrc\0"
)


def fake_git(calls, *, raw=b"A public license", oversized=False, empty=False):
    def execute(command, **kwargs):
        calls.append(command)
        position = command.index("-C") + 2
        args = command[position:]
        operation = args[0]
        if empty and operation == "fetch":
            raise subprocess.CalledProcessError(128, command)
        if operation == "ls-remote":
            output = b""
        elif operation == "rev-parse":
            output = HEAD.encode()
        elif operation == "show":
            output = b"2026-10-01T10:00:00+08:00"
        elif operation == "ls-tree":
            output = ROOT if args[-1] == HEAD else b"100644 blob " + b"5" * 40 + b"\tlicense-file.any\0"
        elif operation == "cat-file":
            output = str(1_048_577 if oversized else len(raw)).encode() if args[1] == "-s" else raw
        else:
            output = b""
        return subprocess.CompletedProcess(command, 0, output, b"")

    return execute


def test_only_root_regular_candidates_and_license_directory_no_checkout(tmp_path, settings):
    settings.WORK_ROOT = tmp_path
    calls = []
    with patch("core.git.subprocess.run", side_effect=fake_git(calls)):
        snapshot = fetch_snapshot("https://github.com/acme/repo")
    assert set(snapshot.files) == {"LICENSE", "LICENSES/license-file.any"}
    assert snapshot.sha == HEAD and not list(tmp_path.iterdir())
    assert all("checkout" not in call and "submodule" not in call for call in calls)
    assert all("protocol.file.allow=never" in call and "credential.helper=" in call for call in calls)


def test_binary_license_not_silently_omitted_or_lossily_normalized(tmp_path, settings):
    settings.WORK_ROOT = tmp_path
    raw = b"some license\xff changed legal text"
    with patch("core.git.subprocess.run", side_effect=fake_git([], raw=raw)):
        snapshot = fetch_snapshot("https://github.com/acme/repo")
    assert snapshot.files["LICENSE"] == ""
    assert snapshot.decoding_errors["LICENSE"] == hashlib.sha256(raw).hexdigest()


def test_verified_empty_repository_has_no_fabricated_commit(tmp_path, settings):
    settings.WORK_ROOT = tmp_path
    with patch("core.git.subprocess.run", side_effect=fake_git([], empty=True)):
        snapshot = fetch_snapshot("https://github.com/acme/repo")
    assert snapshot.sha == "" and snapshot.committed_at is None and snapshot.files == {}


def test_oversized_blob_stops_before_loading_and_cleans_scratch(tmp_path, settings):
    settings.WORK_ROOT = tmp_path
    calls = []
    with (
        patch("core.git.subprocess.run", side_effect=fake_git(calls, oversized=True)),
        pytest.raises(GitLicenseInputError) as error,
    ):
        fetch_snapshot("https://github.com/acme/repo")
    assert error.value.snapshot.sha == HEAD
    assert error.value.evidence["files"][0]["git_blob"] == "1" * 40
    assert not list(tmp_path.iterdir())
    assert not any("blob" in call for call in calls if "cat-file" in call)
