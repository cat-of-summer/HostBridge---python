"""The parts of the desktop interface that can be checked without a screen.

The model holds no widget and the dialog's validation is a pure function of its fields, so
both run under the offscreen platform plugin. What cannot be tested here -- that the window
looks right, that the tray icon appears -- is checked on the desktop.
"""

from __future__ import annotations

import pytest

from core.model import SOURCE_MANUAL, SOURCE_TRAEFIK, Domain

# importorskip only skips on ModuleNotFoundError. A PySide6 that is installed but whose
# system Qt libraries are missing raises a plain ImportError naming the .so, which would
# fail collection instead of skipping -- so both are caught here.
try:
    from PySide6.QtCore import Qt
except ImportError as exc:  # pragma: no cover - depends on the machine, not the code
    pytest.skip(f"PySide6 is unusable here: {exc}", allow_module_level=True)

from ui.domains_model import (
    COLUMN_ADDRESS,
    COLUMN_ENABLED,
    COLUMN_NAME,
    COLUMN_NOTE,
    COLUMN_SOURCE,
    DomainTableModel,
)


def _domains():
    return [
        Domain(id="a", name="etm39.ru", address="127.0.0.1", source=SOURCE_TRAEFIK, note="r1"),
        Domain(id="b", name="shop.test", address="127.0.0.9", source=SOURCE_MANUAL),
    ]


# ---- the table model ---------------------------------------------------------------


def test_the_model_reports_its_shape(qt_app):
    model = DomainTableModel(_domains())
    assert model.rowCount() == 2
    assert model.columnCount() == 5


def test_each_column_shows_the_right_field(qt_app):
    model = DomainTableModel(_domains())

    def cell(row, column):
        return model.data(model.index(row, column), Qt.DisplayRole)

    assert cell(0, COLUMN_NAME) == "etm39.ru"
    assert cell(0, COLUMN_ADDRESS) == "127.0.0.1"
    assert cell(0, COLUMN_SOURCE) == "traefik"
    assert cell(0, COLUMN_NOTE) == "r1"


def test_the_enabled_column_is_a_checkbox(qt_app):
    model = DomainTableModel([Domain(id="a", name="a.test", enabled=False)])
    index = model.index(0, COLUMN_ENABLED)
    assert model.data(index, Qt.CheckStateRole) == Qt.Unchecked
    assert model.flags(index) & Qt.ItemIsUserCheckable


def test_only_the_checkbox_is_editable_in_place(qt_app):
    model = DomainTableModel(_domains())
    assert not model.flags(model.index(0, COLUMN_NAME)) & Qt.ItemIsUserCheckable


def test_toggling_the_checkbox_asks_rather_than_writes(qt_app):
    """The model owns no store; every mutation goes out through the tab and the bridge."""
    model = DomainTableModel(_domains())
    seen: list[tuple[str, bool]] = []
    model.toggle_requested.connect(lambda ident, wanted: seen.append((ident, wanted)))

    model.setData(model.index(0, COLUMN_ENABLED), Qt.Unchecked, Qt.CheckStateRole)

    assert seen == [("a", False)]
    assert model.domain_at(0).enabled is True, "the model must not change it itself"


def test_setting_the_checkbox_to_what_it_already_is_does_nothing(qt_app):
    model = DomainTableModel(_domains())
    seen = []
    model.toggle_requested.connect(lambda *args: seen.append(args))
    assert model.setData(model.index(0, COLUMN_ENABLED), Qt.Checked, Qt.CheckStateRole) is False
    assert seen == []


def test_a_disabled_row_is_dimmed(qt_app):
    model = DomainTableModel([Domain(id="a", name="a.test", enabled=False)])
    assert model.data(model.index(0, COLUMN_NAME), Qt.ForegroundRole) is not None


def test_row_of_finds_a_domain_by_id(qt_app):
    model = DomainTableModel(_domains())
    assert model.row_of("b") == 1
    assert model.row_of("nope") == -1


# ---- replace(), and why it does not always reset -----------------------------------


def test_replacing_with_the_same_rows_keeps_the_selection(qt_app):
    """A reset on every daemon event would make the table jump under the cursor."""
    model = DomainTableModel(_domains())
    resets = []
    changes = []
    model.modelReset.connect(lambda: resets.append(True))
    model.dataChanged.connect(lambda *args: changes.append(args))

    updated = _domains()
    updated[0] = updated[0].touched(address="10.0.0.1")
    model.replace(updated)

    assert resets == [], "same rows, so no reset"
    assert len(changes) == 1
    assert model.data(model.index(0, COLUMN_ADDRESS), Qt.DisplayRole) == "10.0.0.1"


def test_replacing_with_different_rows_resets(qt_app):
    model = DomainTableModel(_domains())
    resets = []
    model.modelReset.connect(lambda: resets.append(True))

    model.replace([Domain(id="c", name="new.test")])

    assert resets == [True]
    assert model.rowCount() == 1


def test_replacing_with_identical_data_announces_nothing(qt_app):
    """A ten-second Traefik poll that changed nothing must not repaint the table."""
    model = DomainTableModel(_domains())
    changes = []
    model.dataChanged.connect(lambda *args: changes.append(args))

    model.replace(_domains())

    assert changes == []


def test_reordering_counts_as_different_rows(qt_app):
    model = DomainTableModel(_domains())
    resets = []
    model.modelReset.connect(lambda: resets.append(True))

    model.replace(list(reversed(_domains())))
    assert resets == [True]


# ---- the dialog --------------------------------------------------------------------


@pytest.fixture
def dialog_factory(qt_app):
    from ui.domain_dialog import DomainDialog

    created = []

    def make(**kwargs):
        dialog = DomainDialog(**kwargs)
        created.append(dialog)
        return dialog

    yield make
    for dialog in created:
        dialog.deleteLater()


def test_an_empty_name_blocks_ok(dialog_factory):
    dialog = dialog_factory()
    assert dialog.blocked


def test_a_valid_name_allows_ok(dialog_factory):
    dialog = dialog_factory()
    dialog.name_edit.setText("shop.test")
    assert not dialog.blocked


def test_an_invalid_name_blocks_ok_and_says_why(dialog_factory):
    dialog = dialog_factory()
    dialog.name_edit.setText("localhost")
    assert dialog.blocked
    assert "suffix" in dialog.message.text()


def test_a_warning_shows_but_does_not_block(dialog_factory):
    """Shadowing a real production domain is the primary use case here, so a warning must
    never make a name impossible to enter."""
    dialog = dialog_factory()
    dialog.name_edit.setText("shop.local")
    assert not dialog.blocked
    assert "mDNS" in dialog.message.text()


def test_a_duplicate_name_blocks(dialog_factory):
    dialog = dialog_factory(taken={"etm39.ru"})
    dialog.name_edit.setText("etm39.ru")
    assert dialog.blocked


def test_editing_a_domain_does_not_collide_with_itself(dialog_factory):
    existing = Domain(id="a", name="etm39.ru")
    dialog = dialog_factory(domain=existing, taken={"etm39.ru", "other.test"})
    assert not dialog.blocked


def test_a_bad_address_blocks(dialog_factory):
    dialog = dialog_factory()
    dialog.name_edit.setText("shop.test")
    dialog.address_edit.setText("not-an-ip")
    assert dialog.blocked
    assert "not an IP" in dialog.message.text()


def test_the_apex_offer_appears_only_for_a_wildcard(dialog_factory):
    dialog = dialog_factory()
    dialog.name_edit.setText("shop.test")
    assert dialog.wants_apex() == ""

    dialog.name_edit.setText("*.shop.test")
    assert dialog.wants_apex() == "shop.test"


def test_the_apex_is_not_offered_when_it_already_exists(dialog_factory):
    dialog = dialog_factory(taken={"shop.test"})
    dialog.name_edit.setText("*.shop.test")
    assert dialog.wants_apex() == ""


def test_the_dialog_returns_normalised_values(dialog_factory):
    dialog = dialog_factory()
    dialog.name_edit.setText("  SHOP.TEST.  ")
    dialog.address_edit.setText("10.0.0.5")
    assert dialog.result_values() == {"name": "shop.test", "address": "10.0.0.5", "note": ""}


# ---- the container table ------------------------------------------------------------


def _containers():
    return [
        {
            "id": "aaa",
            "name": "shop",
            "image": "nginx",
            "state": "running",
            "status": "Up 3 minutes",
            "names": ["shop.test", "www.shop.test"],
            "address": "127.0.0.1",
        },
        {
            "id": "bbb",
            "name": "idle",
            "image": "redis",
            "state": "running",
            "status": "Up 1 hour",
            "names": [],
            "address": "",
        },
    ]


def test_the_container_model_reports_its_shape(qt_app):
    from ui.containers_model import ContainerTableModel

    model = ContainerTableModel(_containers())
    assert model.rowCount() == 2
    assert model.columnCount() == 4


def test_the_declared_hostnames_are_shown_joined(qt_app):
    from ui.containers_model import COLUMN_DOMAINS, COLUMN_NAME, ContainerTableModel

    model = ContainerTableModel(_containers())
    assert model.data(model.index(0, COLUMN_NAME), Qt.DisplayRole) == "shop"
    assert (
        model.data(model.index(0, COLUMN_DOMAINS), Qt.DisplayRole) == "shop.test, www.shop.test"
    )
    assert model.data(model.index(1, COLUMN_DOMAINS), Qt.DisplayRole) == ""


def test_an_unlabelled_container_is_listed_but_dimmed(qt_app):
    """Part of the tab's job is showing what *could* be given a domain, so it is not hidden."""
    from ui.containers_model import COLUMN_NAME, UNLABELLED_COLOUR, ContainerTableModel

    model = ContainerTableModel(_containers())
    assert model.data(model.index(1, COLUMN_NAME), Qt.ForegroundRole) == UNLABELLED_COLOUR
    assert model.data(model.index(0, COLUMN_NAME), Qt.ForegroundRole) is None


def test_the_container_model_is_read_only(qt_app):
    """Discovered domains are owned by the sync pass; editing one here would be undone."""
    from ui.containers_model import COLUMN_DOMAINS, ContainerTableModel

    model = ContainerTableModel(_containers())
    flags = model.flags(model.index(0, COLUMN_DOMAINS))
    assert not flags & Qt.ItemIsEditable
    assert not flags & Qt.ItemIsUserCheckable


def test_the_same_containers_do_not_reset_the_container_model(qt_app):
    """A compose up fires an event per container; a reset each time would drop the selection."""
    from ui.containers_model import ContainerTableModel

    model = ContainerTableModel(_containers())
    resets = []
    model.modelReset.connect(lambda: resets.append(1))

    changed = _containers()
    changed[0]["state"] = "exited"
    model.replace(changed)
    assert resets == []
    assert model.rows()[0]["state"] == "exited"

    model.replace(_containers()[:1])
    assert resets == [1]


def test_names_at_answers_for_a_row_that_is_not_there(qt_app):
    from ui.containers_model import ContainerTableModel

    model = ContainerTableModel(_containers())
    assert model.names_at(-1) == []
    assert model.names_at(9) == []
    assert model.names_at(0) == ["shop.test", "www.shop.test"]
