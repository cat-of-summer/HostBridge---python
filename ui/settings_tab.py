"""Settings, and the diagnosis for the failure nobody can see from inside the application.

Two kinds of setting live here and they are stored in different places for a reason.
Language and theme are this user's preference and go straight to ``config.json`` in their
home. Everything the resolver uses goes through the bridge, because ``settings.json`` sits
in the machine directory whose permissions belong to whoever runs the daemon -- SYSTEM, when
it is a service -- and a window running as the user cannot write there.

The DoH panel is the other way round: it is read **here**, in the process that runs as the
user, because browser profiles live in that user's home. The daemon is elevated and would be
looking at the wrong account's Chrome.

Nothing here edits a browser. A tool that reached into Chrome's ``Local State`` to switch
off Secure DNS would deserve everything it got; we report what we found and name the switch.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
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


def summarise(findings: list[dict]) -> str:
    """One line describing what the browsers are set to.

    A pure function of the scan so it can be checked without a screen, which matters: this
    is the text a confused user reads when their domain will not open, and it has to be
    right about a distinction that is easy to get backwards -- Chrome's "automatic" is
    **safe**, because it upgrades only for resolvers it recognises as DoH providers, and
    127.0.0.1 is not one.
    """
    if not findings:
        return t("settings.doh_none")
    broken = [f for f in findings if f.get("secure")]
    if not broken:
        return t("settings.doh_clear", count=len(findings))
    names = ", ".join(
        f"{f['browser']} {f['profile']}".strip() + f" ({f['mode']})" for f in broken
    )
    return t("settings.doh_problem", browsers=names)


class SettingsTab(QWidget):
    changed = Signal()

    def __init__(self, bridge: Bridge, parent=None) -> None:
        super().__init__(parent)
        self.bridge = bridge
        self._loading = False

        # ---- resolver ----------------------------------------------------------------
        self.listen_address = QLineEdit()
        self.listen_port = QSpinBox()
        self.listen_port.setRange(1, 65535)
        self.upstreams = QLineEdit()
        self.upstreams.setPlaceholderText(t("settings.upstreams_hint"))
        self.bypass = QCheckBox(t("settings.bypass"))
        self.bypass.setToolTip(t("settings.bypass_hint"))

        resolver = QGroupBox(t("settings.group_resolver"))
        resolver_form = QFormLayout(resolver)
        resolver_form.addRow(t("settings.listen_address"), self.listen_address)
        resolver_form.addRow(t("settings.listen_port"), self.listen_port)
        resolver_form.addRow(t("settings.upstreams"), self.upstreams)
        resolver_form.addRow("", self.bypass)

        # ---- sources -----------------------------------------------------------------
        self.traefik_enabled = QCheckBox(t("settings.traefik_enabled"))
        self.traefik_api = QLineEdit()
        self.traefik_poll = QSpinBox()
        self.traefik_poll.setRange(MIN_POLL_SECONDS, MAX_POLL_SECONDS)
        self.traefik_poll.setSuffix(t("settings.seconds_suffix"))
        self.docker_enabled = QCheckBox(t("settings.docker_enabled"))
        self.docker_host = QLineEdit()
        self.docker_host.setPlaceholderText(t("settings.docker_host_hint"))

        sources = QGroupBox(t("settings.group_sources"))
        sources_form = QFormLayout(sources)
        sources_form.addRow("", self.traefik_enabled)
        sources_form.addRow(t("settings.traefik_api"), self.traefik_api)
        sources_form.addRow(t("settings.traefik_poll"), self.traefik_poll)
        sources_form.addRow("", self.docker_enabled)
        sources_form.addRow(t("settings.docker_host"), self.docker_host)

        # ---- interface ---------------------------------------------------------------
        self.language = QComboBox()
        self.language.addItem(t("settings.language_auto"), "")
        for code in SUPPORTED_LANGUAGES:
            self.language.addItem(code, code)
        self.language.currentIndexChanged.connect(self._save_language)

        interface = QGroupBox(t("settings.group_interface"))
        interface_form = QFormLayout(interface)
        interface_form.addRow(t("settings.language"), self.language)

        # ---- secure DNS --------------------------------------------------------------
        self.doh_label = QLabel("")
        self.doh_label.setWordWrap(True)
        self.doh_button = QPushButton(t("settings.doh_rescan"))
        self.doh_button.clicked.connect(self.rescan_doh)

        doh = QGroupBox(t("settings.group_doh"))
        doh_layout = QVBoxLayout(doh)
        doh_layout.addWidget(QLabel(t("settings.doh_explain")))
        doh_layout.addWidget(self.doh_label)
        doh_row = QHBoxLayout()
        doh_row.addWidget(self.doh_button)
        doh_row.addStretch(1)
        doh_layout.addLayout(doh_row)

        # ---- buttons -----------------------------------------------------------------
        self.save_button = QPushButton(t("settings.save"))
        self.save_button.clicked.connect(self.save)
        self.reload_button = QPushButton(t("settings.reload"))
        self.reload_button.clicked.connect(self.refresh)

        self.message = QLabel("")
        self.message.setWordWrap(True)

        buttons = QHBoxLayout()
        buttons.addWidget(self.save_button)
        buttons.addWidget(self.reload_button)
        buttons.addStretch(1)

        layout = QVBoxLayout(self)
        layout.addWidget(resolver)
        layout.addWidget(sources)
        layout.addWidget(interface)
        layout.addWidget(doh)
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
        for widget in (
            self.listen_address,
            self.listen_port,
            self.upstreams,
            self.bypass,
            self.traefik_enabled,
            self.traefik_api,
            self.traefik_poll,
            self.docker_enabled,
            self.docker_host,
            self.save_button,
        ):
            widget.setEnabled(enabled)

    def refresh(self) -> None:
        try:
            payload = self.bridge.settings()
        except BridgeOffline:
            self._set_enabled(False)
            self._report(t("settings.needs_daemon"))
            return
        except BridgeError as exc:
            self._set_enabled(False)
            self._report(str(exc), error=True)
            return

        values = payload.get("settings") or {}
        self._loading = True
        try:
            self.listen_address.setText(str(values.get("listen_address", "")))
            self.listen_port.setValue(int(values.get("listen_port", 53)))
            self.upstreams.setText(", ".join(values.get("upstreams") or []))
            self.bypass.setChecked(bool(values.get("bypass_dns_filter", True)))
            self.traefik_enabled.setChecked(bool(values.get("traefik_enabled", True)))
            self.traefik_api.setText(str(values.get("traefik_api", "")))
            self.traefik_poll.setValue(int(values.get("traefik_poll_seconds", 10)))
            self.docker_enabled.setChecked(bool(values.get("docker_enabled", True)))
            self.docker_host.setText(str(values.get("docker_host", "")))
        finally:
            self._loading = False
        self._set_enabled(True)

    def collect(self) -> dict:
        """The form as the API wants it. Separated so it can be checked without a screen."""
        return {
            "listen_address": self.listen_address.text().strip(),
            "listen_port": self.listen_port.value(),
            "upstreams": [
                part.strip() for part in self.upstreams.text().split(",") if part.strip()
            ],
            "bypass_dns_filter": self.bypass.isChecked(),
            "traefik_enabled": self.traefik_enabled.isChecked(),
            "traefik_api": self.traefik_api.text().strip(),
            "traefik_poll_seconds": self.traefik_poll.value(),
            "docker_enabled": self.docker_enabled.isChecked(),
            "docker_host": self.docker_host.text().strip(),
        }

    def save(self) -> None:
        try:
            outcome = self.bridge.update_settings(**self.collect())
        except BridgeOffline:
            self._report(t("settings.needs_daemon"))
            return
        except BridgeError as exc:
            self._report(str(exc), error=True)
            return

        restart = outcome.get("restart_required") or []
        if restart:
            self._report(t("settings.saved_restart", fields=", ".join(restart)))
        elif outcome.get("changed"):
            self._report(t("settings.saved"))
        else:
            self._report(t("settings.unchanged"))
        self.changed.emit()

    # ---- secure DNS ---------------------------------------------------------------------

    def rescan_doh(self) -> None:
        """Read the browser profiles of the user running this window.

        Done here and not in the daemon on purpose: the daemon is elevated, and on Windows
        that means it would be reading a different account's Chrome, or none at all.
        """
        from system import doh

        findings = [
            {
                "browser": f.browser,
                "profile": f.profile,
                "mode": f.mode,
                "secure": f.secure,
            }
            for f in doh.scan()
        ]
        self.doh_label.setText(summarise(findings))
        self.doh_label.setStyleSheet(
            "color: #e0a05c;" if any(f["secure"] for f in findings) else ""
        )
        self.doh_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
