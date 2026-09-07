"""hostbridge entry point.

Mirrors the layout of the sibling ccas and dockerbundle projects: pick the language before
anything can print, then hand off to the role dispatcher. Imports stay inside the functions
so that a PyInstaller binary starts quickly and so that a missing optional dependency only
breaks the role that needs it -- the daemon must still start on a machine where the Qt
platform plugin cannot load.
"""

from __future__ import annotations

import sys


def _init_language() -> None:
    from ui import i18n

    language = ""
    try:
        from core.config import UserConfig

        language = UserConfig.load().language
    except (OSError, ValueError):
        language = ""

    i18n.set_language(language or None)


def main(argv: list[str] | None = None) -> int:
    _init_language()

    from app.cli import dispatch

    args = sys.argv[1:] if argv is None else list(argv)
    try:
        return dispatch(args)
    except KeyboardInterrupt:
        sys.stderr.write("\ninterrupted\n")
        return 130


if __name__ == "__main__":
    sys.exit(main())
