"""Starting the desktop interface.

Kept apart from the window so that importing the window in a test does not construct a
``QApplication`` as a side effect -- there may only ever be one per process, and a second
one aborts.
"""

from __future__ import annotations

import sys

from core import log
from core.paths import resource_dir

#: What Windows groups the taskbar button under. Without it the shell falls back to the
#: executable it recognises -- the Python interpreter, in a source checkout -- and the
#: application shows up under that icon instead of its own.
APP_ID = "HostBridge.HostBridge"


def load_theme() -> str:
    """The stylesheet, or an empty string when it is missing.

    Read through :func:`core.paths.resource_dir` because in a collected build it lives
    beside the binary rather than next to this file.
    """
    try:
        return (resource_dir("assets") / "theme.qss").read_text(encoding="utf-8")
    except OSError:
        return ""


def claim_taskbar_identity() -> None:
    """Tell the Windows shell this process is its own application. A no-op elsewhere."""
    if not sys.platform.startswith("win"):
        return
    import ctypes

    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    except (AttributeError, OSError) as exc:  # pragma: no cover - shell version dependent
        log.warn(f"gui: could not set the taskbar identity: {exc}")


def run() -> int:
    """Open the window and hand control to Qt."""
    from PySide6.QtWidgets import QApplication

    from client.api import Bridge
    from ui.branding import app_icon
    from ui.main_window import MainWindow

    claim_taskbar_identity()

    application = QApplication.instance() or QApplication(sys.argv[:1])
    application.setApplicationName("HostBridge")
    application.setOrganizationName("HostBridge")
    # Set on the application rather than the window: the tray, the dialogs and the taskbar
    # button all inherit it, so there is one place to change it.
    application.setWindowIcon(app_icon())

    stylesheet = load_theme()
    if stylesheet:
        application.setStyleSheet(stylesheet)

    # Quitting is the tray menu's job; closing the window only hides it.
    application.setQuitOnLastWindowClosed(False)

    window = MainWindow(Bridge.connect())
    window.show()

    log.write("gui: window opened")
    return application.exec()
