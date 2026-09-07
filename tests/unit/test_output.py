"""Regression cover for the windowed build having no console.

``hostbridge.exe`` is built with ``console=False``, and there PyInstaller leaves
``sys.stdout`` and ``sys.stderr`` as ``None``. The first Windows build crashed on launch
with ``AttributeError: 'NoneType' object has no attribute 'write'``. Every test here forces
that state on purpose, so the failure cannot come back on any platform.

The stream substitution happens inside each test body rather than in a fixture: pytest's
capture manager reinstates ``sys.stdout``/``sys.stderr`` between the setup and call phases,
so a fixture that patched them would be silently undone before the test ran.
"""

from __future__ import annotations

import os
import sys

import pytest

from app import output


def _go_windowed(monkeypatch) -> list[tuple[str, bool]]:
    """Drop the standard streams and capture whatever a message box would have shown."""
    shown: list[tuple[str, bool]] = []
    monkeypatch.setattr(output, "_has_console", None)
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    monkeypatch.setattr(
        output, "_message_box", lambda message, *, error: shown.append((message, error)) or True
    )
    return shown


@pytest.fixture(autouse=True)
def _reset_console_state(monkeypatch):
    """Never let one test's cached answer leak into the next."""
    monkeypatch.setattr(output, "_has_console", None)


def test_has_console_is_false_without_streams(monkeypatch):
    _go_windowed(monkeypatch)
    assert not output.has_console()


def test_ensure_streams_binds_writable_streams(monkeypatch):
    _go_windowed(monkeypatch)
    assert output.ensure_streams() is False
    # The point of the exercise: arbitrary code may now write without crashing.
    sys.stdout.write("into the void")
    sys.stderr.write("likewise")
    sys.stdout.flush()


def test_ensure_streams_reports_and_caches_a_real_console():
    assert output.ensure_streams() is True
    assert output.ensure_streams() is True


def test_emit_falls_back_to_a_message_box_without_a_console(monkeypatch):
    shown = _go_windowed(monkeypatch)
    output.ensure_streams()
    output.emit("something happened", error=True)
    assert shown == [("something happened", True)]


def test_emit_never_raises_when_every_route_fails(monkeypatch):
    _go_windowed(monkeypatch)
    monkeypatch.setattr(output, "_message_box", lambda message, *, error: False)
    output.ensure_streams()
    output.emit("nowhere to go")  # must simply return


def test_emit_writes_to_the_stream_when_a_console_exists(capsys, monkeypatch):
    monkeypatch.setattr(output, "_has_console", True)
    output.emit("to stdout")
    output.emit("to stderr", error=True)
    captured = capsys.readouterr()
    assert captured.out == "to stdout\n"
    assert captured.err == "to stderr\n"


def test_emit_does_not_double_the_trailing_newline(capsys, monkeypatch):
    monkeypatch.setattr(output, "_has_console", True)
    output.emit("already ended\n")
    assert capsys.readouterr().out == "already ended\n"


@pytest.mark.skipif(os.name == "nt", reason="tests the non-Windows fallback")
def test_message_box_is_a_no_op_off_windows():
    assert output._message_box("x", error=False) is False
