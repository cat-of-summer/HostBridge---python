"""Starting the desktop interface.

Kept apart from the window so that importing the window in a test does not construct a
``QApplication`` as a side effect -- there may only ever be one per process, and a second
one aborts.
"""

from __future__ import annotations

import sys

from core import log
from core.paths import resource_dir


def load_theme() -> str:
    """The stylesheet, or an empty string when it is missing.

    Read through :func:`core.paths.resource_dir` because in a collected build it lives
    beside the binary rather than next to this file.
    """
    try:
        return (resource_dir("assets") / "theme.qss").read_text(encoding="utf-8")
    except OSError:
        return ""


def run() -> int:
    """Open the window and hand control to Qt."""
    from PySide6.QtWidgets import QApplication

    from client.api import Bridge
    from ui.main_window import MainWindow

    application = QApplication.instance() or QApplication(sys.argv[:1])
    application.setApplicationName("HostBridge")
    application.setOrganizationName("HostBridge")

    stylesheet = load_theme()
    if stylesheet:
        application.setStyleSheet(stylesheet)

    # Quitting is the tray menu's job; closing the window only hides it.
    application.setQuitOnLastWindowClosed(False)

    window = MainWindow(Bridge.connect())
    window.show()

    log.write("gui: window opened")
    return application.exec()
