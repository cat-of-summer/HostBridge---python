"""The application mark, in one place.

The window, the taskbar and the tray all show the same icon, so they all ask here for it.
It is loaded once and kept: a QIcon is cheap to copy and expensive to read off disk, and the
tray asks for it again on every state change.

A drawn fallback exists because :func:`core.paths.resource_dir` can legitimately come up
empty -- a source checkout without the generated assets, or a build that collected the
binaries but not the data directory -- and an application that refuses to open because its
logo is missing would be absurd.
"""

from __future__ import annotations

from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap

from core.paths import resource_dir

ICON_FILE = "hostbridge.png"

#: The mark's own grey, matching the ring drawn by ``build/make_icon.py``.
FALLBACK_COLOUR = "#434343"
FALLBACK_SIZE = 64

_cached: QIcon | None = None


def _drawn() -> QIcon:
    pixmap = QPixmap(FALLBACK_SIZE, FALLBACK_SIZE)
    pixmap.fill(QColor(0, 0, 0, 0))

    painter = QPainter(pixmap)
    try:
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setBrush(QColor("#ffffff"))
        painter.setPen(QColor(FALLBACK_COLOUR))
        painter.drawEllipse(2, 2, FALLBACK_SIZE - 4, FALLBACK_SIZE - 4)
    finally:
        # Explicitly ended: a QPainter still active when its QPixmap is destroyed warns on
        # every platform and crashes on some.
        painter.end()
    return QIcon(pixmap)


def app_icon() -> QIcon:
    """The circular logo, or a plain disc when the asset did not ship."""
    global _cached
    if _cached is not None:
        return _cached

    path = resource_dir("assets") / ICON_FILE
    icon = QIcon(str(path)) if path.is_file() else QIcon()
    _cached = icon if not icon.isNull() else _drawn()
    return _cached
