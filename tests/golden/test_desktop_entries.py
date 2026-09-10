"""The desktop entries, compared as text.

Golden because a desktop entry is a contract with the desktop environment, not with us: a
key renamed or a category dropped changes whether the application appears in the menu at
all, and the failure is silent -- the launcher just skips a file it does not like.
"""

from __future__ import annotations

import os

import pytest

from system import desktop_linux

EXECUTABLE = "/opt/hostbridge/hostbridge"

EXPECTED = """\
[Desktop Entry]
Type=Application
Version=1.0
Name=HostBridge
GenericName=Local DNS manager
Comment=Local DNS for development domains
Exec=/opt/hostbridge/hostbridge
Terminal=false
Categories=Development;Network;Utility;
Keywords=dns;domain;docker;traefik;
StartupNotify=true
StartupWMClass=hostbridge
X-GNOME-Autostart-enabled=true
"""


def test_the_entry_is_exactly_this():
    assert desktop_linux.render_entry(EXECUTABLE) == EXPECTED


def test_an_icon_is_added_as_its_own_key():
    text = desktop_linux.render_entry(EXECUTABLE, icon="/opt/hostbridge/icon.png")
    assert "Icon=/opt/hostbridge/icon.png\n" in text
    assert text.index("Icon=") < text.index("Terminal=")


def test_no_icon_leaves_no_empty_key():
    """``Icon=`` with nothing after it makes some launchers drop the entry."""
    assert "Icon=" not in desktop_linux.render_entry(EXECUTABLE)


def test_a_path_with_a_space_is_quoted():
    """An unquoted Exec with a space is silently refused by the desktop, not reported."""
    text = desktop_linux.render_entry("/home/dev/My Apps/hostbridge")
    assert "Exec='/home/dev/My Apps/hostbridge'\n" in text


# ---- where the files go --------------------------------------------------------------


def test_the_xdg_variables_are_honoured(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

    assert desktop_linux.applications_dir() == tmp_path / "data" / "applications"
    assert desktop_linux.autostart_dir() == tmp_path / "config" / "autostart"


def test_a_relative_xdg_value_is_ignored(monkeypatch):
    """The spec says so, and an entry written into the working directory is unfindable."""
    monkeypatch.setenv("XDG_DATA_HOME", "relative/path")
    assert desktop_linux.applications_dir().is_absolute()
    assert "relative" not in str(desktop_linux.applications_dir())


def test_install_writes_both_copies():
    plan = desktop_linux.plan_install(EXECUTABLE)
    names = [path.name for path, _text in plan.write]
    assert names == [desktop_linux.ENTRY_NAME, desktop_linux.ENTRY_NAME]
    assert plan.write[0][0].parent.name == "applications"
    assert plan.write[1][0].parent.name == "autostart"
    assert plan.write[0][1] == plan.write[1][1], "both copies are the same entry"


def test_autostart_can_be_declined():
    plan = desktop_linux.plan_install(EXECUTABLE, autostart=False)
    assert [path.parent.name for path, _ in plan.write] == ["applications"]


def test_uninstall_names_both_and_writes_nothing():
    plan = desktop_linux.plan_uninstall()
    assert not plan.write
    assert [path.parent.name for path in plan.remove] == ["applications", "autostart"]


# ---- actually putting them on disk ---------------------------------------------------


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_the_round_trip_leaves_nothing_behind(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

    written = desktop_linux.execute(desktop_linux.plan_install(EXECUTABLE))
    assert len(written) == 2
    assert desktop_linux.installed() and desktop_linux.autostart_enabled()
    for path in written:
        # A desktop entry that is not world-readable is skipped by some launchers, and
        # tracking that down is a miserable afternoon.
        assert path.stat().st_mode & 0o777 == 0o644
        assert path.read_text(encoding="utf-8") == EXPECTED

    removed = desktop_linux.execute(desktop_linux.plan_uninstall())
    assert len(removed) == 2
    assert not desktop_linux.installed() and not desktop_linux.autostart_enabled()


def test_removing_what_is_not_there_is_not_an_error(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    assert desktop_linux.execute(desktop_linux.plan_uninstall()) == []


def test_the_absoluteness_check_is_native_to_the_platform(monkeypatch, tmp_path):
    r"""Regression: the check used to be a leading ``/``, which broke the Windows runner.

    On Linux the two questions are identical, so the fault was invisible there. The Windows
    runner's temporary directory is ``C:\Users\...``, so the variable was judged relative and
    ignored, and the test read the runner's real home instead of the one it had just set.
    """
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    assert desktop_linux.applications_dir().is_relative_to(tmp_path)
    assert desktop_linux.autostart_dir().is_relative_to(tmp_path)
