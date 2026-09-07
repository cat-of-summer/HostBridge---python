from __future__ import annotations

import os

import pytest

from system import secure


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_harden_file_removes_group_and_other_access(tmp_path):
    target = tmp_path / "daemon.json"
    target.write_text("{}", encoding="utf-8")
    target.chmod(0o644)
    assert secure.is_world_readable(target)

    secure.harden_file(target)
    assert not secure.is_world_readable(target)
    assert target.stat().st_mode & 0o777 == secure.FILE_MODE


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_harden_dir_removes_group_and_other_access(tmp_path):
    target = tmp_path / "state"
    target.mkdir(mode=0o755)
    secure.harden_dir(target)
    assert target.stat().st_mode & 0o777 == secure.DIR_MODE


def test_hardening_a_missing_path_is_a_no_op(tmp_path):
    # Must not raise and, thanks to the autouse guard, must not shell out either.
    secure.harden_file(tmp_path / "absent")
    secure.harden_dir(tmp_path / "absent")


def test_is_world_readable_is_false_for_a_missing_path(tmp_path):
    assert not secure.is_world_readable(tmp_path / "absent")
