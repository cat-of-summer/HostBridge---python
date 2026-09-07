from __future__ import annotations

import json

import pytest

from core.model import SOURCE_MANUAL, SOURCE_TRAEFIK, Domain
from core.paths import domains_file
from core.store import DomainStore, StoreReadOnly
from core.version import SCHEMA_VERSION


@pytest.fixture
def store() -> DomainStore:
    return DomainStore()


def test_a_missing_file_loads_as_empty(store):
    snapshot = store.load()
    assert snapshot.domains == []
    assert not snapshot.read_only


def test_add_and_reload(store):
    store.add(Domain(name="shop.test"))
    names = [d.name for d in store.load().domains]
    assert names == ["shop.test"]


def test_the_written_file_carries_the_schema_and_version(store):
    store.add(Domain(name="shop.test"))
    payload = json.loads(domains_file().read_text(encoding="utf-8"))
    assert payload["schema"] == SCHEMA_VERSION
    assert payload["updated_at"]
    assert payload["app_version"]


def test_a_duplicate_name_is_refused(store):
    store.add(Domain(name="shop.test"))
    with pytest.raises(ValueError, match="shop.test"):
        store.add(Domain(name="SHOP.TEST"))


def test_toggle_flips_and_can_be_set_explicitly(store):
    created = store.add(Domain(name="shop.test"))
    assert store.toggle(created.id).enabled is False
    assert store.toggle(created.id).enabled is True
    assert store.toggle(created.id, enabled=False).enabled is False


def test_update_changes_fields_and_stamps_the_time(store):
    created = store.add(Domain(name="shop.test"))
    updated = store.update(created.id, address="10.0.0.5")
    assert updated.address == "10.0.0.5"
    assert updated.updated_at >= created.updated_at


def test_update_of_a_missing_record_returns_none(store):
    assert store.update("nope", address="10.0.0.5") is None


def test_delete(store):
    created = store.add(Domain(name="shop.test"))
    assert store.delete(created.id) is True
    assert store.delete(created.id) is False
    assert store.load().domains == []


def test_a_corrupt_entry_costs_only_that_entry(store):
    domains_file().parent.mkdir(parents=True, exist_ok=True)
    domains_file().write_text(
        json.dumps(
            {
                "schema": SCHEMA_VERSION,
                "domains": [{"name": "good.test"}, {"nope": True}, "garbage", None],
            }
        ),
        encoding="utf-8",
    )
    assert [d.name for d in store.load().domains] == ["good.test"]


def test_a_newer_schema_loads_read_only_and_refuses_to_be_overwritten(store):
    domains_file().parent.mkdir(parents=True, exist_ok=True)
    domains_file().write_text(
        json.dumps({"schema": SCHEMA_VERSION + 5, "domains": [{"name": "future.test"}]}),
        encoding="utf-8",
    )
    snapshot = store.load()
    assert snapshot.read_only
    assert [d.name for d in snapshot.domains] == ["future.test"]

    with pytest.raises(StoreReadOnly):
        store.add(Domain(name="new.test"))


def test_mutate_rereads_under_the_lock(store):
    """A caller holding a stale snapshot must not erase a concurrent write."""
    store.add(Domain(name="first.test"))
    stale = store.load()

    store.add(Domain(name="second.test"))

    # The stale snapshot still lists one domain, but mutate() ignores it and re-reads.
    assert len(stale.domains) == 1
    store.add(Domain(name="third.test"))
    assert {d.name for d in store.load().domains} == {"first.test", "second.test", "third.test"}


def test_backups_accumulate_and_are_capped(store):
    from core.store import BACKUP_KEEP

    for index in range(BACKUP_KEEP + 4):
        store.add(Domain(name=f"d{index}.test"))
    backups = sorted(domains_file().parent.glob(f"{domains_file().name}.*.bak"))
    assert 0 < len(backups) <= BACKUP_KEEP


# ---- the anti-clobber rule --------------------------------------------------------


def _traefik(name: str, owner: str) -> Domain:
    return Domain(name=name, source=SOURCE_TRAEFIK, owner=owner)


def test_reconcile_adds_and_then_removes_what_vanished(store):
    result = store.reconcile(
        SOURCE_TRAEFIK, [_traefik("etm39.ru", "r1"), _traefik("eabr.org", "r2")]
    )
    assert sorted(result.added) == ["eabr.org", "etm39.ru"]

    result = store.reconcile(SOURCE_TRAEFIK, [_traefik("etm39.ru", "r1")])
    assert result.removed == ["eabr.org"]
    assert [d.name for d in store.load().domains] == ["etm39.ru"]


def test_reconcile_never_deletes_a_manual_record(store):
    store.add(Domain(name="mine.test"))
    store.reconcile(SOURCE_TRAEFIK, [_traefik("etm39.ru", "r1")])
    store.reconcile(SOURCE_TRAEFIK, [])

    remaining = store.load().domains
    assert [d.name for d in remaining] == ["mine.test"]
    assert remaining[0].source == SOURCE_MANUAL


def test_a_manual_name_wins_a_collision_and_the_clash_is_reported(store):
    store.add(Domain(name="etm39.ru", address="10.0.0.9"))
    result = store.reconcile(SOURCE_TRAEFIK, [_traefik("etm39.ru", "r1")])

    assert result.conflicts == ["etm39.ru"]
    assert result.added == []
    domains = store.load().domains
    assert len(domains) == 1
    assert domains[0].source == SOURCE_MANUAL
    assert domains[0].address == "10.0.0.9"


def test_reconcile_updates_a_renamed_router_target(store):
    store.reconcile(SOURCE_TRAEFIK, [_traefik("old.ru", "r1")])
    result = store.reconcile(SOURCE_TRAEFIK, [_traefik("new.ru", "r1")])
    assert result.updated == ["new.ru"]
    assert [d.name for d in store.load().domains] == ["new.ru"]


def test_a_hand_edited_field_survives_the_next_sync(store):
    store.reconcile(SOURCE_TRAEFIK, [_traefik("etm39.ru", "r1")])
    discovered = store.load().domains[0]

    store.update(discovered.id, address="192.168.1.50")
    store.reconcile(SOURCE_TRAEFIK, [_traefik("etm39.ru", "r1")])

    kept = store.load().domains[0]
    assert kept.address == "192.168.1.50"
    assert "address" in kept.pinned_fields


def test_toggling_a_discovered_record_is_not_treated_as_a_hand_edit(store):
    """Enable/disable stays under sync control; only real edits pin a field."""
    store.reconcile(SOURCE_TRAEFIK, [_traefik("etm39.ru", "r1")])
    discovered = store.load().domains[0]

    store.toggle(discovered.id, enabled=False)
    assert store.load().domains[0].pinned_fields == []


def test_a_discovered_record_without_an_owner_is_refused(store):
    result = store.reconcile(SOURCE_TRAEFIK, [Domain(name="x.test", source=SOURCE_TRAEFIK)])
    assert result.skipped_no_owner == 1
    assert store.load().domains == []


def test_one_source_does_not_touch_another(store):
    store.reconcile(SOURCE_TRAEFIK, [_traefik("etm39.ru", "r1")])
    store.reconcile("docker", [Domain(name="app.test", source="docker", owner="c1")])
    store.reconcile("docker", [])

    assert [d.name for d in store.load().domains] == ["etm39.ru"]
