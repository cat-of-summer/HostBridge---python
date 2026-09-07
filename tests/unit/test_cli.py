from __future__ import annotations

import sys

import pytest

from app.cli import dispatch
from app.roles import ELEVATED_ROLES, Role, needs_elevation
from core.version import __version__


def test_version_prints_and_exits_zero(capsys):
    assert dispatch(["--version"]) == 0
    assert capsys.readouterr().out.strip() == f"hostbridge {__version__}"


def test_no_arguments_selects_the_gui_role(capsys):
    # Not implemented yet, but it must be the GUI that is reported as missing -- a default
    # that silently ran the daemon would prompt for elevation on an ordinary launch.
    dispatch([])
    assert Role.GUI.value in capsys.readouterr().err


def test_role_flags_are_mutually_exclusive():
    with pytest.raises(SystemExit):
        dispatch(["--service", "--repair"])


def test_the_gui_never_requires_elevation():
    assert not needs_elevation(Role.GUI)
    assert not needs_elevation(Role.VERSION)
    assert not needs_elevation(Role.STATUS)


@pytest.mark.parametrize("role", sorted(ELEVATED_ROLES, key=lambda r: r.value))
def test_privileged_roles_are_declared(role):
    assert needs_elevation(role)


def test_lang_flag_switches_the_catalogue(monkeypatch, capsys):
    monkeypatch.delenv("HOSTBRIDGE_LANG", raising=False)
    from ui import i18n

    dispatch(["--lang", "ru", "--status"])
    assert i18n.current_language() == "ru"
    i18n.set_language("en")


class TestWindowedBuildHasNoConsole:
    """The reported crash: double-clicking hostbridge.exe died in _not_yet.

    PyInstaller's windowed build leaves sys.stdout and sys.stderr as None, so every path
    that printed anything raised AttributeError before reaching its own logic.
    """

    @pytest.fixture(autouse=True)
    def windowed(self, monkeypatch):
        from app import output

        self.shown: list[tuple[str, bool]] = []
        monkeypatch.setattr(output, "_has_console", None)
        monkeypatch.setattr(sys, "stdout", None)
        monkeypatch.setattr(sys, "stderr", None)
        monkeypatch.setattr(
            output,
            "_message_box",
            lambda message, *, error: self.shown.append((message, error)) or True,
        )
        output.ensure_streams()
        yield
        monkeypatch.setattr(output, "_has_console", None)

    def test_the_default_role_reports_instead_of_crashing(self):
        assert dispatch([]) == 4
        assert self.shown and Role.GUI.value in self.shown[0][0]
        assert self.shown[0][1] is True

    def test_version_reaches_the_user(self):
        assert dispatch(["--version"]) == 0
        assert self.shown == [(f"hostbridge {__version__}", False)]

    def test_help_does_not_crash(self):
        with pytest.raises(SystemExit) as exc:
            dispatch(["--help"])
        assert exc.value.code == 0
        assert self.shown and "usage:" in self.shown[0][0]

    def test_an_argument_error_does_not_crash(self):
        with pytest.raises(SystemExit) as exc:
            dispatch(["--service", "--repair"])
        assert exc.value.code == 2
        assert any("not allowed with" in message for message, _ in self.shown)

    def test_an_unknown_flag_does_not_crash(self):
        with pytest.raises(SystemExit):
            dispatch(["--nonsense"])
