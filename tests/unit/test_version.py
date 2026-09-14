r"""The release tag and the binary must agree, and there must be only one place to look.

The reusable CI workflow rejects any tag that is not ``^v[0-9]+(\.[0-9]+)*$`` and builds
whatever the tree contains. A binary whose ``--version`` disagrees with the tag it was cut
from is not something a user can diagnose from the outside, so the stamping that keeps them
in step is checked here.

``pyproject.toml`` carries no version literal of its own -- it declares the field dynamic
and reads :mod:`core.version` -- so what used to be an equality check between two files is
now a check that the indirection is still wired up.
"""

from __future__ import annotations

import re
from pathlib import Path

from core.version import __version__

ROOT = Path(__file__).resolve().parents[2]


def _pyproject() -> str:
    return (ROOT / "pyproject.toml").read_text(encoding="utf-8")


def test_pyproject_takes_the_version_from_the_module():
    text = _pyproject()
    assert 'dynamic = ["version"]' in text
    assert 'version = { attr = "core.version.__version__" }' in text


def test_pyproject_declares_no_version_of_its_own():
    """A static literal here would shadow the dynamic field and go quietly out of date."""
    assert re.search(r'(?m)^version\s*=\s*"', _pyproject()) is None


def test_version_is_a_valid_release_tag():
    assert re.fullmatch(r"[0-9]+(\.[0-9]+)*", __version__), (
        f"{__version__!r} cannot be tagged as v{__version__} under the workflow's rule"
    )


# ---- stamping the release tag into the tree -----------------------------------------


def _stamper():
    """Import the build script by path: build/ is not a package and must not become one."""
    import importlib.util

    path = ROOT / "build" / "version_stamp.py"
    spec = importlib.util.spec_from_file_location("version_stamp", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_a_release_tag_yields_its_version():
    stamp = _stamper()
    assert stamp.version_of("v1.2.3") == "1.2.3"
    assert stamp.version_of("v2") == "2"
    assert stamp.version_of("v0.1") == "0.1"


def test_a_branch_prefixed_tag_keeps_only_the_version():
    """MULTIPLE_PACKAGES tags look like ``main/v1.2.3``."""
    assert _stamper().version_of("main/v1.2.3") == "1.2.3"


def test_anything_that_is_not_a_release_tag_stamps_nothing():
    """Every push builds; only some pushes are releases, and that is not an error."""
    stamp = _stamper()
    for ref in ("", "main", "feature/thing", "1.2.3", "v1.2.3-rc1", "release-1"):
        assert stamp.version_of(ref) == "", ref


def test_the_normalised_ref_is_read_without_its_v():
    """``REF_NAME_NORM`` is the workflow's own form: the tag with the ``v`` already gone."""
    stamp = _stamper()
    assert stamp.version_of_normalised("1.2.3") == "1.2.3"
    assert stamp.version_of_normalised("2") == "2"
    assert stamp.version_of_normalised("main-1.2.3") == "1.2.3"
    assert stamp.version_of_normalised("feature-thing") == ""
    assert stamp.version_of_normalised("pr-12.x") == ""


def test_the_version_is_taken_from_the_environment_the_workflow_sets(monkeypatch):
    stamp = _stamper()
    monkeypatch.delenv("GITHUB_REF_NAME", raising=False)
    monkeypatch.delenv("GITHUB_REF", raising=False)

    monkeypatch.setenv("HOSTBRIDGE_VERSION", "1.2.3")
    assert stamp.requested_version([]) == "1.2.3"

    monkeypatch.setenv("HOSTBRIDGE_VERSION", "main-4.5")
    assert stamp.requested_version([]) == "4.5"

    monkeypatch.setenv("HOSTBRIDGE_VERSION", "")
    monkeypatch.setenv("GITHUB_REF_NAME", "v6.7")
    assert stamp.requested_version([]) == "6.7"


def test_an_argument_beats_the_environment(monkeypatch):
    stamp = _stamper()
    monkeypatch.setenv("HOSTBRIDGE_VERSION", "1.2.3")
    assert stamp.requested_version(["version_stamp.py", "v9.9"]) == "9.9"


def test_the_tag_is_read_from_either_github_variable(monkeypatch):
    stamp = _stamper()

    monkeypatch.setenv("GITHUB_REF_NAME", "v3.4")
    monkeypatch.delenv("GITHUB_REF", raising=False)
    assert stamp.tag_from_environment() == "v3.4"

    monkeypatch.delenv("GITHUB_REF_NAME", raising=False)
    monkeypatch.setenv("GITHUB_REF", "refs/tags/v5.6")
    assert stamp.tag_from_environment() == "v5.6"

    monkeypatch.setenv("GITHUB_REF", "refs/heads/main")
    assert stamp.tag_from_environment() == ""


def test_stamping_rewrites_the_module(tmp_path):
    stamp = _stamper()
    (tmp_path / "core").mkdir()
    (tmp_path / "core" / "version.py").write_text(
        '__version__ = "0.0.0"\n\nSCHEMA_VERSION = 1\n', encoding="utf-8"
    )

    changes = stamp.stamp("9.9.9", root=tmp_path)
    assert len(changes) == 1
    text = (tmp_path / "core" / "version.py").read_text(encoding="utf-8")
    assert '__version__ = "9.9.9"' in text
    assert "SCHEMA_VERSION = 1" in text, "the rest of the file must survive untouched"


def test_stamping_the_version_already_there_changes_nothing(tmp_path):
    stamp = _stamper()
    (tmp_path / "core").mkdir()
    (tmp_path / "core" / "version.py").write_text('__version__ = "1.0"\n', encoding="utf-8")
    assert stamp.stamp("1.0", root=tmp_path) == []


def test_the_stamper_finds_the_line_the_module_actually_uses():
    """A pattern that matched nothing would let a release build ship the placeholder."""
    stamp = _stamper()
    text = (ROOT / "core" / "version.py").read_text(encoding="utf-8")
    assert stamp.MODULE_VERSION.search(text).group(1) == __version__
