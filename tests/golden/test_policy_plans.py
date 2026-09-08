"""Golden tests for the name-resolution policy planners.

These literals *are* the specification. A reviewer can read exactly what HostBridge will ask
the operating system to do without a Windows machine, and a careless edit to either planner
fails here rather than on somebody's laptop.

Nothing is executed: both planners are pure, and the autouse ``no_subprocess`` fixture would
fail the test if either tried.
"""

from __future__ import annotations

import pytest

from system import nrpt_win, resolved_linux

SERVERS = ["127.0.0.1", "::1"]


# ---- Windows NRPT -----------------------------------------------------------------


EXPECTED_ADD_EXACT = (
    "Add-DnsClientNrptRule -Namespace 'shop.test' "
    "-NameServers '127.0.0.1','::1' "
    "-Comment 'HostBridge' "
    "-DisplayName 'HostBridge shop.test' "
    "-ErrorAction Stop"
)

EXPECTED_ADD_SUFFIX = (
    "Add-DnsClientNrptRule -Namespace '.dobroedelo.ru' "
    "-NameServers '127.0.0.1','::1' "
    "-Comment 'HostBridge' "
    "-DisplayName 'HostBridge .dobroedelo.ru' "
    "-ErrorAction Stop"
)

EXPECTED_REMOVE_EXACT = (
    "Get-DnsClientNrptRule -ErrorAction SilentlyContinue | "
    "Where-Object { $_.Comment -eq 'HostBridge' -and $_.Namespace -eq 'shop.test' } | "
    "Remove-DnsClientNrptRule -Force -ErrorAction SilentlyContinue"
)


def test_nrpt_plan_scripts_exactly_what_is_expected():
    plan = nrpt_win.plan_apply(["shop.test", ".dobroedelo.ru"], SERVERS, present=[])
    assert plan.add == ("shop.test", ".dobroedelo.ru")
    assert plan.remove == ()
    assert plan.script == (EXPECTED_ADD_EXACT, EXPECTED_ADD_SUFFIX)


def test_nrpt_plan_is_a_set_difference_not_a_rewrite():
    """Toggling one domain must be one rule operation, not a rebuild of the whole table."""
    plan = nrpt_win.plan_apply(
        ["shop.test", "new.test"], SERVERS, present=["shop.test", "gone.test"]
    )
    assert plan.add == ("new.test",)
    assert plan.remove == ("gone.test",)


def test_applying_the_same_set_twice_is_a_no_op():
    plan = nrpt_win.plan_apply(["shop.test"], SERVERS, present=["shop.test"])
    assert plan.empty
    assert plan.script == ()


def test_removals_are_scripted_before_additions():
    """A namespace whose servers changed must not briefly have two rules racing."""
    plan = nrpt_win.plan_apply(["a.test"], SERVERS, present=["b.test"])
    assert plan.script[0].startswith("Get-DnsClientNrptRule")
    assert plan.script[1].startswith("Add-DnsClientNrptRule")


def test_removing_a_namespace_filters_on_our_marker():
    """A corporate Group Policy can own rules in the same table; deleting one would break
    the user's VPN in a way they would never connect back to this application."""
    plan = nrpt_win.plan_remove(["shop.test"])
    assert plan.script == (EXPECTED_REMOVE_EXACT,)
    assert "$_.Comment -eq 'HostBridge'" in plan.script[0]


def test_remove_all_is_scoped_to_our_marker_too():
    plan = nrpt_win.plan_remove_all()
    assert len(plan.script) == 1
    assert "$_.Comment -eq 'HostBridge'" in plan.script[0]
    assert "Remove-DnsClientNrptRule -Force" in plan.script[0]


def test_the_whole_batch_becomes_one_powershell_invocation():
    """Start-up dominates the cost, so twenty rules must not mean twenty processes."""
    plan = nrpt_win.plan_apply(["a.test", "b.test", "c.test"], SERVERS, present=[])
    argv = nrpt_win.to_argv(plan.script)
    assert argv[0] == "powershell"
    assert "-NoProfile" in argv
    assert argv.count("-Command") == 1
    assert argv[-1].count("Add-DnsClientNrptRule") == 3


def test_a_quote_in_a_namespace_cannot_break_out_of_the_string():
    """The quote is doubled, so the payload stays inside one PowerShell literal."""
    hostile = "evil'; Remove-Item C:\\ -Recurse; #"
    plan = nrpt_win.plan_remove([hostile])

    assert "'evil''; Remove-Item C:\\ -Recurse; #'" in plan.script[0]
    # Every quote is part of a pair: an odd count would mean the literal was left open and
    # everything after it would be parsed as code.
    assert plan.script[0].count("'") % 2 == 0


def test_duplicate_namespaces_collapse():
    plan = nrpt_win.plan_apply(["a.test", "a.test"], SERVERS, present=[])
    assert plan.add == ("a.test",)


# ---- Linux systemd-resolved -------------------------------------------------------


def test_resolved_plan_sets_servers_then_routing_domains():
    plan = resolved_linux.plan_apply(["eth0"], ["shop.test", ".dobroedelo.ru"], SERVERS)
    assert plan.commands == (
        ("resolvectl", "dns", "eth0", "127.0.0.1", "::1"),
        ("resolvectl", "domain", "eth0", "~shop.test", "~dobroedelo.ru"),
    )


def test_a_routing_domain_carries_the_tilde():
    """Without it resolved treats the entry as a search domain and appends it to bare
    names, which is a completely different and unwanted behaviour."""
    assert resolved_linux.routing_domain(".foo.test") == "~foo.test"
    assert resolved_linux.routing_domain("app.test") == "~app.test"


def test_resolved_covers_every_link():
    plan = resolved_linux.plan_apply(["eth0", "wlan0"], ["a.test"], SERVERS)
    assert len(plan.commands) == 4
    assert plan.links == ("eth0", "wlan0")


def test_resolved_removal_is_a_single_revert_per_link():
    plan = resolved_linux.plan_remove(["eth0", "wlan0"])
    assert plan.commands == (
        ("resolvectl", "revert", "eth0"),
        ("resolvectl", "revert", "wlan0"),
    )


def test_an_empty_plan_reports_itself_as_empty():
    assert resolved_linux.plan_apply([], [], SERVERS).empty
    assert resolved_linux.plan_remove([]).empty


@pytest.mark.parametrize(
    "name", ["lo", "docker0", "br-abc123", "veth1234", "virbr0", "tun0", "wg0"]
)
def test_virtual_interfaces_are_skipped(name):
    assert name.startswith(resolved_linux.SKIP_PREFIXES)


def test_real_interfaces_are_not_skipped():
    for name in ("eth0", "wlan0", "enp3s0", "eno1"):
        assert not name.startswith(resolved_linux.SKIP_PREFIXES)
