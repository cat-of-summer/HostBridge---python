from __future__ import annotations

import pytest

from core.match import (
    HSTS_PRELOADED_TLDS,
    Zone,
    apex_of,
    is_wildcard,
    normalise,
    parent_suffixes,
    validate,
)
from core.model import Domain

# ---- normalisation ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Shop.TEST", "shop.test"),
        ("  shop.test  ", "shop.test"),
        ("shop.test.", "shop.test"),
        ("SHOP.TEST.", "shop.test"),
        ("*.Foo.Test", "*.foo.test"),
        ("", ""),
        ("   ", ""),
    ],
)
def test_normalise(raw, expected):
    assert normalise(raw) == expected


def test_normalise_punycodes_non_ascii_labels():
    assert normalise("пример.test") == "xn--e1afmkfd.test"


def test_normalise_leaves_ascii_labels_alone():
    # Round-tripping every label through the idna codec would reject perfectly ordinary
    # names such as an underscore-prefixed service label.
    assert normalise("_acme-challenge.shop.test") == "_acme-challenge.shop.test"


def test_apex_and_wildcard_helpers():
    assert is_wildcard("*.foo.test")
    assert not is_wildcard("foo.test")
    assert apex_of("*.foo.test") == "foo.test"
    assert apex_of("foo.test") == "foo.test"


# ---- validation -------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "shop.test",
        "api.shop.test",
        "*.shop.test",
        "etm39.ru",
        "www.hevelsolar.com",
        "vnu.localhost",
    ],
)
def test_valid_names(name):
    assert validate(name).ok


@pytest.mark.parametrize(
    ("name", "key"),
    [
        ("", "domain.error_empty"),
        ("*.", "domain.error_wildcard_bare"),
        ("foo.*.test", "domain.error_wildcard_position"),
        ("shop..test", "domain.error_empty_label"),
        ("-shop.test", "domain.error_label_hyphen"),
        ("shop-.test", "domain.error_label_hyphen"),
        ("sh op.test", "domain.error_label_chars"),
        ("shop/test.ru", "domain.error_label_chars"),
        ("localhost", "domain.error_single_label"),
    ],
)
def test_invalid_names_report_the_right_error(name, key):
    result = validate(name)
    assert not result.ok
    assert result.error is not None
    assert result.error.key == key


def test_a_label_over_63_characters_is_rejected():
    result = validate("a" * 64 + ".test")
    assert result.error is not None
    assert result.error.key == "domain.error_label_too_long"


def test_a_name_over_253_characters_is_rejected():
    result = validate(".".join(["abcdefghij"] * 25) + ".test")
    assert result.error is not None
    assert result.error.key == "domain.error_too_long"


def test_local_warns_about_mdns_and_suggests_test():
    result = validate("shop.local")
    assert result.ok, "a .local name is usable, just usually not resolvable -- warn, do not block"
    keys = [issue.key for issue in result.warnings]
    assert "domain.warn_mdns" in keys
    suggestion = next(i for i in result.warnings if i.key == "domain.warn_mdns")
    assert suggestion.params["suggested"] == "shop.test"


@pytest.mark.parametrize("tld", ["dev", "app", "page", "zip"])
def test_hsts_preloaded_tlds_warn(tld):
    result = validate(f"shop.{tld}")
    assert result.ok
    assert any(issue.key == "domain.warn_hsts_tld" for issue in result.warnings)


def test_safe_tlds_produce_no_warnings():
    assert validate("shop.test").warnings == ()
    assert validate("shop.localhost").warnings == ()


def test_hsts_list_and_mdns_do_not_overlap():
    assert "local" not in HSTS_PRELOADED_TLDS


def test_a_wildcard_warns_that_the_apex_is_not_covered():
    result = validate("*.shop.test")
    assert result.ok
    warning = next(i for i in result.warnings if i.key == "domain.warn_wildcard_apex")
    assert warning.params["apex"] == "shop.test"


# ---- the match ladder -------------------------------------------------------------


def test_parent_suffixes_yields_longest_first_and_excludes_the_name():
    assert list(parent_suffixes("a.b.foo.test")) == ["b.foo.test", "foo.test", "test"]
    assert list(parent_suffixes("test")) == []


def _zone(*specs) -> Zone:
    return Zone.from_domains([Domain(name=n, address=a, enabled=e) for n, a, e in specs])


def test_exact_match_wins():
    zone = _zone(("shop.test", "127.0.0.1", True))
    assert zone.lookup("shop.test") == "127.0.0.1"
    assert zone.lookup("SHOP.TEST.") == "127.0.0.1"


def test_unknown_name_forwards():
    assert _zone(("shop.test", "127.0.0.1", True)).lookup("google.com") is None


def test_wildcard_covers_descendants_at_any_depth():
    zone = _zone(("*.shop.test", "10.0.0.1", True))
    assert zone.lookup("a.shop.test") == "10.0.0.1"
    assert zone.lookup("a.b.c.shop.test") == "10.0.0.1"


def test_wildcard_does_not_cover_its_own_apex():
    """The rule people forget, and the reason the UI offers to add the apex too."""
    zone = _zone(("*.shop.test", "10.0.0.1", True))
    assert zone.lookup("shop.test") is None


def test_exact_beats_a_wildcard_that_also_matches():
    zone = _zone(("*.shop.test", "10.0.0.1", True), ("api.shop.test", "127.0.0.1", True))
    assert zone.lookup("api.shop.test") == "127.0.0.1"
    assert zone.lookup("other.shop.test") == "10.0.0.1"


def test_the_longest_wildcard_suffix_wins():
    zone = _zone(("*.shop.test", "10.0.0.1", True), ("*.api.shop.test", "10.0.0.2", True))
    assert zone.lookup("v1.api.shop.test") == "10.0.0.2"
    assert zone.lookup("v1.web.shop.test") == "10.0.0.1"


def test_a_disabled_domain_forwards_instead_of_answering():
    """Switching a shadowed production domain off must hand the real site straight back."""
    zone = _zone(("etm39.ru", "127.0.0.1", False))
    assert zone.lookup("etm39.ru") is None


def test_empty_zone_answers_nothing():
    zone = Zone.from_domains([])
    assert len(zone) == 0
    assert zone.lookup("shop.test") is None


def test_namespaces_render_exact_names_and_dotted_suffixes():
    zone = _zone(
        ("shop.test", "127.0.0.1", True),
        ("*.api.shop.test", "127.0.0.1", True),
        ("off.test", "127.0.0.1", False),
    )
    assert zone.namespaces() == (".api.shop.test", "shop.test")


def test_zone_ignores_records_with_an_unusable_name():
    zone = Zone.from_domains([Domain(name="   ", address="127.0.0.1")])
    assert len(zone) == 0
