"""The window: a tab bar, a status line, and a banner when the resolver is not running.

The placeholder tabs are deliberate. Docker and Settings arrive in later milestones, but the
tab indices are saved in the user's config, and a tab appearing later would silently shift
whatever they had selected. Showing them now, disabled and labelled with the milestone they
belong to, costs a label and avoids that.
"""

from __future__ import annotations

import time

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPlainTextEdit,
    QPushButton,
    QStatusBar,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from client.api import Bridge, BridgeError
from core.version import __version__
from ui.bridge import BridgeWorker
from ui.domains_tab import DomainsTab
from ui.i18n import t
from ui.tray import Tray

#: The log tab is a rolling view, not an archive; the file log keeps everything.
LOG_LINES = 2000

#: How long to wait for a just-started resolver to answer. The first start also captures
#: the upstream resolvers and registers the namespaces, so it is not instant.
START_TIMEOUT_SECONDS = 20.0


def _placeholder(text: str) -> QWidget:
    widget = QWidget()
    label = QLabel(text)
    label.setAlignment(Qt.AlignCenter)
    label.setStyleSheet("color: #8b8f96;")
    layout = QVBoxLayout(widget)
    layout.addWidget(label)
    return widget


class MainWindow(QMainWindow):
    def __init__(self, bridge: Bridge) -> None:
        super().__init__()
        self.bridge = bridge

        self.setWindowTitle(f"{t('app.name')} {__version__}")
        self.resize(940, 560)

        self.banner = QFrame()
        self.banner_label = QLabel(t("gui.banner_offline"))
        self.banner_label.setWordWrap(True)
        self.banner_button = QPushButton(t("gui.banner_start"))
        self.banner_button.clicked.connect(self._start_resolver)
        banner_layout = QHBoxLayout(self.banner)
        banner_layout.setContentsMargins(10, 6, 10, 6)
        banner_layout.addWidget(self.banner_label, 1)
        banner_layout.addWidget(self.banner_button)
        self.banner.setStyleSheet("background: #3a3222; border: 1px solid #5c4f2e;")
        self.banner.setVisible(False)

        self.domains_tab = DomainsTab(bridge)
        self.domains_tab.changed.connect(self.refresh_status)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(LOG_LINES)

        self.tabs = QTabWidget()
        self.tabs.addTab(self.domains_tab, t("gui.tab_domains"))
        self.tabs.addTab(self.log_view, t("gui.tab_log"))
        self.tabs.addTab(_placeholder(t("gui.tab_pending", milestone="M6")), t("gui.tab_docker"))
        self.tabs.addTab(
            _placeholder(t("gui.tab_pending", milestone="M9")), t("gui.tab_settings")
        )
        self.tabs.setTabEnabled(2, False)
        self.tabs.setTabEnabled(3, False)

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.banner)
        layout.addWidget(self.tabs, 1)
        self.setCentralWidget(central)

        self.setStatusBar(QStatusBar())

        self.allow_quit = False
        self._start_timer: QTimer | None = None
        self._start_deadline = 0.0
        self.tray: Tray | None = None
        if Tray.available():
            self.tray = Tray(self)
            self.tray.show()

        self.worker = BridgeWorker(self)
        self.worker.event.connect(self._on_event)
        self.worker.connection.connect(self._on_connection)
        self.worker.start()

        self.domains_tab.refresh()
        self.refresh_status()

    # ---- events from the daemon ---------------------------------------------------

    def _on_event(self, payload: dict) -> None:
        kind = payload.get("kind")
        if kind == "domains":
            self.domains_tab.refresh()
            self.refresh_status()
        message = payload.get("message")
        if kind == "log" and message:
            self.log_view.appendPlainText(str(message))

    def _on_connection(self, online: bool, reason: str) -> None:
        self.banner.setVisible(not online)
        if self.tray is not None:
            self.tray.set_state(online=online, error=reason)
        if reason:
            self.log_view.appendPlainText(reason)
        # The bridge the worker holds is the live one; the tab must not keep using a
        # connection that has since gone away.
        if self.worker.bridge is not None:
            self.bridge = self.worker.bridge
            self.domains_tab.bridge = self.worker.bridge
        self.domains_tab.refresh()
        self.refresh_status()

    # ---- status -------------------------------------------------------------------

    def refresh_status(self) -> None:
        try:
            snapshot = self.bridge.snapshot()
        except BridgeError as exc:
            self.statusBar().showMessage(str(exc))
            return

        status = snapshot.status or {}
        if snapshot.online:
            text = t(
                "gui.status_running",
                listen=", ".join(status.get("listen") or ()) or "-",
                claimed=len(status.get("claimed") or ()),
                total=len(snapshot.domains),
            )
        else:
            text = t("gui.status_stopped", total=len(snapshot.domains))
        self.banner.setVisible(not snapshot.online)
        self.statusBar().showMessage(text)

    def _start_resolver(self) -> None:
        """Ask for the resolver to be started, then wait for it to answer.

        The consent prompt blocks this thread while it is up, which is fine -- it is
        system-modal anyway, and a window that carried on painting behind it would only look
        broken. The *waiting* afterwards is on a timer rather than a loop, because that part
        can take fifteen seconds and freezing there would look like a hang.
        """
        from client.bootstrap import start_resolver

        self.banner_button.setEnabled(False)
        self.banner_button.setText(t("gui.banner_starting"))

        outcome = start_resolver()
        self.log_view.appendPlainText(outcome.message)
        if not outcome.started:
            self._finish_start_attempt()
            if not outcome.cancelled:
                self.tabs.setCurrentWidget(self.log_view)
            return

        self._start_deadline = time.monotonic() + START_TIMEOUT_SECONDS
        self._start_timer = QTimer(self)
        self._start_timer.setInterval(500)
        self._start_timer.timeout.connect(self._poll_for_resolver)
        self._start_timer.start()

    def _poll_for_resolver(self) -> None:
        bridge = Bridge.connect()
        if bridge.online:
            self.bridge = bridge
            self.domains_tab.bridge = bridge
            self._finish_start_attempt()
            self.domains_tab.refresh()
            self.refresh_status()
            return

        if time.monotonic() >= self._start_deadline:
            self.log_view.appendPlainText(t("bootstrap.timeout"))
            self.tabs.setCurrentWidget(self.log_view)
            self._finish_start_attempt()

    def _finish_start_attempt(self) -> None:
        if self._start_timer is not None:
            self._start_timer.stop()
            self._start_timer = None
        self.banner_button.setEnabled(True)
        self.banner_button.setText(t("gui.banner_start"))

    # ---- lifetime -----------------------------------------------------------------

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt's signature
        # Closing hides rather than quits: the application is meant to keep running. The
        # tray says so once, because a window that vanishes silently reads as a crash.
        if self.tray is not None and not self.allow_quit:
            event.ignore()
            self.hide()
            self.tray.note_hidden()
            return

        # A QThread still running when QApplication tears down is the classic PySide crash
        # on exit, so it is stopped and waited for here rather than left to chance.
        self.worker.stop()
        # Comfortably more than the stream's poll interval, so the thread has actually
        # noticed rather than being abandoned mid-read.
        self.worker.wait(4000)
        super().closeEvent(event)
