from __future__ import annotations

import json

import pytest

from core import jsonio


def test_round_trip(tmp_path):
    target = tmp_path / "sample.json"
    jsonio.write_json_atomic(target, {"domains": ["shop.test"]}, harden=False)
    assert jsonio.read_json(target) == {"domains": ["shop.test"]}


def test_read_tolerates_a_byte_order_mark(tmp_path):
    target = tmp_path / "bom.json"
    target.write_bytes(b"\xef\xbb\xbf" + json.dumps({"a": 1}).encode("utf-8"))
    assert jsonio.read_json(target) == {"a": 1}


def test_read_returns_the_default_for_missing_and_broken_files(tmp_path):
    assert jsonio.read_json(tmp_path / "absent.json", default={"d": True}) == {"d": True}
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    assert jsonio.read_json(broken, default=None) is None


def test_a_crash_between_write_and_replace_leaves_the_original_intact(tmp_path, monkeypatch):
    target = tmp_path / "domains.json"
    jsonio.write_json_atomic(target, {"generation": 1}, harden=False)

    def _explode(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(jsonio.os, "replace", _explode)
    with pytest.raises(OSError):
        jsonio.write_json_atomic(target, {"generation": 2}, harden=False)

    assert jsonio.read_json(target) == {"generation": 1}
    # And the temporary file is not left behind to accumulate.
    assert [p.name for p in tmp_path.iterdir()] == ["domains.json"]


def test_write_creates_missing_parent_directories(tmp_path):
    target = tmp_path / "deep" / "nested" / "file.json"
    jsonio.write_json_atomic(target, [1, 2, 3], harden=False)
    assert jsonio.read_json(target) == [1, 2, 3]


def test_an_ordinary_write_never_shells_out_even_on_windows(tmp_path, monkeypatch):
    """Regression: the default write must not spawn a process on any platform.

    This forces the Windows branch while running on Linux, because the original bug was
    invisible on POSIX -- there ``harden_file`` is a ``chmod`` -- and only surfaced on the
    Windows CI runner, where it is an ``icacls`` process. The daemon rewrites the domain
    store on every Docker event, so a process spawn per write is not acceptable.
    """
    from system import secure

    monkeypatch.setattr(secure, "IS_WINDOWS", True)
    # The autouse no_subprocess fixture turns any real spawn into a failure, so this
    # completing is the assertion.
    jsonio.write_json_atomic(tmp_path / "quiet.json", {"a": 1})
    assert jsonio.read_json(tmp_path / "quiet.json") == {"a": 1}


def test_explicit_hardening_tightens_the_acl_on_windows(tmp_path, monkeypatch, fake_spawn):
    """The opt-in path is what ``daemon.json`` uses, so it has to actually run."""
    from system import secure

    monkeypatch.setattr(secure, "IS_WINDOWS", True)
    monkeypatch.setenv("USERNAME", "dev")
    monkeypatch.delenv("USERDOMAIN", raising=False)
    calls = fake_spawn()

    target = tmp_path / "daemon.json"
    jsonio.write_json_atomic(target, {"token": "s3cret"}, harden=True)

    assert calls == [("icacls", str(target), "/inheritance:r", "/grant:r", "dev:F")]


def test_a_failed_hardening_does_not_lose_the_written_file(tmp_path, monkeypatch, fake_spawn):
    from system import secure

    monkeypatch.setattr(secure, "IS_WINDOWS", True)
    monkeypatch.setenv("USERNAME", "dev")
    fake_spawn(returncode=5, stderr="access denied")

    target = tmp_path / "daemon.json"
    jsonio.write_json_atomic(target, {"token": "s3cret"}, harden=True)

    # Hardening is best-effort: the daemon must still start, and the token file must exist.
    assert jsonio.read_json(target) == {"token": "s3cret"}
