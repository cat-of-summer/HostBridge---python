"""The window: a tab bar, a status line, and a strip of notices above them.

Three tabs now -- domains, Docker, settings. The log tab is gone: it showed a developer's
view of the daemon's own chatter, while the things a user needs to be told arrive through
:mod:`ui.notice` instead, one strip at a time and carrying the button that acts on them.

The resolver is started here rather than waited for. Nothing in this window works without
one, so asking the user to press a button first was asking them to confirm the only thing
they could have wanted. It is asked for once, as soon as the window has painted, and it
stops on its own when this process goes away -- the daemon is told our pid and watches it.
"""

from __future__ import annotations

import time

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QMainWindow,
    QStatusBar,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from client.api import Bridge, BridgeError
from core.version import __version__
from ui.bridge import BridgeWorker
from ui.docker_tab import DockerTab
from ui.domains_tab import DomainsTab
from ui.i18n import t
from ui.notice import DOH, ERROR, OFFLINE, RESOLVER, NoticeBar
from ui.settings_tab import SettingsTab
from ui.tray import Tray

#: How long to wait for a just-started resolver to answer. The first start also captures
#: the upstream resolvers and registers the namespaces, so it is not instant.
START_TIMEOUT_SECONDS = 20.0


class MainWindow(QMainWindow):
    def __init__(self, bridge: Bridge) -> None:
        super().__init__()
        self.bridge = bridge

        self.setWindowTitle(f"{t('app.name')} {__version__}")
        self.resize(940, 560)

        self.notices = NoticeBar()

        self.domains_tab = DomainsTab(bridge)
        self.domains_tab.changed.connect(self.refresh_status)

        self.docker_tab = DockerTab(bridge)
        self.docker_tab.changed.connect(self.domains_tab.refresh)
        self.docker_tab.changed.connect(self.refresh_status)

        self.settings_tab = SettingsTab(bridge)
        self.settings_tab.changed.connect(self.refresh_status)

        self.tabs = QTabWidget()
        self.tabs.addTab(self.domains_tab, t("gui.tab_domains"))
        self.tabs.addTab(self.docker_tab, t("gui.tab_docker"))
        self.tabs.addTab(self.settings_tab, t("gui.tab_settings"))
        # Listing containers costs a round trip to the engine, so it happens when the tab is
        # first looked at rather than on every window open.
        self.tabs.currentChanged.connect(self._on_tab_changed)

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.notices)
        layout.addWidget(self.tabs, 1)
        self.setCentralWidget(central)

        self.setStatusBar(QStatusBar())

        self.allow_quit = False
        self._start_timer: QTimer | None = None
        self._start_deadline = 0.0
        self._starting = False
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

        # Once the event loop is running, so the window is on screen before the consent
        # prompt goes up in front of it. A UAC dialog over a half-painted window looks as
        # though it came from nowhere.
        QTimer.singleShot(0, self._on_ready)

    # ---- start-up ---------------------------------------------------------------------

    def _on_ready(self) -> None:
        self._autostart_resolver()
        self._check_browsers()

    def _autostart_resolver(self) -> None:
        """Ask for a resolver unless there already is one."""
        if self.bridge.online:
            return
        self._start_resolver()

    def _check_browsers(self) -> None:
        """Say so once if a browser resolves names without asking the system.

        Read here, in the process that runs as the user, and not in the daemon: the daemon
        is elevated, and on Windows that means it would be reading a different account's
        Chrome, or none at all. Nothing is changed -- a tool that reached into Chrome's
        settings to switch Secure DNS off would deserve everything it got.
        """
        from system import doh

        message = doh.warning(doh.scan())
        if message:
            self.notices.show_notice(DOH, message)

    # ---- tabs ---------------------------------------------------------------------

    def _on_tab_changed(self, index: int) -> None:
        if self.tabs.widget(index) is self.docker_tab:
            self.docker_tab.reload()
        elif self.tabs.widget(index) is self.settings_tab:
            self.settings_tab.refresh()

    # ---- events from the daemon ---------------------------------------------------

    def _on_event(self, payload: dict) -> None:
        if payload.get("kind") == "domains":
            self.domains_tab.refresh()
            self.refresh_status()

    def _on_connection(self, online: bool, reason: str) -> None:
        if self.tray is not None:
            self.tray.set_state(online=online, error=reason)
        if reason:
            self.notices.show_notice(RESOLVER, reason, kind=ERROR)
        elif online:
            self.notices.clear(RESOLVER)
        # The bridge the worker holds is the live one; the tab must not keep using a
        # connection that has since gone away.
        if self.worker.bridge is not None:
            self.bridge = self.worker.bridge
            self.domains_tab.bridge = self.worker.bridge
            self.docker_tab.bridge = self.worker.bridge
            self.settings_tab.bridge = self.worker.bridge
        self.domains_tab.refresh()
        if self.tabs.currentWidget() is self.docker_tab:
            self.docker_tab.reload()
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
        self._show_offline(not snapshot.online)
        self.statusBar().showMessage(text)

    def _show_offline(self, offline: bool) -> None:
        """The one notice that carries an action: no resolver, and here is how to get one."""
        if not offline:
            self.notices.clear(OFFLINE)
            return
        # Not while an attempt is in flight: the notice would appear and vanish again a
        # moment later, which reads as a flicker rather than as news.
        if self._starting or OFFLINE in self.notices.active():
            return
        self.notices.show_notice(
            OFFLINE,
            t("gui.banner_offline"),
            action=(t("gui.banner_start"), self._start_resolver),
        )

    # ---- starting the resolver ----------------------------------------------------

    def _start_resolver(self) -> None:
        """Ask for the resolver to be started, then wait for it to answer.

        The consent prompt blocks this thread while it is up, which is fine -- it is
        system-modal anyway, and a window that carried on painting behind it would only look
        broken. The *waiting* afterwards is on a timer rather than a loop, because that part
        can take fifteen seconds and freezing there would look like a hang.
        """
        from client.bootstrap import start_resolver

        if self._starting:
            return
        self._starting = True
        self.notices.clear(OFFLINE)
        self.notices.clear(RESOLVER)
        self.statusBar().showMessage(t("bootstrap.started"))

        outcome = start_resolver()
        if not outcome.started:
            self._finish_start_attempt()
            # A refusal is the user's answer, not a failure: the offline notice and its
            # button are the whole of what needs saying. Anything else gets reported.
            if not outcome.cancelled:
                self.notices.show_notice(RESOLVER, outcome.message, kind=ERROR)
            self.refresh_status()
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
            self.docker_tab.bridge = bridge
            self.settings_tab.bridge = bridge
            self._finish_start_attempt()
            self.domains_tab.refresh()
            self.refresh_status()
            return

        if time.monotonic() >= self._start_deadline:
            self._finish_start_attempt()
            self.notices.show_notice(RESOLVER, t("bootstrap.timeout"), kind=ERROR)
            self.refresh_status()

    def _finish_start_attempt(self) -> None:
        if self._start_timer is not None:
            self._start_timer.stop()
            self._start_timer = None
        self._starting = False

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
