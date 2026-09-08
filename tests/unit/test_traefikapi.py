r"""The Traefik rule parser, which replaces the regular expression in hosts.bat.

The rules below are real shapes, including the ones from the machine this was written for.
The two-predicate cases are the point: ``Host\((.*)\)`` is greedy and swallows whatever
follows, and rules with a second predicate are ordinary rather than exotic.
"""

from __future__ import annotations

import pytest

from core.model import SOURCE_TRAEFIK
from core.store import DomainStore
from discover.traefikapi import parse_rule, regexp_predicates, routers_to_domains

# ---- the parser -------------------------------------------------------------------


def test_a_single_host():
    assert parse_rule("Host(`etm39.ru`)") == ["etm39.ru"]


def test_several_hosts_in_one_predicate():
    assert parse_rule("Host(`a.test`, `b.test`)") == ["a.test", "b.test"]


def test_a_second_predicate_is_not_swallowed():
    """The bug a greedy regular expression would introduce, as a test."""
    names = parse_rule("Host(`a.test`) && PathPrefix(`/api`)")
    assert names == ["a.test"]
    assert not any("/" in name for name in names), "a path must never become a hostname"


def test_a_predicate_before_the_host_is_ignored():
    assert parse_rule("PathPrefix(`/api`) && Host(`a.test`)") == ["a.test"]


def test_alternation_collects_both_sides():
    assert parse_rule("Host(`a.test`) || Host(`b.test`)") == ["a.test", "b.test"]


def test_a_realistic_compound_rule():
    rule = "Host(`admin.dobroedelo.ru`, `api.dobroedelo.ru`) && PathPrefix(`/v1`) && Method(`GET`)"
    assert parse_rule(rule) == ["admin.dobroedelo.ru", "api.dobroedelo.ru"]


def test_host_sni_is_recognised_for_tcp_routers():
    assert parse_rule("HostSNI(`secure.test`)") == ["secure.test"]


def test_the_v1_host_header_spelling_still_works():
    assert parse_rule("HostHeader(`legacy.test`)") == ["legacy.test"]


def test_quotes_other_than_backticks_are_accepted():
    assert parse_rule('Host("a.test")') == ["a.test"]
    assert parse_rule("Host('a.test')") == ["a.test"]


def test_a_parenthesis_inside_a_value_does_not_end_the_scan():
    assert parse_rule("Host(`a.test`) && Query(`x=(1,2)`) && Host(`b.test`)") == [
        "a.test",
        "b.test",
    ]


def test_duplicates_collapse():
    assert parse_rule("Host(`a.test`) || Host(`a.test`)") == ["a.test"]


def test_whitespace_around_the_predicate_is_tolerated():
    assert parse_rule("Host  (  `a.test`  )") == ["a.test"]


def test_a_regexp_predicate_is_skipped_rather_than_parsed_into_nonsense():
    assert parse_rule("HostRegexp(`^.+\\.example\\.com$`)") == []
    assert regexp_predicates("HostRegexp(`^.+\\.example\\.com$`)") == ["^.+\\.example\\.com$"]


def test_a_regexp_alongside_a_literal_keeps_the_literal():
    rule = "Host(`a.test`) || HostRegexp(`^x.+\\.test$`)"
    assert parse_rule(rule) == ["a.test"]


def test_an_unbalanced_rule_yields_what_it_can():
    """A truncated rule still names a host worth registering."""
    assert parse_rule("Host(`a.test`") == ["a.test"]


@pytest.mark.parametrize(
    "rule", ["", "PathPrefix(`/api`)", "Method(`GET`)", "ClientIP(`10.0.0.0/8`)"]
)
def test_rules_without_a_host_yield_nothing(rule):
    assert parse_rule(rule) == []


def test_a_hostname_is_not_confused_with_a_similarly_named_predicate():
    """``Host`` must not match inside ``HostRegexp``; the word boundary and ordering do it."""
    assert parse_rule("HostRegexp(`^a$`) && Host(`b.test`)") == ["b.test"]


# ---- routers to records -----------------------------------------------------------


def _router(name: str, rule: str, status: str = "enabled") -> dict:
    return {"name": name, "rule": rule, "status": status, "provider": "docker"}


def test_routers_become_records_owned_by_the_router():
    result = routers_to_domains([_router("web@docker", "Host(`etm39.ru`)")])
    assert len(result.domains) == 1
    domain = result.domains[0]
    assert domain.name == "etm39.ru"
    assert domain.source == SOURCE_TRAEFIK
    assert domain.owner == "host:etm39.ru"
    assert domain.note == "web@docker"
    assert domain.address == "127.0.0.1"


def test_each_hostname_gets_a_distinct_owner():
    """Otherwise the second name looks like a rename of the first on the next sync."""
    result = routers_to_domains([_router("web@docker", "Host(`a.test`, `b.test`)")])
    owners = {domain.owner for domain in result.domains}
    assert owners == {"host:a.test", "host:b.test"}


def test_a_disabled_router_is_skipped():
    result = routers_to_domains([_router("web@docker", "Host(`a.test`)", status="disabled")])
    assert result.domains == []


def test_a_router_without_a_name_is_skipped():
    """Without an owner a record could never be cleaned up when the router disappears."""
    result = routers_to_domains([{"rule": "Host(`a.test`)", "status": "enabled"}])
    assert result.domains == []


def test_an_unusable_hostname_is_reported_not_imported():
    result = routers_to_domains([_router("web@docker", "Host(`localhost`)")])
    assert result.domains == []
    assert result.skipped_invalid == ["localhost"]


def test_the_same_name_from_two_routers_is_imported_once():
    result = routers_to_domains(
        [_router("a@docker", "Host(`same.test`)"), _router("b@docker", "Host(`same.test`)")]
    )
    assert len(result.domains) == 1


def test_garbage_entries_are_ignored():
    result = routers_to_domains(["nonsense", None, 42, {"rule": 5, "name": "x"}])
    assert result.domains == []


def test_the_users_live_router_table_imports_cleanly():
    """The five routers actually running on the target machine."""
    routers = [
        _router("dobroedelo-admin@docker", "Host(`admin.dobroedelo.ru`)"),
        _router("dobroedelo-api@docker", "Host(`api.dobroedelo.ru`)"),
        _router("php-apache-nginxeabr@docker", "Host(`eabr.org`)"),
        _router("php-nginxetm39ru@docker", "Host(`etm39.ru`)"),
        _router("php-nginxhevelsolar@docker", "Host(`www.hevelsolar.com`)"),
    ]
    result = routers_to_domains(routers)
    assert sorted(domain.name for domain in result.domains) == [
        "admin.dobroedelo.ru",
        "api.dobroedelo.ru",
        "eabr.org",
        "etm39.ru",
        "www.hevelsolar.com",
    ]


# ---- reconciliation with the store ------------------------------------------------


def test_an_import_round_trip_through_the_store():
    store = DomainStore()
    routers = [_router("a@docker", "Host(`etm39.ru`)"), _router("b@docker", "Host(`eabr.org`)")]

    added = store.reconcile(SOURCE_TRAEFIK, routers_to_domains(routers).domains)
    assert sorted(added.added) == ["eabr.org", "etm39.ru"]

    # One container stops: its router disappears and so should its domain, while the other
    # is left exactly as it was.
    gone = store.reconcile(SOURCE_TRAEFIK, routers_to_domains(routers[:1]).domains)
    assert gone.removed == ["eabr.org"]
    assert [d.name for d in store.load().domains] == ["etm39.ru"]


def test_importing_twice_changes_nothing():
    """A ten-second poll must not rewrite the store on every tick."""
    store = DomainStore()
    routers = [_router("a@docker", "Host(`etm39.ru`)")]

    store.reconcile(SOURCE_TRAEFIK, routers_to_domains(routers).domains)
    again = store.reconcile(SOURCE_TRAEFIK, routers_to_domains(routers).domains)

    assert not again.changed


def test_a_name_served_by_two_routers_is_owned_by_the_name():
    """Taken from the live router table: a site and its HTTP-to-HTTPS redirect are two
    routers carrying the same host, and taking one down must not delete the domain."""
    routers = [
        _router("php-apache-redirecteabr@docker", "Host(`eabr.org`)"),
        _router("php-apacheeabr@docker", "Host(`eabr.org`)"),
    ]
    result = routers_to_domains(routers)

    assert len(result.domains) == 1
    domain = result.domains[0]
    assert domain.owner == "host:eabr.org"
    assert "php-apacheeabr@docker" in domain.note


def test_losing_the_redirect_router_keeps_the_domain():
    store = DomainStore()
    both = [
        _router("php-apache-redirecteabr@docker", "Host(`eabr.org`)"),
        _router("php-apacheeabr@docker", "Host(`eabr.org`)"),
    ]
    store.reconcile(SOURCE_TRAEFIK, routers_to_domains(both).domains)

    # The redirect router goes away; the site router stays.
    outcome = store.reconcile(SOURCE_TRAEFIK, routers_to_domains(both[1:]).domains)

    assert outcome.removed == [], "the domain is still served, so it must survive"
    assert [d.name for d in store.load().domains] == ["eabr.org"]
