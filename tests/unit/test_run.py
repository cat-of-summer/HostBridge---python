from __future__ import annotations

import subprocess

import pytest

from system import run as run_module
from system.run import CommandError, Completed, run


def test_the_guard_stops_a_real_process():
    with pytest.raises(AssertionError, match="tried to start a real process"):
        run(["netsh", "interface", "ipv4", "show", "config"])


def test_arguments_are_stringified_and_passed_as_a_tuple(fake_spawn):
    calls = fake_spawn()
    run(["icacls", 53, "/grant:r"])
    assert calls == [("icacls", "53", "/grant:r")]


def test_output_prefers_stderr(fake_spawn):
    fake_spawn(returncode=1, stdout="out", stderr="  boom  ")
    result = run(["sc", "query"])
    assert not result.ok
    assert result.output == "boom"


def test_output_falls_back_to_stdout(fake_spawn):
    fake_spawn(returncode=1, stdout="  detail  ", stderr="")
    assert run(["sc", "query"]).output == "detail"


def test_check_raises_on_a_non_zero_exit(fake_spawn):
    fake_spawn(returncode=2, stderr="denied")
    with pytest.raises(CommandError, match="exited 2"):
        run(["sc", "create"], check=True)


def test_a_missing_binary_becomes_a_command_error(monkeypatch):
    def _missing(argv, **_kwargs):
        raise FileNotFoundError(argv[0])

    monkeypatch.setattr(run_module, "_spawn", _missing)
    with pytest.raises(CommandError, match="not found"):
        run(["resolvectl", "status"])


def test_a_timeout_becomes_a_command_error(monkeypatch):
    def _slow(argv, **_kwargs):
        raise subprocess.TimeoutExpired(argv, 1.0)

    monkeypatch.setattr(run_module, "_spawn", _slow)
    with pytest.raises(CommandError, match="timed out"):
        run(["netsh"], timeout=1.0)


def test_completed_reports_success():
    assert Completed(("true",), 0, "", "").ok
