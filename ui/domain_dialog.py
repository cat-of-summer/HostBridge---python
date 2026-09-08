"""The add and edit form.

Validation lives in :mod:`core.match`, not here. The console screen and the daemon apply the
same rules, and three copies of "is this name usable" would drift within a week. This dialog
only shows what that function returned.

The distinction the form has to get right is error versus warning. An error disables OK; a
warning does not. Blocking on a warning would make ``*.dobroedelo.ru`` -- shadowing a real
production domain, which is the primary use case here -- impossible to enter.
"""

from __future__ import annotations

import ipaddress

from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
)

from core.match import apex_of, is_wildcard, validate
from core.model import Domain
from ui.i18n import t

ERROR_STYLE = "color: #e06c60;"
WARNING_STYLE = "color: #d6a44c;"


class DomainDialog(QDialog):
    def __init__(self, parent=None, domain: Domain | None = None, taken: set[str] = frozenset()):
        super().__init__(parent)
        self.domain = domain
        self._taken = {name.lower() for name in taken}
        if domain is not None:
            self._taken.discard(domain.name.lower())

        self.setWindowTitle(t("gui.dialog_edit") if domain else t("gui.dialog_add"))
        self.setMinimumWidth(460)

        self.name_edit = QLineEdit(domain.name if domain else "")
        self.address_edit = QLineEdit(domain.address if domain else "127.0.0.1")
        self.note_edit = QLineEdit(domain.note if domain else "")
        self.apex_check = QCheckBox(t("gui.dialog_add_apex"))
        self.apex_check.setChecked(True)
        self.apex_check.setVisible(False)

        self.message = QLabel("")
        self.message.setWordWrap(True)
        self.message.setVisible(False)

        form = QFormLayout()
        form.addRow(t("domain.name"), self.name_edit)
        form.addRow(t("domain.address"), self.address_edit)
        form.addRow(t("domain.note"), self.note_edit)
        form.addRow("", self.apex_check)

        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.message)
        layout.addWidget(self.buttons)

        self.name_edit.textChanged.connect(self._revalidate)
        self.address_edit.textChanged.connect(self._revalidate)
        self._revalidate()

    # ---- validation ---------------------------------------------------------------

    def _revalidate(self) -> None:
        raw = self.name_edit.text().strip()
        checked = validate(raw)

        problems: list[str] = []
        blocking = False

        if raw and not checked.ok:
            problems.append(t(checked.error.key, **checked.error.params))
            blocking = True
        elif checked.name and checked.name.lower() in self._taken:
            problems.append(t("gui.dialog_duplicate", name=checked.name))
            blocking = True

        address = self.address_edit.text().strip()
        if address:
            try:
                ipaddress.ip_address(address)
            except ValueError:
                problems.append(t("gui.dialog_bad_address", address=address))
                blocking = True

        warnings = [t(issue.key, **issue.params) for issue in checked.warnings] if raw else []

        self.apex_check.setVisible(bool(raw) and checked.ok and is_wildcard(checked.name))

        if problems:
            self.message.setText("\n".join(problems))
            self.message.setStyleSheet(ERROR_STYLE)
            self.message.setVisible(True)
        elif warnings:
            self.message.setText("\n".join(warnings))
            self.message.setStyleSheet(WARNING_STYLE)
            self.message.setVisible(True)
        else:
            self.message.setVisible(False)

        self.buttons.button(QDialogButtonBox.Ok).setEnabled(bool(raw) and not blocking)

    # ---- result -------------------------------------------------------------------

    @property
    def blocked(self) -> bool:
        """Whether OK is currently refused. Exposed for the tests."""
        return not self.buttons.button(QDialogButtonBox.Ok).isEnabled()

    def result_values(self) -> dict[str, str]:
        checked = validate(self.name_edit.text().strip())
        return {
            "name": checked.name,
            "address": self.address_edit.text().strip() or "127.0.0.1",
            "note": self.note_edit.text().strip(),
        }

    def wants_apex(self) -> str:
        """The apex to create alongside a wildcard, or an empty string.

        A wildcard genuinely does not match its own apex, so offering to add it is the only
        way most people will end up with both -- and they are separate records on purpose,
        because switching the bare domain back to production while keeping the subdomains
        local is a thing people actually want.
        """
        # Derived from the name rather than from the checkbox's visibility. A child widget
        # of a dialog that has not been shown reports isVisible() as False regardless of
        # setVisible(), so reading it back here would answer correctly only by accident.
        checked = validate(self.name_edit.text().strip())
        if not checked.ok or not is_wildcard(checked.name):
            return ""
        if not self.apex_check.isChecked():
            return ""
        apex = apex_of(checked.name)
        return "" if apex.lower() in self._taken else apex
