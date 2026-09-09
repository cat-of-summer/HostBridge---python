r"""The release tag, the package metadata and the binary must all agree.

The reusable CI workflow rejects any tag that is not ``^v[0-9]+(\.[0-9]+)*$`` and builds
whatever the tree contains. A binary whose ``--version`` disagrees with the tag it was cut
from is not something a user can diagnose from the outside, so it is checked here.
"""

from __future__ import annotations

import re
from pathlib import Path

from core.version import __version__

ROOT = Path(__file__).resolve().parents[2]


def _pyproject_version() -> str:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'(?m)^version\s*=\s*"([^"]+)"', text)
    assert match, "pyproject.toml has no top-level version"
    return match.group(1)


def test_version_matches_pyproject():
    assert __version__ == _pyproject_version()


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


def test_stamping_rewrites_both_files(tmp_path):
    stamp = _stamper()
    (tmp_path / "core").mkdir()
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "hostbridge"\nversion = "0.1.0"\n\n'
        '[tool.ruff]\ntarget-version = "py310"\n',
        encoding="utf-8",
    )
    (tmp_path / "core" / "version.py").write_text(
        '__version__ = "0.1.0"\n\nSCHEMA_VERSION = 1\n', encoding="utf-8"
    )

    changes = stamp.stamp("9.9.9", root=tmp_path)
    assert len(changes) == 2
    assert 'version = "9.9.9"' in (tmp_path / "pyproject.toml").read_text(encoding="utf-8")
    assert '__version__ = "9.9.9"' in (tmp_path / "core" / "version.py").read_text(
        encoding="utf-8"
    )


def test_ruffs_target_version_is_not_mistaken_for_the_package_version(tmp_path):
    """``target-version`` sits in the same file and would be a miserable thing to corrupt."""
    stamp = _stamper()
    (tmp_path / "core").mkdir()
    (tmp_path / "pyproject.toml").write_text(
        '[tool.ruff]\ntarget-version = "py310"\n\n[project]\nversion = "0.1.0"\n',
        encoding="utf-8",
    )
    (tmp_path / "core" / "version.py").write_text('__version__ = "0.1.0"\n', encoding="utf-8")

    stamp.stamp("2.0", root=tmp_path)
    text = (tmp_path / "pyproject.toml").read_text(encoding="utf-8")
    assert 'target-version = "py310"' in text
    assert 'version = "2.0"' in text


def test_stamping_the_version_already_there_changes_nothing(tmp_path):
    stamp = _stamper()
    (tmp_path / "core").mkdir()
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "1.0"\n', encoding="utf-8")
    (tmp_path / "core" / "version.py").write_text('__version__ = "1.0"\n', encoding="utf-8")
    assert stamp.stamp("1.0", root=tmp_path) == []


def test_the_stamper_finds_the_same_line_the_check_does():
    """Both look for the version in pyproject.toml, and they must agree on which line.

    If one matched a line the other did not, a stamped build would sail past a check that
    was reading something else entirely -- which is the whole value of the check.
    """
    stamp = _stamper()
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert stamp.PYPROJECT_VERSION.search(text).group(1) == _pyproject_version()


def test_the_committed_tree_is_consistent_after_a_stamp(tmp_path):
    """Stamping is what the version test then checks, so the pair must stay in step."""
    import shutil

    stamp = _stamper()
    (tmp_path / "core").mkdir()
    shutil.copy(ROOT / "pyproject.toml", tmp_path / "pyproject.toml")
    shutil.copy(ROOT / "core" / "version.py", tmp_path / "core" / "version.py")

    stamp.stamp("7.8.9", root=tmp_path)
    pyproject = re.search(
        r'(?m)^version\s*=\s*"([^"]+)"', (tmp_path / "pyproject.toml").read_text("utf-8")
    ).group(1)
    module = re.search(
        r'(?m)^__version__\s*=\s*"([^"]+)"',
        (tmp_path / "core" / "version.py").read_text("utf-8"),
    ).group(1)
    assert pyproject == module == "7.8.9"
