"""The container label vocabulary, and who ends up owning a name.

The rules under test are the ones that decide whether a `compose up` produces a usable
domain list or a mess: the opt-in switch, both spellings of our own labels, Traefik's rules
read through the same tokeniser as the API, and -- the one that came from real data -- two
containers declaring the same hostname.
"""

from __future__ import annotations

from core.model import SOURCE_DOCKER
from discover.labels import (
    address_of,
    container_name,
    containers_to_domains,
    labels_of,
    wanted_names,
)


def container(name: str, labels: dict[str, str], **extra):
    payload = {"Id": f"{name}0123456789abcdef", "Names": [f"/{name}"], "Labels": labels}
    payload.update(extra)
    return payload


# ---- our own labels -----------------------------------------------------------------


def test_our_labels_need_the_opt_in():
    """Without ``hostbridge.enable`` nothing is claimed.

    Every container with a published port becoming a domain would fill the list with things
    the user never asked for, so the switch is required rather than inferred.
    """
    assert wanted_names({"hostbridge.domain": "shop.test"}) == []


def test_the_opt_in_accepts_the_usual_spellings():
    for value in ("1", "true", "TRUE", "yes", "on"):
        found = wanted_names({"hostbridge.enable": value, "hostbridge.domain": "shop.test"})
        assert found == ["shop.test"], value


def test_the_opt_in_rejects_anything_else():
    assert wanted_names({"hostbridge.enable": "maybe", "hostbridge.domain": "shop.test"}) == []


def test_both_spellings_are_read_and_the_list_is_split():
    found = wanted_names(
        {
            "hostbridge.enable": "true",
            "hostbridge.domain": "shop.test",
            "hostbridge.domains": "api.shop.test, *.cdn.shop.test ,",
        }
    )
    assert found == ["shop.test", "api.shop.test", "*.cdn.shop.test"]


def test_a_name_declared_twice_is_listed_once():
    found = wanted_names(
        {
            "hostbridge.enable": "true",
            "hostbridge.domain": "shop.test",
            "hostbridge.domains": "shop.test,api.shop.test",
        }
    )
    assert found == ["shop.test", "api.shop.test"]


def test_the_address_defaults_and_is_overridable():
    assert address_of({}) == "127.0.0.1"
    assert address_of({"hostbridge.address": " 127.0.0.9 "}) == "127.0.0.9"


# ---- Traefik's labels ---------------------------------------------------------------


def test_a_traefik_rule_is_an_opt_in_by_itself():
    """Declaring a ``Host()`` rule *is* the opt-in.

    A container that already says what it answers to has said it once; asking the user to
    repeat it in a second vocabulary would be rude.
    """
    found = wanted_names({"traefik.http.routers.shop.rule": "Host(`shop.test`)"})
    assert found == ["shop.test"]


def test_a_traefik_rule_is_parsed_not_regexed():
    """The same tokeniser as the API, so a neighbouring predicate is not swallowed."""
    found = wanted_names(
        {
            "traefik.http.routers.a.rule": "Host(`a.test`,`b.test`) && PathPrefix(`/x`)",
            "traefik.http.routers.b.rule": "Host(`c.test`) || Host(`d.test`)",
        }
    )
    assert found == ["a.test", "b.test", "c.test", "d.test"]


def test_only_rule_labels_are_read():
    found = wanted_names(
        {
            "traefik.http.routers.shop.entrypoints": "websecure",
            "traefik.http.services.shop.loadbalancer.server.port": "80",
        }
    )
    assert found == []


def test_the_two_vocabularies_combine():
    found = wanted_names(
        {
            "hostbridge.enable": "true",
            "hostbridge.domain": "shop.test",
            "traefik.http.routers.shop.rule": "Host(`www.shop.test`)",
        }
    )
    assert found == ["shop.test", "www.shop.test"]


def test_label_keys_are_matched_case_insensitively():
    found = labels_of(container("c", {"HostBridge.Enable": "true", "HOSTBRIDGE.DOMAIN": "a.test"}))
    assert wanted_names(found) == ["a.test"]


# ---- the listing --------------------------------------------------------------------


def test_the_container_name_loses_dockers_leading_slash():
    assert container_name({"Names": ["/php-apache"], "Id": "abc"}) == "php-apache"


def test_a_container_without_names_falls_back_to_a_short_id():
    assert container_name({"Id": "0123456789abcdef0000"}) == "0123456789ab"


def test_a_listing_becomes_domains():
    result = containers_to_domains(
        [
            container("shop", {"hostbridge.enable": "true", "hostbridge.domain": "shop.test"}),
            container("idle", {}),
        ]
    )
    assert [domain.name for domain in result.domains] == ["shop.test"]
    domain = result.domains[0]
    assert domain.source == SOURCE_DOCKER
    assert domain.address == "127.0.0.1"
    assert domain.note == "shop"


def test_a_name_is_owned_by_the_hostname_not_the_container():
    """The regression that came from live data.

    ``eabr.org`` is declared by both a service and its redirect helper. Owning the record by
    container would make it vanish when one of them stops and reappear under a different
    owner on the next poll; owning it by hostname keeps it stable.
    """
    result = containers_to_domains(
        [
            container("php-apache", {"traefik.http.routers.a.rule": "Host(`eabr.org`)"}),
            container("php-redirect", {"traefik.http.routers.b.rule": "Host(`eabr.org`)"}),
        ]
    )
    assert len(result.domains) == 1
    assert result.domains[0].owner == "host:eabr.org"
    assert result.domains[0].note == "php-apache, php-redirect"


def test_an_unusable_name_is_reported_rather_than_dropped_silently():
    result = containers_to_domains(
        [container("bad", {"hostbridge.enable": "true", "hostbridge.domain": "not a domain"})]
    )
    assert result.domains == []
    assert result.skipped_invalid == ["not a domain"]


def test_names_are_normalised_before_they_are_compared():
    result = containers_to_domains(
        [
            container("a", {"traefik.http.routers.a.rule": "Host(`Shop.Test`)"}),
            container("b", {"traefik.http.routers.b.rule": "Host(`shop.test.`)"}),
        ]
    )
    assert len(result.domains) == 1


def test_an_empty_listing_is_not_an_error():
    assert containers_to_domains([]).domains == []
    assert containers_to_domains(None).domains == []
