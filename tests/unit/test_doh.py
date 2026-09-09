"""Reading what browsers are set to, and getting the safe/unsafe line right.

The distinction that matters and is easy to get backwards: Chrome's ``automatic`` is
**safe**. It upgrades to DoH only when the system resolver is a known DoH provider, and
127.0.0.1 is not one -- which is precisely why the default configuration works and why
warning about it would train the user to ignore the warning.
"""

from __future__ import annotations

import json

from system import doh


def _local_state(root, payload):
    root.mkdir(parents=True, exist_ok=True)
    (root / "Local State").write_text(json.dumps(payload), encoding="utf-8")
    return root


def _prefs(root, text):
    root.mkdir(parents=True, exist_ok=True)
    path = root / "prefs.js"
    path.write_text(text, encoding="utf-8")
    return path


# ---- Chromium ------------------------------------------------------------------------


def test_secure_mode_is_the_problem(tmp_path):
    root = _local_state(tmp_path / "chrome", {"dns_over_https": {"mode": "secure"}})
    finding = doh.read_chromium("Chrome", root)
    assert finding.secure is True
    assert finding.mode == "secure"


def test_automatic_is_safe(tmp_path):
    """Chrome upgrades only for resolvers it recognises as DoH providers."""
    root = _local_state(tmp_path / "chrome", {"dns_over_https": {"mode": "automatic"}})
    assert doh.read_chromium("Chrome", root).secure is False


def test_off_is_safe(tmp_path):
    root = _local_state(tmp_path / "chrome", {"dns_over_https": {"mode": "off"}})
    assert doh.read_chromium("Chrome", root).secure is False


def test_an_absent_key_is_read_as_automatic(tmp_path):
    """What this machine's Chrome and Edge actually look like: no such key at all."""
    root = _local_state(tmp_path / "chrome", {"browser": {"enabled_labs_experiments": []}})
    finding = doh.read_chromium("Chrome", root)
    assert finding.mode == "automatic"
    assert finding.secure is False


def test_unreadable_json_is_not_a_finding(tmp_path):
    root = tmp_path / "chrome"
    root.mkdir()
    (root / "Local State").write_text("{not json", encoding="utf-8")
    assert doh.read_chromium("Chrome", root) is None


def test_a_missing_file_is_not_a_finding(tmp_path):
    assert doh.read_chromium("Chrome", tmp_path / "nowhere") is None


# ---- Firefox -------------------------------------------------------------------------


def test_trr_first_is_still_a_problem(tmp_path):
    """Mode 2 sounds like it falls back to us and does not.

    The fallback covers network failures; a DoH server answering NXDOMAIN for shop.test is
    a perfectly successful response, so Firefox never asks the system.
    """
    path = _prefs(tmp_path / "p1", 'user_pref("network.trr.mode", 2);\n')
    assert doh.read_firefox_profile(path).secure is True


def test_trr_only_is_a_problem(tmp_path):
    path = _prefs(tmp_path / "p1", 'user_pref("network.trr.mode", 3);\n')
    assert doh.read_firefox_profile(path).secure is True


def test_trr_off_is_safe(tmp_path):
    for mode in (0, 5):
        path = _prefs(tmp_path / f"p{mode}", f'user_pref("network.trr.mode", {mode});\n')
        assert doh.read_firefox_profile(path).secure is False, mode


def test_an_untouched_profile_produces_nothing(tmp_path):
    """Firefox's default is off, so silence is the honest answer rather than a reassurance."""
    path = _prefs(tmp_path / "p1", 'user_pref("browser.startup.page", 3);\n')
    assert doh.read_firefox_profile(path) is None


def test_the_preference_is_found_among_others(tmp_path):
    text = (
        'user_pref("browser.startup.page", 3);\n'
        'user_pref("network.trr.mode",   3);\n'
        'user_pref("network.trr.uri", "https://example.invalid/dns-query");\n'
    )
    path = _prefs(tmp_path / "p1", text)
    finding = doh.read_firefox_profile(path)
    assert finding.mode == "3" and finding.secure is True


def test_a_similar_looking_preference_is_not_confused(tmp_path):
    path = _prefs(tmp_path / "p1", 'user_pref("network.trr.mode_backup", 3);\n')
    assert doh.read_firefox_profile(path) is None


# ---- the whole scan ------------------------------------------------------------------


def test_the_scan_reports_safe_profiles_too(tmp_path, monkeypatch):
    """"We looked at Chrome and it is fine" beats silence, which looks like not looking."""
    root = _local_state(tmp_path / "chrome", {"dns_over_https": {"mode": "off"}})
    monkeypatch.setattr(doh, "_chromium_roots", lambda: iter([("Chrome", root)]))
    monkeypatch.setattr(doh, "_firefox_roots", lambda: iter([]))

    findings = doh.scan()
    assert [f.browser for f in findings] == ["Chrome"]
    assert doh.problems(findings) == []


def test_problems_filters_to_what_actually_breaks(tmp_path, monkeypatch):
    good = _local_state(tmp_path / "a", {"dns_over_https": {"mode": "off"}})
    bad = _local_state(tmp_path / "b", {"dns_over_https": {"mode": "secure"}})
    monkeypatch.setattr(
        doh, "_chromium_roots", lambda: iter([("Chrome", good), ("Edge", bad)])
    )
    monkeypatch.setattr(doh, "_firefox_roots", lambda: iter([]))

    assert [f.browser for f in doh.problems()] == ["Edge"]


def test_a_browser_that_is_not_installed_is_skipped(tmp_path, monkeypatch):
    monkeypatch.setattr(
        doh, "_chromium_roots", lambda: iter([("Chrome", tmp_path / "absent")])
    )
    monkeypatch.setattr(doh, "_firefox_roots", lambda: iter([]))
    assert doh.scan() == []


def test_nothing_is_ever_written(tmp_path, monkeypatch):
    """Browser configuration belongs to the user; this module only ever reads."""
    root = _local_state(tmp_path / "chrome", {"dns_over_https": {"mode": "secure"}})
    before = (root / "Local State").read_bytes()
    monkeypatch.setattr(doh, "_chromium_roots", lambda: iter([("Chrome", root)]))
    monkeypatch.setattr(doh, "_firefox_roots", lambda: iter([]))

    doh.scan()
    assert (root / "Local State").read_bytes() == before
