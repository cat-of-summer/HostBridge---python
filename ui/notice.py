"""The strip along the top of the window that says what went wrong.

It replaces the log tab. A rolling view of every line the daemon emits was a developer's
tool sitting in a user's window: the interesting lines -- the resolver would not start, the
browser will not ask the system -- scrolled past among heartbeats, and everything it showed
is in ``hostbridge.log`` anyway.

So instead there is one strip, it appears only when there is something to say, and when the
thing it says can be acted on it carries the button that acts on it.

Notices are keyed, and a key holds at most one notice: a resolver that drops twice in a row
must not stack two identical strips, and the condition clearing must take the strip that
reported it down rather than leave the user to dismiss stale news.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QToolButton,
    QVBoxLayout,
)

from ui.i18n import t

#: Notice keys. Named rather than spelled out at each call site, so that putting one up and
#: taking it down again cannot disagree by a typo.
OFFLINE = "offline"
RESOLVER = "resolver"
DOH = "doh"

WARNING = "warning"
ERROR = "error"


class Notice(QFrame):
    """One line: text, an optional action, and a way to make it go away."""

    def __init__(
        self,
        text: str,
        *,
        kind: str,
        action: tuple[str, Callable[[], None]] | None = None,
    ) -> None:
        super().__init__()
        # Read by assets/theme.qss as `QFrame[notice="error"]`; the colours belong in the
        # stylesheet with the rest of the palette, not in a setStyleSheet call here.
        self.setProperty("notice", kind)

        self.label = QLabel(text)
        self.label.setWordWrap(True)
        self.label.setTextInteractionFlags(Qt.TextSelectableByMouse)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 6, 6, 6)
        layout.addWidget(self.label, 1)

        self.button: QPushButton | None = None
        if action is not None:
            caption, handler = action
            self.button = QPushButton(caption)
            self.button.clicked.connect(handler)
            layout.addWidget(self.button)

        self.close_button = QToolButton()
        self.close_button.setText("\u2715")
        self.close_button.setToolTip(t("notice.dismiss"))
        self.close_button.setAutoRaise(True)
        layout.addWidget(self.close_button)


class NoticeBar(QFrame):
    """Holds the notices. Empty means hidden, so it costs no space when all is well."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._notices: dict[str, Notice] = {}

        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(1)
        self.setVisible(False)

    def show_notice(
        self,
        key: str,
        text: str,
        *,
        kind: str = WARNING,
        action: tuple[str, Callable[[], None]] | None = None,
    ) -> Notice:
        """Put up a notice, replacing whatever this key was saying before."""
        self.clear(key)
        notice = Notice(text, kind=kind, action=action)
        notice.close_button.clicked.connect(lambda: self.clear(key))
        self._notices[key] = notice
        self._layout.addWidget(notice)
        self.setVisible(True)
        return notice

    def clear(self, key: str) -> None:
        notice = self._notices.pop(key, None)
        if notice is not None:
            self._layout.removeWidget(notice)
            notice.setParent(None)
            notice.deleteLater()
        self.setVisible(bool(self._notices))

    def notice(self, key: str) -> Notice | None:
        return self._notices.get(key)

    def active(self) -> set[str]:
        """Which notices are up. Checked rather than mirrored in the window's own state."""
        return set(self._notices)
