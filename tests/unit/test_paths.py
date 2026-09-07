from __future__ import annotations

import os
from pathlib import Path

from core import paths


def test_state_directories_follow_the_environment_overrides(tmp_path):
    assert paths.user_home() == Path(os.environ["HOSTBRIDGE_HOME"])
    assert paths.machine_home() == Path(os.environ["HOSTBRIDGE_MACHINE_HOME"])


def test_daemon_owned_files_live_in_machine_state():
    machine = paths.machine_home()
    for probe in (paths.domains_file(), paths.netstate_file(), paths.daemon_file()):
        assert probe.parent == machine


def test_preferences_live_in_user_state():
    assert paths.config_file().parent == paths.user_home()


def test_machine_home_has_a_platform_default(monkeypatch):
    monkeypatch.delenv("HOSTBRIDGE_MACHINE_HOME", raising=False)
    resolved = paths.machine_home()
    # Never the user's home: the service runs as SYSTEM or root and the store has to
    # survive a change of interactive user.
    assert Path.home() not in resolved.parents


def test_resource_dir_resolves_inside_a_frozen_bundle(monkeypatch, tmp_path):
    monkeypatch.setattr(paths.sys, "_MEIPASS", str(tmp_path), raising=False)
    assert paths.resource_dir("lang") == tmp_path / "lang"
    assert paths.resource_dir("assets/icons") == tmp_path / "assets" / "icons"
