"""The table model behind the Docker tab.

Like :mod:`ui.domains_model` it holds no widget and does no I/O, so it can be checked
headless. Rows are plain dictionaries as the control API returns them rather than a
dataclass: nothing here needs behaviour, and a container listing is exactly one hop from the
wire to the table.

Identity is the container id, so :meth:`ContainerTableModel.replace` can tell "the same
containers, one changed state" from "a different set of containers" -- during a ``compose
up`` the daemon fires an event per container, and resetting the model on each one would make
the table jump under the user's cursor.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PySide6.QtGui import QColor

from ui.i18n import t

COLUMN_NAME = 0
COLUMN_IMAGE = 1
COLUMN_STATE = 2
COLUMN_DOMAINS = 3
COLUMN_COUNT = 4

#: A container that declares no hostname is listed anyway -- part of the tab's job is to show
#: what *could* be given one -- but dimmed, so the labelled ones stand out.
UNLABELLED_COLOUR = QColor("#6b6e75")


class ContainerTableModel(QAbstractTableModel):
    def __init__(self, rows: Sequence[dict[str, Any]] = (), parent=None) -> None:
        super().__init__(parent)
        self._rows: list[dict[str, Any]] = [dict(row) for row in rows]

    # ---- data ---------------------------------------------------------------------

    def rows(self) -> tuple[dict[str, Any], ...]:
        return tuple(self._rows)

    def row_at(self, row: int) -> dict[str, Any] | None:
        if 0 <= row < len(self._rows):
            return self._rows[row]
        return None

    def names_at(self, row: int) -> list[str]:
        found = self.row_at(row)
        if found is None:
            return []
        return [str(name) for name in found.get("names") or ()]

    def replace(self, rows: Sequence[dict[str, Any]]) -> None:
        fresh = [dict(row) for row in rows]
        same_rows = [r.get("id") for r in fresh] == [r.get("id") for r in self._rows]

        if not same_rows:
            self.beginResetModel()
            self._rows = fresh
            self.endResetModel()
            return

        changed = [
            index
            for index, (before, after) in enumerate(zip(self._rows, fresh, strict=True))
            if before != after
        ]
        self._rows = fresh
        if not changed:
            return
        self.dataChanged.emit(
            self.index(min(changed), 0),
            self.index(max(changed), COLUMN_COUNT - 1),
        )

    # ---- QAbstractTableModel ------------------------------------------------------

    def rowCount(self, parent=QModelIndex()) -> int:  # noqa: N802, B008 - Qt's signature
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent=QModelIndex()) -> int:  # noqa: N802, B008 - Qt's signature
        return 0 if parent.isValid() else COLUMN_COUNT

    def headerData(self, section: int, orientation, role=Qt.DisplayRole):  # noqa: N802
        if orientation != Qt.Horizontal or role != Qt.DisplayRole:
            return None
        return {
            COLUMN_NAME: t("gui.column_container"),
            COLUMN_IMAGE: t("gui.column_image"),
            COLUMN_STATE: t("gui.column_state"),
            COLUMN_DOMAINS: t("gui.column_domains"),
        }.get(section)

    def flags(self, index):
        if not index.isValid():
            return Qt.NoItemFlags
        return Qt.ItemIsEnabled | Qt.ItemIsSelectable

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        row = self.row_at(index.row())
        if row is None:
            return None
        column = index.column()
        names = [str(name) for name in row.get("names") or ()]

        if role == Qt.DisplayRole:
            if column == COLUMN_NAME:
                return str(row.get("name", ""))
            if column == COLUMN_IMAGE:
                return str(row.get("image", ""))
            if column == COLUMN_STATE:
                return str(row.get("state", ""))
            if column == COLUMN_DOMAINS:
                return ", ".join(names)
            return None

        if role == Qt.ForegroundRole and not names:
            return UNLABELLED_COLOUR

        if role == Qt.ToolTipRole:
            return str(row.get("status") or row.get("id") or "")

        if role == Qt.UserRole:
            return str(row.get("id", ""))

        return None
