"""Settings: the three things there is a real decision to make about.

Two kinds of setting live here and they are stored in different places for a reason. The
language is this user's preference and goes straight to ``config.json`` in their home.
Everything the resolver uses goes through the bridge, because ``settings.json`` sits in the
machine directory whose permissions belong to whoever runs the daemon -- SYSTEM, when it is
a service -- and a window running as the user cannot write there.

What used to be here and is not any more: the listen address, the port, the upstream
servers, the Docker host, and two checkboxes that turned the import sources on and off.
Every one of them had exactly one right answer for the machine it was running on, and a form
that asks a question with one right answer is asking the user to do the program's job.
Traefik is enabled by having an address to poll; Docker labels are always read; the resolver
listens where it has to listen. The fields survive in :class:`core.config.DaemonSettings` for
the rare machine that needs them changed in the file.

The Secure DNS panel has gone too, to :meth:`ui.main_window.MainWindow._check_browsers`. It
never was a setting -- nothing here has ever edited a browser, and a tool that reached into
Chrome's ``Local State`` to switch off Secure DNS would deserve everything it got. It is a
diagnosis, so it is made once at start-up and reported only when the answer is bad.
"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from client.api import Bridge, BridgeError, BridgeOffline
from ui.i18n import SUPPORTED_LANGUAGES, t

#: Never below one: a zero would turn the poller into a busy loop, and the daemon clamps it
#: anyway -- rejecting it in the form is friendlier than having it silently corrected.
MIN_POLL_SECONDS = 1
MAX_POLL_SECONDS = 3600


class SettingsTab(QWidget):
    changed = Signal()

    def __init__(self, bridge: Bridge, parent=None) -> None:
        super().__init__(parent)
        self.bridge = bridge
        self._loading = False

        # ---- sources -----------------------------------------------------------------
        self.traefik_api = QLineEdit()
        self.traefik_api.setPlaceholderText(t("settings.traefik_api_hint"))
        self.traefik_poll = QSpinBox()
        self.traefik_poll.setRange(MIN_POLL_SECONDS, MAX_POLL_SECONDS)
        self.traefik_poll.setSuffix(t("settings.seconds_suffix"))

        sources = QGroupBox(t("settings.group_sources"))
        sources_form = QFormLayout(sources)
        sources_form.addRow(t("settings.traefik_api"), self.traefik_api)
        sources_form.addRow(t("settings.traefik_poll"), self.traefik_poll)

        # ---- interface ---------------------------------------------------------------
        self.language = QComboBox()
        self.language.addItem(t("settings.language_auto"), "")
        for code in SUPPORTED_LANGUAGES:
            self.language.addItem(code, code)
        self.language.currentIndexChanged.connect(self._save_language)

        interface = QGroupBox(t("settings.group_interface"))
        interface_form = QFormLayout(interface)
        interface_form.addRow(t("settings.language"), self.language)

        # ---- buttons -----------------------------------------------------------------
        self.save_button = QPushButton(t("settings.save"))
        self.save_button.clicked.connect(self.save)

        self.message = QLabel("")
        self.message.setWordWrap(True)

        buttons = QHBoxLayout()
        buttons.addWidget(self.save_button)
        buttons.addStretch(1)

        layout = QVBoxLayout(self)
        layout.addWidget(sources)
        layout.addWidget(interface)
        layout.addLayout(buttons)
        layout.addWidget(self.message)
        layout.addStretch(1)

        self._load_language()

    # ---- language ---------------------------------------------------------------------

    def _load_language(self) -> None:
        from core.config import UserConfig

        self._loading = True
        try:
            index = self.language.findData(UserConfig.load().language)
            self.language.setCurrentIndex(max(0, index))
        finally:
            self._loading = False

    def _save_language(self) -> None:
        """Written straight to the user's own config; the daemon has no opinion on it.

        Applied on the next start rather than live: retranslating a built window means
        rebuilding every widget, and a language change is a once-ever action.
        """
        if self._loading:
            return
        from core.config import UserConfig

        config = UserConfig.load()
        config.language = self.language.currentData() or ""
        config.save()
        self._report(t("settings.language_restart"))

    # ---- daemon settings --------------------------------------------------------------

    def _report(self, text: str, *, error: bool = False) -> None:
        self.message.setText(text)
        self.message.setStyleSheet("color: #e06c60;" if error else "color: #7ea86a;")

    def _set_enabled(self, enabled: bool) -> None:
        for widget in (self.traefik_api, self.traefik_poll, self.save_button):
            widget.setEnabled(enabled)

    def refresh(self) -> None:
        """Re-read the form from the daemon.

        Called by the window every time this tab is opened, which is why there is no
        "reload" button: pressing one would do what looking at the tab already did.
        """
        try:
            payload = self.bridge.settings()
        except BridgeOffline:
            # Silently: the notice strip above the tabs already says the resolver is not
            # running, and a greyed-out form says the rest.
            self._set_enabled(False)
            self._report("")
            return
        except BridgeError as exc:
            self._set_enabled(False)
            self._report(str(exc), error=True)
            return

        values = payload.get("settings") or {}
        self._loading = True
        try:
            self.traefik_api.setText(str(values.get("traefik_api", "")))
            self.traefik_poll.setValue(int(values.get("traefik_poll_seconds", 10)))
        finally:
            self._loading = False
        self._set_enabled(True)

    def collect(self) -> dict:
        """The form as the API wants it. Separated so it can be checked without a screen."""
        return {
            "traefik_api": self.traefik_api.text().strip(),
            "traefik_poll_seconds": self.traefik_poll.value(),
        }

    def save(self) -> None:
        try:
            outcome = self.bridge.update_settings(**self.collect())
        except BridgeOffline:
            self._set_enabled(False)
            self._report("")
            return
        except BridgeError as exc:
            self._report(str(exc), error=True)
            return

        self._report(t("settings.saved") if outcome.get("changed") else t("settings.unchanged"))
        self.changed.emit()
