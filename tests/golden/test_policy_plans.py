"""Golden tests for the name-resolution policy planners.

These literals *are* the specification. A reviewer can read exactly what HostBridge will ask
the operating system to do without a Windows machine, and a careless edit to either planner
fails here rather than on somebody's laptop.

Nothing is executed: both planners are pure, and the autouse ``no_subprocess`` fixture would
fail the test if either tried.
"""

from __future__ import annotations

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
#
# The shape here was corrected after running it on a real systemd machine. Configuring the
# machine's own links replaced the servers DHCP had given them and cleared their
# default-route status, so our domains resolved perfectly and nothing else did. We take a
# link of our own instead, and never touch a real one.


def test_the_plan_claims_our_own_link_and_no_real_one():
    plan = resolved_linux.plan_apply(["shop.test", ".dobroedelo.ru"], SERVERS)
    assert plan.commands == (
        ("ip", "link", "add", "hostbridge0", "type", "dummy"),
        ("ip", "link", "set", "hostbridge0", "up"),
        ("ip", "addr", "add", "169.254.53.1/32", "dev", "hostbridge0"),
        ("resolvectl", "dns", "hostbridge0", "127.0.0.1", "::1"),
        ("resolvectl", "domain", "hostbridge0", "~shop.test", "~dobroedelo.ru"),
    )
    assert plan.links == ("hostbridge0",)


def test_no_real_interface_name_can_appear_in_a_plan():
    """The regression that broke general name resolution, as an assertion."""
    plan = resolved_linux.plan_apply(["a.test"], SERVERS)
    flat = " ".join(" ".join(argv) for argv in plan.commands)
    for real in ("eth0", "wlan0", "enp3s0", "eno1"):
        assert real not in flat


def test_the_link_gets_an_address_because_resolved_ignores_it_otherwise():
    """An up-but-addressless dummy shows ``Current Scopes: none`` and is never consulted."""
    plan = resolved_linux.plan_apply(["a.test"], SERVERS)
    assert any(argv[:3] == ("ip", "addr", "add") for argv in plan.commands)


def test_the_address_is_link_local_and_a_single_host():
    """Unroutable by construction, so it cannot collide with a network the machine is on."""
    assert resolved_linux.LINK_ADDRESS.startswith("169.254.")
    assert resolved_linux.LINK_ADDRESS.endswith("/32")


def test_the_link_is_created_before_it_is_configured():
    plan = resolved_linux.plan_apply(["a.test"], SERVERS)
    verbs = [argv[0] for argv in plan.commands]
    assert verbs.index("resolvectl") > max(i for i, v in enumerate(verbs) if v == "ip")


def test_a_routing_domain_carries_the_tilde():
    """Without it resolved treats the entry as a search domain and appends it to bare
    names the user types, which is a different and much ruder behaviour."""
    assert resolved_linux.routing_domain(".foo.test") == "~foo.test"
    assert resolved_linux.routing_domain("app.test") == "~app.test"


def test_removal_reverts_then_deletes_the_link():
    plan = resolved_linux.plan_remove()
    assert plan.commands == (
        ("resolvectl", "revert", "hostbridge0"),
        ("ip", "link", "del", "hostbridge0"),
    )


def test_removal_ignores_the_links_an_older_state_file_names():
    """Those are real interfaces a previous build configured.

    Reverting them now would discard settings that are not ours -- whatever that build did
    to them, this one did not, and resolvectl revert does not know the difference.
    """
    plan = resolved_linux.plan_remove(["eth0", "wlan0"])
    assert plan.commands == (
        ("resolvectl", "revert", "hostbridge0"),
        ("ip", "link", "del", "hostbridge0"),
    )


def test_an_empty_namespace_set_plans_nothing():
    """No domains means no link: an interface with nothing to claim is pure litter."""
    assert resolved_linux.plan_apply([], SERVERS).empty


def test_duplicate_namespaces_collapse_on_linux_too():
    plan = resolved_linux.plan_apply(["a.test", "a.test"], SERVERS)
    domains = [argv for argv in plan.commands if argv[1] == "domain"][0]
    assert domains == ("resolvectl", "domain", "hostbridge0", "~a.test")


# ---- which refusals are not refusals -------------------------------------------------


def test_ip_saying_it_already_exists_is_not_a_failure():
    """Re-applying a changed namespace set runs the same commands, so this is the norm."""
    assert resolved_linux._tolerable(("ip", "link", "add"), "RTNETLINK answers: File exists")


def test_ip_saying_the_device_is_gone_is_not_a_failure():
    assert resolved_linux._tolerable(("ip", "link", "del"), "Cannot find device \"hostbridge0\"")


def test_a_real_ip_failure_is_still_a_failure():
    assert not resolved_linux._tolerable(("ip", "link", "add"), "Operation not permitted")


def test_resolvectl_failures_are_never_tolerated():
    """Only ``ip`` has the repeat-safe verbs; a resolvectl refusal always means something."""
    assert not resolved_linux._tolerable(("resolvectl", "dns"), "File exists")


# ---- is resolved actually consulted? -------------------------------------------------
#
# The other silent failure found by running it: resolvectl answered our domains perfectly
# while getent -- and therefore curl and the browser -- returned nothing, because those go
# through the C library, which reads /etc/resolv.conf and had never heard of resolved.


def _resolv(monkeypatch, tmp_path, text):
    path = tmp_path / "resolv.conf"
    path.write_text(text, encoding="utf-8")
    monkeypatch.setattr(resolved_linux, "RESOLV_CONF", path)


def test_the_stub_is_recognised(monkeypatch, tmp_path):
    _resolv(monkeypatch, tmp_path, "nameserver 127.0.0.53\noptions edns0\n")
    assert resolved_linux.stub_in_use() is True


def test_the_second_stub_address_counts_too(monkeypatch, tmp_path):
    _resolv(monkeypatch, tmp_path, "nameserver 127.0.0.54\n")
    assert resolved_linux.stub_in_use() is True


def test_someone_elses_resolver_is_reported(monkeypatch, tmp_path):
    """What a container runtime or a VPN client leaves behind."""
    _resolv(monkeypatch, tmp_path, "# Generated by Docker Engine.\nnameserver 192.168.65.7\n")
    assert resolved_linux.stub_in_use() is False


def test_a_missing_file_does_not_cry_wolf(monkeypatch, tmp_path):
    monkeypatch.setattr(resolved_linux, "RESOLV_CONF", tmp_path / "absent")
    assert resolved_linux.stub_in_use() is True


def test_a_file_with_no_nameserver_is_not_treated_as_a_fault(monkeypatch, tmp_path):
    _resolv(monkeypatch, tmp_path, "# nothing here\nsearch example.com\n")
    assert resolved_linux.stub_in_use() is True
