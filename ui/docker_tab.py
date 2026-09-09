"""The Docker tab: what the engine is running, and what it declares.

Read-only on purpose, with one exception. Domains discovered from labels are owned by the
sync pass -- editing one here would be undone by the next scan -- so the tab offers the
action that *does* survive: copying a container's hostname into a manual record, which the
store then protects from the sync pass by ownership. Everything else is a view.

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

from client.api import Bridge, BridgeConflict, BridgeError, BridgeOffline
from ui.containers_model import COLUMN_NAME, ContainerTableModel
from ui.i18n import t


class DockerTab(QWidget):
    changed = Signal()
    """Raised after a domain is created here, so the window refreshes its status line."""

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
        self.rescan_button = QPushButton(t("gui.docker_rescan"))
        self.pin_button = QPushButton(t("gui.docker_pin"))
        self.refresh_button.clicked.connect(self.refresh)
        self.rescan_button.clicked.connect(self.rescan)
        self.pin_button.clicked.connect(self.pin_selected)

        self.message = QLabel("")
        self.message.setWordWrap(True)

        toolbar = QHBoxLayout()
        toolbar.addWidget(self.refresh_button)
        toolbar.addWidget(self.rescan_button)
        toolbar.addWidget(self.pin_button)
        toolbar.addStretch(1)

        layout = QVBoxLayout(self)
        layout.addLayout(toolbar)
        layout.addWidget(self.view, 1)
        layout.addWidget(self.message)

        self.view.selectionModel().selectionChanged.connect(self._update_buttons)
        self._update_buttons()

    # ---- data ---------------------------------------------------------------------

    def _selected_row(self) -> int:
        rows = {index.row() for index in self.view.selectionModel().selectedIndexes()}
        return next(iter(sorted(rows)), -1)

    def _update_buttons(self, *_args) -> None:
        self.pin_button.setEnabled(bool(self.model.names_at(self._selected_row())))

    def _report(self, text: str, *, error: bool = False) -> None:
        self.message.setText(text)
        self.message.setStyleSheet("color: #e06c60;" if error else "color: #7ea86a;")

    def refresh(self) -> None:
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
            self._update_buttons()
            return
        except BridgeError as exc:
            self.model.replace([])
            self._report(str(exc), error=True)
            self._update_buttons()
            return

        rows = payload.get("containers") or []
        self.model.replace(rows)
        self._update_buttons()

        if not payload.get("reachable"):
            self._report(t("gui.docker_unreachable", error=payload.get("error") or ""))
            return
        self._report(t("gui.docker_counted", count=len(rows)))

    def rescan(self) -> None:
        """Ask the daemon for an immediate sync pass, then redraw.

        Distinct from Refresh: that redraws the container list, this makes the daemon
        reconcile the domains it derives from those labels. The event stream normally does
        it, and a button is what you reach for when you have just fixed a label.
        """
        try:
            outcome = self.bridge.refresh_docker()
        except BridgeOffline:
            self._report(t("gui.docker_no_daemon"))
            return
        except BridgeError as exc:
            self._report(str(exc), error=True)
            return

        self.refresh()
        if not outcome.get("reachable"):
            self._report(t("gui.docker_unreachable", error=""))
            return
        self._report(t("gui.docker_synced") if outcome.get("changed") else t("gui.docker_same"))
        self.changed.emit()

    # ---- actions ------------------------------------------------------------------

    def pin_selected(self) -> None:
        """Copy the selected container's hostnames into manual records.

        Manual records win over discovered ones and the sync pass never touches them, so
        this is how a user takes a container's domain and keeps it -- with a different
        address, say -- after the container is gone.
        """
        row = self._selected_row()
        names = self.model.names_at(row)
        if not names:
            return
        container = self.model.row_at(row) or {}
        address = str(container.get("address") or "127.0.0.1")
        note = str(container.get("name") or "")

        added, failures = 0, []
        for name in names:
            try:
                self.bridge.add(name, address, note)
            except BridgeConflict:
                # Already present as a manual record, which is the state this button wants.
                continue
            except BridgeError as exc:
                failures.append(f"{name}: {exc}")
            else:
                added += 1

        if failures:
            self._report("\n".join(failures), error=True)
        else:
            self._report(t("gui.docker_pinned", count=added))
        self.changed.emit()
