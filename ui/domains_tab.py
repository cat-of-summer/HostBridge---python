"""The domain list: the tab that does the work.

Every mutation goes through :class:`client.api.Bridge`. The tab never touches the store and
never builds a request; if the daemon is not running the bridge writes the file directly and
says the resolver is down, and the banner above the tabs reports that.

Calls to the bridge are made on the GUI thread on purpose. They are millisecond-scale
requests to loopback, the client carries a five-second timeout, and a worker thread for each
would buy nothing but a class of ordering bug.
"""

from __future__ import annotations

from PySide6.QtCore import QSortFilterProxyModel, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from client.api import Bridge, BridgeConflict, BridgeError
from ui.domain_dialog import DomainDialog
from ui.domains_model import COLUMN_ENABLED, COLUMN_NAME, DomainTableModel
from ui.i18n import t


class DomainsTab(QWidget):
    changed = Signal()
    """Raised after any successful mutation, so the window can refresh its status line."""

    def __init__(self, bridge: Bridge, parent=None) -> None:
        super().__init__(parent)
        self.bridge = bridge

        self.model = DomainTableModel()
        self.proxy = QSortFilterProxyModel(self)
        self.proxy.setSourceModel(self.model)
        self.proxy.setFilterCaseSensitivity(Qt.CaseInsensitive)
        self.proxy.setFilterKeyColumn(COLUMN_NAME)

        self.view = QTableView()
        self.view.setModel(self.proxy)
        self.view.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.view.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.view.setAlternatingRowColors(True)
        self.view.setSortingEnabled(True)
        self.view.verticalHeader().setVisible(False)
        self.view.horizontalHeader().setStretchLastSection(True)
        self.view.horizontalHeader().setSectionResizeMode(COLUMN_ENABLED, QHeaderView.Fixed)
        self.view.setColumnWidth(COLUMN_ENABLED, 40)
        self.view.doubleClicked.connect(lambda _index: self.edit_selected())

        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText(t("gui.filter"))
        self.filter_edit.setClearButtonEnabled(True)
        self.filter_edit.textChanged.connect(self.proxy.setFilterFixedString)

        self.add_button = QPushButton(t("gui.add"))
        self.edit_button = QPushButton(t("gui.edit"))
        self.delete_button = QPushButton(t("gui.delete"))
        self.add_button.clicked.connect(self.add)
        self.edit_button.clicked.connect(self.edit_selected)
        self.delete_button.clicked.connect(self.delete_selected)

        self.message = QLabel("")
        self.message.setWordWrap(True)

        toolbar = QHBoxLayout()
        toolbar.addWidget(self.add_button)
        toolbar.addWidget(self.edit_button)
        toolbar.addWidget(self.delete_button)
        toolbar.addStretch(1)
        toolbar.addWidget(self.filter_edit, 2)

        layout = QVBoxLayout(self)
        layout.addLayout(toolbar)
        layout.addWidget(self.view, 1)
        layout.addWidget(self.message)

        self.model.toggle_requested.connect(self._toggle)
        self.view.selectionModel().selectionChanged.connect(self._update_buttons)
        self._update_buttons()

    # ---- data ---------------------------------------------------------------------

    def apply_snapshot(self, snapshot) -> None:
        self.model.replace(snapshot.domains)
        self._update_buttons()

    def refresh(self) -> None:
        try:
            self.apply_snapshot(self.bridge.snapshot())
        except BridgeError as exc:
            self._report(str(exc), error=True)

    def _selected_domains(self):
        rows = {index.row() for index in self.view.selectionModel().selectedIndexes()}
        found = []
        for row in sorted(rows):
            source = self.proxy.mapToSource(self.proxy.index(row, 0))
            domain = self.model.domain_at(source.row())
            if domain is not None:
                found.append(domain)
        return found

    def _update_buttons(self, *_args) -> None:
        selected = self._selected_domains()
        self.edit_button.setEnabled(len(selected) == 1)
        self.delete_button.setEnabled(bool(selected))

    def _report(self, text: str, *, error: bool = False) -> None:
        self.message.setText(text)
        self.message.setStyleSheet("color: #e06c60;" if error else "color: #7ea86a;")

    # ---- actions ------------------------------------------------------------------

    def _toggle(self, identifier: str, enabled: bool) -> None:
        try:
            self.bridge.toggle(identifier, enabled)
        except BridgeError as exc:
            self._report(str(exc), error=True)
        else:
            self._report(t("gui.saved"))
        self.refresh()
        self.changed.emit()

    def add(self) -> None:
        taken = {domain.name for domain in self.model.domains()}
        dialog = DomainDialog(self, taken=taken)
        if dialog.exec() != DomainDialog.Accepted:
            return

        values = dialog.result_values()
        apex = dialog.wants_apex()
        try:
            self.bridge.add(values["name"], values["address"], values["note"])
            if apex:
                self.bridge.add(apex, values["address"], values["note"])
        except BridgeConflict as exc:
            self._report(t("gui.dialog_duplicate", name=exc.name), error=True)
        except BridgeError as exc:
            self._report(str(exc), error=True)
        else:
            self._report(t("gui.added", name=values["name"]))
        self.refresh()
        self.changed.emit()

    def edit_selected(self) -> None:
        selected = self._selected_domains()
        if len(selected) != 1:
            return
        domain = selected[0]

        taken = {other.name for other in self.model.domains()}
        dialog = DomainDialog(self, domain=domain, taken=taken)
        if dialog.exec() != DomainDialog.Accepted:
            return

        values = dialog.result_values()
        try:
            self.bridge.update(
                domain.id,
                name=values["name"],
                address=values["address"],
                note=values["note"],
            )
        except BridgeError as exc:
            self._report(str(exc), error=True)
        else:
            self._report(t("gui.saved"))
        self.refresh()
        self.changed.emit()

    def delete_selected(self) -> None:
        selected = self._selected_domains()
        if not selected:
            return

        if len(selected) == 1:
            question = t("gui.confirm_delete_one", name=selected[0].name)
        else:
            question = t("gui.confirm_delete_many", count=len(selected))
        answer = QMessageBox.question(self, t("gui.confirm_title"), question)
        if answer != QMessageBox.Yes:
            return

        failures = []
        for domain in selected:
            try:
                self.bridge.delete(domain.id)
            except BridgeError as exc:
                failures.append(f"{domain.name}: {exc}")

        if failures:
            self._report("\n".join(failures), error=True)
        else:
            self._report(t("gui.deleted", count=len(selected)))
        self.refresh()
        self.changed.emit()
