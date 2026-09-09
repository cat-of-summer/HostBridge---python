r"""Make the tree say what the release tag says, just before it is built.

The tag is the thing a user sees and the thing they will quote in a bug report, so the tag
wins. Without this the binary carries whatever ``pyproject.toml`` happened to hold when the
commit was made, and a ``--version`` that disagrees with the release it came from is not
something anyone can diagnose from the outside.

Run from ``build/build.sh`` and ``build/build.ps1`` before PyInstaller collects anything.
Ordering matters twice over:

* the frozen binary embeds ``core/version.py``, so the stamp has to land before collection;
* the reusable workflow runs ``BUILD_COMMAND`` **before** ``CI_COMMAND``, so the version test
  then checks the stamped pair rather than the committed one -- which is what makes the
  check meaningful on a release build instead of merely self-consistent.

Nothing is committed. The working tree of a CI runner is disposable and the repository keeps
whatever version it had; only the artifact is stamped.

Doing nothing is the normal outcome. A branch push, a pull request and a developer's local
build all have no release tag, and this exits quietly having touched no file.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: The workflow refuses any tag that is not ``v#``, ``v#.#`` or ``v#.#.#``, and with
#: MULTIPLE_PACKAGES it prefixes a branch: ``main/v1.2.3``. Only the last segment is a
#: version, so the prefix is dropped before anything else looks at it.
TAG_PATTERN = re.compile(r"^v(?P<version>[0-9]+(?:\.[0-9]+)*)$")

#: Anchored at the start of a line so ``target-version`` in [tool.ruff] cannot match. The
#: same expression the version test uses, deliberately: one of them finding a line the other
#: does not would defeat the check entirely.
PYPROJECT_VERSION = re.compile(r'(?m)^version\s*=\s*"([^"]+)"')

MODULE_VERSION = re.compile(r'(?m)^__version__\s*=\s*"([^"]+)"')


def tag_from_environment() -> str:
    """The release tag this build was cut from, or "" when there is not one."""
    name = os.environ.get("GITHUB_REF_NAME", "").strip()
    if not name:
        ref = os.environ.get("GITHUB_REF", "").strip()
        name = ref[len("refs/tags/") :] if ref.startswith("refs/tags/") else ""
    return name


def version_of(tag: str) -> str:
    """The bare version in ``tag``, or "" if it is not a release tag at all.

    A branch name reaching here is ordinary rather than an error: every push builds, and
    only some pushes are releases.
    """
    if not tag:
        return ""
    match = TAG_PATTERN.match(tag.strip().split("/")[-1])
    return match.group("version") if match else ""


def _replace(path: Path, pattern: re.Pattern[str], version: str, template: str) -> str | None:
    """Rewrite the one line ``pattern`` matches. Returns the previous value."""
    text = path.read_text(encoding="utf-8")
    match = pattern.search(text)
    if match is None:
        raise SystemExit(f"{path.name}: no version line was found to stamp")
    if match.group(1) == version:
        return None
    updated = text[: match.start()] + template.format(version=version) + text[match.end() :]
    path.write_text(updated, encoding="utf-8", newline="\n")
    return match.group(1)


def stamp(version: str, root: Path = ROOT) -> list[str]:
    """Write ``version`` into both files that carry it. Returns a line per change."""
    changes: list[str] = []

    previous = _replace(
        root / "pyproject.toml", PYPROJECT_VERSION, version, 'version = "{version}"'
    )
    if previous is not None:
        changes.append(f"pyproject.toml: {previous} -> {version}")

    previous = _replace(
        root / "core" / "version.py", MODULE_VERSION, version, '__version__ = "{version}"'
    )
    if previous is not None:
        changes.append(f"core/version.py: {previous} -> {version}")

    return changes


def main(argv: list[str]) -> int:
    tag = argv[1] if len(argv) > 1 else tag_from_environment()
    version = version_of(tag)
    if not version:
        print(f"version: not a release tag ({tag or 'no tag'}); leaving the tree alone")
        return 0

    changes = stamp(version)
    if not changes:
        print(f"version: already {version}")
        return 0
    for line in changes:
        print(f"version: {line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
