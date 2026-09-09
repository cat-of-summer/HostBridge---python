"""Elevation: the mechanism, not the policy.

Which roles need rights is declared in :mod:`app.roles` and tested there. What matters here
is that the mechanism cannot be reached by accident -- the autouse guard in ``conftest``
turns any real attempt into a failed test, because a unit test must never put a consent
dialog in front of whoever is running the suite.
"""

from __future__ import annotations

import sys

import pytest

from app.roles import ELEVATED_ROLES, Role, needs_elevation
from system import elevate


def test_the_guard_stops_a_real_elevation(monkeypatch):
    """Forced unelevated on purpose: the suite may well be running as root in a container,
    and then the call would take the already-elevated path and never reach this guard."""
    monkeypatch.setattr(elevate, "is_elevated", lambda: False)
    with pytest.raises(AssertionError, match="tried to elevate|detached process"):
        elevate.run_elevated(["--install-service"], wait=True)


def test_the_already_elevated_path_is_guarded_too(monkeypatch):
    """There it goes through system.run rather than ShellExecuteExW, so it is the
    subprocess guard that has to catch it."""
    monkeypatch.setattr(elevate, "is_elevated", lambda: True)
    with pytest.raises(AssertionError, match="tried to start a real process"):
        elevate.run_elevated(["--install-service"], wait=True)


def test_the_gui_is_never_in_the_elevated_set():
    """A normal launch must not raise a prompt, which is why the exe carries no manifest."""
    assert not needs_elevation(Role.GUI)
    assert not needs_elevation(Role.CONSOLE)
    assert not needs_elevation(Role.STATUS)
    assert not needs_elevation(Role.VERSION)


@pytest.mark.parametrize("role", sorted(ELEVATED_ROLES, key=lambda r: r.value))
def test_every_privileged_role_is_declared(role):
    assert needs_elevation(role)


def test_starting_the_service_is_a_privileged_role():
    """It is what the window's button runs, and it must not silently do nothing."""
    assert needs_elevation(Role.SERVICE_START)


# ---- how the command is built -------------------------------------------------------


def test_from_source_the_command_is_the_interpreter_plus_main(monkeypatch):
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    executable, prefix = elevate.own_executable()
    assert executable == sys.executable
    assert len(prefix) == 1
    assert prefix[0].endswith("main.py")


def test_frozen_the_daemon_is_started_from_the_windowed_binary(monkeypatch, tmp_path):
    """An elevated console build would park a console window on the desktop for as long as
    the resolver runs."""
    windowed = tmp_path / "hostbridge.exe"
    console = tmp_path / "hostbridge-cli.exe"
    windowed.write_text("", encoding="utf-8")
    console.write_text("", encoding="utf-8")

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(console))

    executable, prefix = elevate.own_executable(windowed=True)
    assert executable == str(windowed)
    assert prefix == []


def test_frozen_without_a_windowed_sibling_falls_back_to_the_current_binary(
    monkeypatch, tmp_path
):
    console = tmp_path / "hostbridge-cli.exe"
    console.write_text("", encoding="utf-8")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(console))

    executable, _ = elevate.own_executable(windowed=True)
    assert executable == str(console)


def test_asking_for_the_console_binary_leaves_it_alone(monkeypatch, tmp_path):
    console = tmp_path / "hostbridge-cli.exe"
    console.write_text("", encoding="utf-8")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(console))

    executable, _ = elevate.own_executable(windowed=False)
    assert executable == str(console)


# ---- outcomes ------------------------------------------------------------------------


def test_a_dismissed_prompt_is_its_own_outcome_not_a_failure(monkeypatch):
    """The user said no. Reporting that as an error would be wrong."""
    monkeypatch.setattr(elevate, "IS_WINDOWS", True)
    monkeypatch.setattr(elevate, "is_elevated", lambda: False)
    monkeypatch.setattr(
        elevate,
        "_shell_execute",
        lambda *args, **kwargs: elevate.Elevation(started=False, cancelled=True),
    )

    outcome = elevate.run_elevated(["--install-service"], wait=True)
    assert outcome.cancelled
    assert not outcome.ok


def test_a_nonzero_exit_is_not_ok(monkeypatch):
    monkeypatch.setattr(elevate, "IS_WINDOWS", True)
    monkeypatch.setattr(elevate, "is_elevated", lambda: False)
    monkeypatch.setattr(
        elevate,
        "_shell_execute",
        lambda *args, **kwargs: elevate.Elevation(started=True, exit_code=5),
    )
    assert not elevate.run_elevated(["--install-service"], wait=True).ok


def test_a_clean_run_is_ok(monkeypatch):
    monkeypatch.setattr(elevate, "IS_WINDOWS", True)
    monkeypatch.setattr(elevate, "is_elevated", lambda: False)
    monkeypatch.setattr(
        elevate,
        "_shell_execute",
        lambda *args, **kwargs: elevate.Elevation(started=True, exit_code=0),
    )
    assert elevate.run_elevated(["--install-service"], wait=True).ok


def test_the_arguments_reach_shell_execute_as_one_command_line(monkeypatch):
    monkeypatch.setattr(elevate, "IS_WINDOWS", True)
    monkeypatch.setattr(elevate, "is_elevated", lambda: False)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    seen = {}

    def _capture(verb, file, parameters, show, wait):
        seen.update(verb=verb, file=file, parameters=parameters, show=show, wait=wait)
        return elevate.Elevation(started=True, exit_code=0)

    monkeypatch.setattr(elevate, "_shell_execute", _capture)
    elevate.run_elevated(["--daemon-foreground"], wait=False)

    assert seen["verb"] == "runas"
    assert seen["parameters"] == "--daemon-foreground"
    assert seen["show"] == elevate.SW_HIDE
    assert seen["wait"] is False


def test_when_already_elevated_nothing_is_prompted(monkeypatch, fake_spawn):
    """Running the console build from an administrator prompt must not ask again."""
    monkeypatch.setattr(elevate, "is_elevated", lambda: True)
    monkeypatch.setattr(
        elevate,
        "_shell_execute",
        lambda *args, **kwargs: pytest.fail("must not prompt when already elevated"),
    )
    fake_spawn(returncode=0)

    assert elevate.run_elevated(["--install-service"], wait=True).ok


def test_on_posix_a_failed_pkexec_suggests_the_sudo_line(monkeypatch):
    """No polkit agent is ordinary on a minimal window manager, and a failure the user
    cannot act on is worse than a command they can paste."""
    monkeypatch.setattr(elevate, "IS_WINDOWS", False)
    monkeypatch.setattr(elevate, "is_elevated", lambda: False)
    monkeypatch.setattr(
        elevate,
        "_spawn_detached",
        lambda argv: elevate.Elevation(started=False, reason="pkexec: not found"),
    )

    outcome = elevate.run_elevated(["--daemon-foreground"], wait=False)
    assert not outcome.started
    assert "sudo" in outcome.reason
