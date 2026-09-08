"""The tray icon.

Closing the window hides it here instead of quitting, because the point of the application
is to keep running. That is also why it says so once, with a balloon: a window that vanishes
with no explanation reads as a crash, and the user goes looking for it in Task Manager.

On Linux the tray is skipped entirely when the desktop has none. GNOME without an extension
has no tray at all, and constructing one there produces an application that is running,
invisible, and unquittable.
"""

from __future__ import annotations

from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from ui.i18n import t

RUNNING_COLOUR = "#5fa85f"
STOPPED_COLOUR = "#8b8f96"
ERROR_COLOUR = "#c9564a"

ICON_SIZE = 32


def make_icon(colour: str) -> QIcon:
    """A filled circle in the given colour.

    Drawn rather than shipped as a file: three states need three icons, they have to look
    the same on both platforms, and a painted circle cannot be the wrong size for the
    tray's DPI.
    """
    pixmap = QPixmap(ICON_SIZE, ICON_SIZE)
    pixmap.fill(QColor(0, 0, 0, 0))

    painter = QPainter(pixmap)
    try:
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setBrush(QColor(colour))
        painter.setPen(QColor(colour).darker(140))
        painter.drawEllipse(3, 3, ICON_SIZE - 6, ICON_SIZE - 6)
    finally:
        # Explicitly ended: a QPainter still active when its QPixmap is destroyed warns on
        # every platform and crashes on some.
        painter.end()
    return QIcon(pixmap)


class Tray(QSystemTrayIcon):
    def __init__(self, window) -> None:
        super().__init__(window)
        self.window = window
        self._told_about_hiding = False

        menu = QMenu()
        self.show_action = QAction(t("tray.show"), menu)
        self.show_action.triggered.connect(self._show_window)
        self.state_action = QAction(t("tray.stopped"), menu)
        self.state_action.setEnabled(False)
        self.quit_action = QAction(t("tray.quit"), menu)
        self.quit_action.triggered.connect(self._quit)

        menu.addAction(self.show_action)
        menu.addSeparator()
        menu.addAction(self.state_action)
        menu.addSeparator()
        menu.addAction(self.quit_action)
        self.setContextMenu(menu)

        self.activated.connect(self._on_activated)
        self.set_state(online=False)

    @staticmethod
    def available() -> bool:
        return QSystemTrayIcon.isSystemTrayAvailable()

    def set_state(self, *, online: bool, error: str = "") -> None:
        if error:
            self.setIcon(make_icon(ERROR_COLOUR))
            self.state_action.setText(error)
            self.setToolTip(f"{t('app.name')} - {error}")
            return
        colour = RUNNING_COLOUR if online else STOPPED_COLOUR
        text = t("tray.running") if online else t("tray.stopped")
        self.setIcon(make_icon(colour))
        self.state_action.setText(text)
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
