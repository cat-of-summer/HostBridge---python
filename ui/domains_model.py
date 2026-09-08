"""The table model behind the domain list.

Holds no widget and performs no I/O, which is what lets the whole thing be tested headless
under a bare ``QCoreApplication``. Everything the view needs is here; everything the model
needs arrives through :meth:`DomainTableModel.replace`.

The interesting method is :meth:`replace`. It is called on every event from the daemon --
during a ``compose up`` that can be several times a second -- and a blanket
``beginResetModel`` each time would drop the selection and the scroll position, making the
table jump under the user's cursor. So it diffs first and only resets when the rows really
did change identity.
"""

from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt, Signal
from PySide6.QtGui import QColor

from core.model import SOURCE_MANUAL, Domain
from ui.i18n import t

COLUMN_ENABLED = 0
COLUMN_NAME = 1
COLUMN_ADDRESS = 2
COLUMN_SOURCE = 3
COLUMN_NOTE = 4
COLUMN_COUNT = 5

#: Dimmed, so a discovered row reads as "managed elsewhere" without a legend.
DISCOVERED_COLOUR = QColor("#8b8f96")
DISABLED_COLOUR = QColor("#6b6e75")


class DomainTableModel(QAbstractTableModel):
    toggle_requested = Signal(str, bool)
    """Emitted instead of writing anywhere: the tab owns the one path through the bridge."""

    def __init__(self, domains: Sequence[Domain] = (), parent=None) -> None:
        super().__init__(parent)
        self._domains: list[Domain] = list(domains)

    # ---- data ---------------------------------------------------------------------

    def domains(self) -> tuple[Domain, ...]:
        return tuple(self._domains)

    def domain_at(self, row: int) -> Domain | None:
        if 0 <= row < len(self._domains):
            return self._domains[row]
        return None

    def row_of(self, identifier: str) -> int:
        for index, domain in enumerate(self._domains):
            if domain.id == identifier:
                return index
        return -1

    def replace(self, domains: Sequence[Domain]) -> None:
        """Adopt a new list, preserving the selection when the rows are the same ones.

        A full reset is only unavoidable when the set or the order of rows changed. When
        the identities line up -- the common case, because a toggle or an address edit does
        not move anything -- the change is announced as ``dataChanged`` and the view keeps
        its selection and scroll position.
        """
        fresh = list(domains)
        same_rows = [d.id for d in fresh] == [d.id for d in self._domains]

        if not same_rows:
            self.beginResetModel()
            self._domains = fresh
            self.endResetModel()
            return

        changed = [
            row
            for row, (before, after) in enumerate(zip(self._domains, fresh, strict=True))
            if before != after
        ]
        self._domains = fresh
        if not changed:
            return
        self.dataChanged.emit(
            self.index(min(changed), 0),
            self.index(max(changed), COLUMN_COUNT - 1),
        )

    # ---- QAbstractTableModel ------------------------------------------------------

    def rowCount(self, parent=QModelIndex()) -> int:  # noqa: N802, B008 - Qt's signature
        return 0 if parent.isValid() else len(self._domains)

    def columnCount(self, parent=QModelIndex()) -> int:  # noqa: N802, B008 - Qt's signature
        return 0 if parent.isValid() else COLUMN_COUNT

    def headerData(self, section: int, orientation, role=Qt.DisplayRole):  # noqa: N802
        if orientation != Qt.Horizontal or role != Qt.DisplayRole:
            return None
        return {
            COLUMN_ENABLED: t("gui.column_enabled"),
            COLUMN_NAME: t("gui.column_name"),
            COLUMN_ADDRESS: t("gui.column_address"),
            COLUMN_SOURCE: t("gui.column_source"),
            COLUMN_NOTE: t("gui.column_note"),
        }.get(section)

    def flags(self, index):
        if not index.isValid():
            return Qt.NoItemFlags
        base = Qt.ItemIsEnabled | Qt.ItemIsSelectable
        if index.column() == COLUMN_ENABLED:
            # A checkbox rather than a dialog: switching a domain off is the action people
            # take most often and it should cost one click.
            return base | Qt.ItemIsUserCheckable
        return base

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        domain = self.domain_at(index.row())
        if domain is None:
            return None
        column = index.column()

        if role == Qt.CheckStateRole and column == COLUMN_ENABLED:
            return Qt.Checked if domain.enabled else Qt.Unchecked

        if role == Qt.DisplayRole:
            if column == COLUMN_NAME:
                return domain.name
            if column == COLUMN_ADDRESS:
                return domain.address
            if column == COLUMN_SOURCE:
                return t(f"console.source_{domain.source}")
            if column == COLUMN_NOTE:
                return domain.note
            return None

        if role == Qt.ForegroundRole:
            if not domain.enabled:
                return DISABLED_COLOUR
            if domain.source != SOURCE_MANUAL:
                return DISCOVERED_COLOUR
            return None

        if role == Qt.ToolTipRole:
            if domain.source != SOURCE_MANUAL and domain.note:
                return t("gui.tooltip_discovered", source=domain.source, owner=domain.note)
            return domain.name

        if role == Qt.UserRole:
            return domain.id

        return None

    def setData(self, index, value, role=Qt.EditRole) -> bool:  # noqa: N802
        """Only the checkbox is editable in place; everything else goes through the dialog.

        The model does not write anywhere. It emits :attr:`toggle_requested` and the tab
        decides -- which keeps every mutation on the one path through the bridge.
        """
        if not index.isValid() or role != Qt.CheckStateRole:
            return False
        if index.column() != COLUMN_ENABLED:
            return False
        domain = self.domain_at(index.row())
        if domain is None:
            return False

        wanted = Qt.CheckState(value) == Qt.Checked
        if wanted == domain.enabled:
            return False
        self.toggle_requested.emit(domain.id, wanted)
        return True
