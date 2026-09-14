r"""Make the tree say what the release tag says, just before it is built.

The tag is the thing a user sees and the thing they will quote in a bug report, so the tag
wins. One file carries the version -- ``core/version.py`` -- and ``pyproject.toml`` declares
it dynamic and reads that module, so stamping one line stamps the metadata too.

Run from ``build/build.sh`` and ``build/build.ps1`` before PyInstaller collects anything.
Ordering matters twice over:

* the frozen binary embeds ``core/version.py``, so the stamp has to land before collection;
* the reusable workflow runs ``BUILD_COMMAND`` **before** ``CI_COMMAND``, so the version test
  then checks the stamped tree rather than the committed one -- which is what makes the
  check meaningful on a release build instead of merely self-consistent.

Where the version comes from, in order:

1. an argument, for a hand-run build;
2. ``HOSTBRIDGE_VERSION``, which is what the workflow's ``BUILD_COMMAND`` sets from
   ``REF_NAME_NORM`` -- the resolved ref with the ``v`` already stripped (``1.2.3``, or
   ``main-1.2.3`` for a branch-prefixed tag);
3. ``GITHUB_REF_NAME`` / ``GITHUB_REF``, the raw tag, for a workflow that passes nothing.

Nothing is committed. The working tree of a CI runner is disposable and the repository keeps
whatever version it had; only the artifact is stamped.

Doing nothing is the normal outcome. A branch push, a pull request and a developer's local
build all have no release tag, and this exits quietly having touched no file -- leaving the
``0.0.0`` that says exactly that.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: A bare version: ``2``, ``0.1``, ``1.2.3``. The workflow accepts no more than this, so
#: neither does anything here -- a pre-release suffix that silently became part of a version
#: string would be worse than a build that refuses to stamp.
VERSION_PATTERN = re.compile(r"[0-9]+(?:\.[0-9]+)*")

#: The workflow refuses any tag that is not ``v#``, ``v#.#`` or ``v#.#.#``, and with
#: MULTIPLE_PACKAGES it prefixes a branch: ``main/v1.2.3``. Only the last segment is a
#: version, so the prefix is dropped before anything else looks at it.
TAG_PATTERN = re.compile(rf"^v(?P<version>{VERSION_PATTERN.pattern})$")

MODULE_VERSION = re.compile(r'(?m)^__version__\s*=\s*"([^"]+)"')


def tag_from_environment() -> str:
    """The release tag this build was cut from, or "" when there is not one."""
    name = os.environ.get("GITHUB_REF_NAME", "").strip()
    if not name:
        ref = os.environ.get("GITHUB_REF", "").strip()
        name = ref[len("refs/tags/") :] if ref.startswith("refs/tags/") else ""
    return name


def version_of(tag: str) -> str:
    """The bare version in a release tag, or "" if it is not a release tag at all.

    A branch name reaching here is ordinary rather than an error: every push builds, and
    only some pushes are releases. The leading ``v`` is required precisely so that a branch
    called ``2`` cannot be mistaken for a version.
    """
    if not tag:
        return ""
    match = TAG_PATTERN.match(tag.strip().split("/")[-1])
    return match.group("version") if match else ""


def version_of_normalised(value: str) -> str:
    """The bare version in a ``REF_NAME_NORM``, or "" when that ref was not a release.

    The workflow normalises a tag by dropping the ``v`` (``v1.2.3`` -> ``1.2.3``) and, for a
    branch-prefixed one, by joining with a hyphen (``main/v1.2.3`` -> ``main-1.2.3``). So
    the version is whatever follows the last hyphen, or the whole string when there is none.

    Without the ``v`` there is nothing left to tell a version from a branch named ``2``,
    which is why the caller is expected to have checked ``REF_TYPE`` first. This is kept
    separate from :func:`version_of` for exactly that reason: one of them is safe against an
    arbitrary ref and the other is not, and merging them would lose the distinction.
    """
    candidate = value.strip().rsplit("-", 1)[-1]
    return candidate if VERSION_PATTERN.fullmatch(candidate) else ""


def requested_version(argv: list[str]) -> str:
    """Resolve the version to stamp from the argument, the environment, or the tag."""
    given = argv[1].strip() if len(argv) > 1 else os.environ.get("HOSTBRIDGE_VERSION", "").strip()
    if given:
        return version_of(given) or version_of_normalised(given)
    return version_of(tag_from_environment())


def stamp(version: str, root: Path = ROOT) -> list[str]:
    """Write ``version`` into the module that carries it. Returns a line per change."""
    path = root / "core" / "version.py"
    text = path.read_text(encoding="utf-8")
    match = MODULE_VERSION.search(text)
    if match is None:
        raise SystemExit(f"{path.name}: no version line was found to stamp")
    if match.group(1) == version:
        return []

    updated = text[: match.start()] + f'__version__ = "{version}"' + text[match.end() :]
    path.write_text(updated, encoding="utf-8", newline="\n")
    return [f"core/version.py: {match.group(1)} -> {version}"]


def main(argv: list[str]) -> int:
    version = requested_version(argv)
    if not version:
        source = (argv[1] if len(argv) > 1 else tag_from_environment()) or "no tag"
        print(f"version: not a release ({source}); leaving the tree alone")
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
