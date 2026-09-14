"""The tray icon.

Closing the window hides it here instead of quitting, because the point of the application
is to keep running. That is also why it says so once, with a balloon: a window that vanishes
with no explanation reads as a crash, and the user goes looking for it in Task Manager.

The menu is two entries. The resolver's state used to be a third, disabled one; it is in the
tooltip now, because the resolver starts with the application and stops with it -- a line
that says "running" every time anyone reads it is not information.

On Linux the tray is skipped entirely when the desktop has none. GNOME without an extension
has no tray at all, and constructing one there produces an application that is running,
invisible, and unquittable.
"""

from __future__ import annotations

from PySide6.QtGui import QAction
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from ui.branding import app_icon
from ui.i18n import t


class Tray(QSystemTrayIcon):
    def __init__(self, window) -> None:
        super().__init__(window)
        self.window = window
        self._told_about_hiding = False

        self.setIcon(app_icon())

        menu = QMenu()
        self.show_action = QAction(t("tray.show"), menu)
        self.show_action.triggered.connect(self._show_window)
        self.quit_action = QAction(t("tray.quit"), menu)
        self.quit_action.triggered.connect(self._quit)

        menu.addAction(self.show_action)
        menu.addSeparator()
        menu.addAction(self.quit_action)
        self.setContextMenu(menu)

        self.activated.connect(self._on_activated)
        self.set_state(online=False)

    @staticmethod
    def available() -> bool:
        return QSystemTrayIcon.isSystemTrayAvailable()

    def set_state(self, *, online: bool, error: str = "") -> None:
        """Report the resolver in the tooltip. The icon itself never changes."""
        text = error or (t("tray.running") if online else t("tray.stopped"))
        self.setToolTip(f"{t('app.name')} - {text}")

    def note_hidden(self) -> None:
        """Explain the disappearing window, once."""
        if self._told_about_hiding:
            return
        self._told_about_hiding = True
        self.showMessage(t("app.name"), t("tray.minimised"), self.icon(), 4000)

    def _on_activated(self, reason) -> None:
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            self._show_window()

    def _show_window(self) -> None:
        self.window.showNormal()
        self.window.raise_()
        self.window.activateWindow()

    def _quit(self) -> None:
        from PySide6.QtWidgets import QApplication

        self.window.allow_quit = True
        self.window.close()
        QApplication.instance().quit()
