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
