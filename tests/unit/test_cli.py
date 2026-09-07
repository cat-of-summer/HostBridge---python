from __future__ import annotations

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
