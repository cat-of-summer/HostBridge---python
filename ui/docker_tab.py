"""The Docker tab: what the engine is running, and what it declares.

Read-only, with no exceptions. Domains discovered from labels belong to the container that
declared them -- the compose file is where they are changed, and a copy taken here would
only be a second truth that drifts.

There is one button, and it does the whole of what "refresh" can mean: make the daemon
reconcile the domains it derives from those labels, then redraw the listing. They used to be
two, and the difference between them was a fact about our internals rather than about
anything the user was trying to do.

The listing is asked of the daemon rather than of Docker directly. The window runs as the
user, and on Windows the engine's named pipe is routinely readable only by ``docker-users``;
the daemon is elevated and already holds a connection.
"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from client.api import Bridge, BridgeError, BridgeOffline
from ui.containers_model import COLUMN_NAME, ContainerTableModel
from ui.i18n import t


class DockerTab(QWidget):
    changed = Signal()
    """Raised after a sync pass changed the domains, so the window refreshes its status."""

    def __init__(self, bridge: Bridge, parent=None) -> None:
        super().__init__(parent)
        self.bridge = bridge

        self.model = ContainerTableModel()

        self.view = QTableView()
        self.view.setModel(self.model)
        self.view.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.view.setSelectionMode(QAbstractItemView.SingleSelection)
        self.view.setAlternatingRowColors(True)
        self.view.setSortingEnabled(False)
        self.view.verticalHeader().setVisible(False)
        self.view.horizontalHeader().setStretchLastSection(True)
        self.view.horizontalHeader().setSectionResizeMode(COLUMN_NAME, QHeaderView.Interactive)
        self.view.setColumnWidth(COLUMN_NAME, 200)

        self.refresh_button = QPushButton(t("gui.docker_refresh"))
        self.refresh_button.clicked.connect(self.refresh)

        self.message = QLabel("")
        self.message.setWordWrap(True)

        toolbar = QHBoxLayout()
        toolbar.addWidget(self.refresh_button)
        toolbar.addStretch(1)

        layout = QVBoxLayout(self)
        layout.addLayout(toolbar)
        layout.addWidget(self.view, 1)
        layout.addWidget(self.message)

    # ---- data ---------------------------------------------------------------------

    def _report(self, text: str, *, error: bool = False) -> None:
        self.message.setText(text)
        self.message.setStyleSheet("color: #e06c60;" if error else "color: #7ea86a;")

    def reload(self) -> None:
        """Redraw the listing. An unreachable engine is stated, not treated as an error.

        A developer machine without Docker is ordinary, and the resolver has nothing to do
        with containers -- so the tab says so in one line and the rest of the window carries
        on working.
        """
        try:
            payload = self.bridge.containers()
        except BridgeOffline:
            self.model.replace([])
            self._report(t("gui.docker_no_daemon"))
            return
        except BridgeError as exc:
            self.model.replace([])
            self._report(str(exc), error=True)
            return

        rows = payload.get("containers") or []
        self.model.replace(rows)

        if not payload.get("reachable"):
            self._report(t("gui.docker_unreachable", error=payload.get("error") or ""))
            return
        self._report(t("gui.docker_counted", count=len(rows)))

    def refresh(self) -> None:
        """The button: reconcile the label-derived domains, then redraw.

        The event stream normally keeps the domains in step by itself; a button is what you
        reach for when you have just fixed a label and do not want to wonder whether it
        landed.
        """
        try:
            outcome = self.bridge.refresh_docker()
        except BridgeOffline:
            self._report(t("gui.docker_no_daemon"))
            return
        except BridgeError as exc:
            self._report(str(exc), error=True)
            return

        self.reload()
        if not outcome.get("reachable"):
            self._report(t("gui.docker_unreachable", error=""))
            return
        self._report(t("gui.docker_synced") if outcome.get("changed") else t("gui.docker_same"))
        self.changed.emit()
