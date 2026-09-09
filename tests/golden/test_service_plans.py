"""Golden tests for what would be registered with the platform's service manager.

These literals are the specification. A reviewer can read exactly what HostBridge asks
Windows and systemd to do without either platform in front of them, and a careless edit
fails here rather than on somebody's machine.

Nothing is executed: both planners are pure, and the autouse ``no_subprocess`` fixture would
fail the test if either tried.
"""

from __future__ import annotations

from system import service_linux, service_win

EXECUTABLE = r"C:\Program Files\HostBridge\hostbridge.exe"
LINUX_EXECUTABLE = "/opt/hostbridge/hostbridge"


# ---- Windows ----------------------------------------------------------------------


def test_the_install_plan_registers_starts_and_arms_recovery():
    plan = service_win.plan_install(EXECUTABLE)
    assert plan.commands == (
        (
            "sc",
            "create",
            "HostBridge",
            "binPath= ",
            f'"{EXECUTABLE}" --service',
            "start= ",
            "auto",
            "DisplayName= ",
            "HostBridge local DNS",
            "depend= ",
            "Dnscache/Tcpip",
        ),
        (
            "sc",
            "description",
            "HostBridge",
            "Answers virtual development domains and forwards everything else.",
        ),
        (
            "sc",
            "failure",
            "HostBridge",
            "reset= ",
            "60",
            "actions= ",
            "restart/5000/restart/10000/restart/30000",
        ),
    )


def test_the_service_runs_our_own_binary_with_the_service_flag():
    """A plain exe with no SCM handshake fails with error 1053, so the flag matters."""
    plan = service_win.plan_install(EXECUTABLE)
    assert plan.commands[0][4].endswith("--service")


def test_sc_arguments_keep_the_space_after_the_equals_sign():
    """sc.exe rejects ``binPath=value``. It reads like a typo and is not one."""
    plan = service_win.plan_install(EXECUTABLE)
    for argument in plan.commands[0]:
        if argument.endswith("= "):
            assert argument.endswith(" ")
    assert "binPath= " in plan.commands[0]


def test_the_service_waits_for_the_dns_client_and_tcpip():
    """Both are needed before a socket can be bound or a rule registered."""
    assert "Dnscache/Tcpip" in service_win.plan_install(EXECUTABLE).commands[0]


def test_the_uninstall_plan_stops_before_deleting():
    plan = service_win.plan_uninstall()
    assert plan.commands == (
        ("sc", "stop", "HostBridge"),
        ("sc", "delete", "HostBridge"),
    )


def test_start_and_stop_are_single_commands():
    assert service_win.plan_start().commands == (("sc", "start", "HostBridge"),)
    assert service_win.plan_stop().commands == (("sc", "stop", "HostBridge"),)


# ---- Linux ------------------------------------------------------------------------


EXPECTED_UNIT = """\
[Unit]
Description=HostBridge local DNS for virtual development domains
Documentation=https://github.com/cat-of-summer/HostBridge---python
After=network-online.target systemd-resolved.service
Wants=network-online.target

[Service]
Type=simple
ExecStart=/opt/hostbridge/hostbridge --service
ExecStopPost=/opt/hostbridge/hostbridge --repair --quiet
Restart=on-failure
RestartSec=5
AmbientCapabilities=CAP_NET_BIND_SERVICE
CapabilityBoundingSet=CAP_NET_BIND_SERVICE CAP_NET_ADMIN
StateDirectory=hostbridge

[Install]
WantedBy=multi-user.target
"""


def test_the_generated_unit_is_exactly_this():
    assert service_linux.render_unit(LINUX_EXECUTABLE) == EXPECTED_UNIT


def test_the_unit_repairs_on_stop_whatever_killed_it():
    """Belt and braces: a SIGKILL followed by a unit stop must still remove the rules, or a
    namespace is left pointing at a socket nobody holds."""
    unit = service_linux.render_unit(LINUX_EXECUTABLE)
    assert "ExecStopPost=/opt/hostbridge/hostbridge --repair --quiet" in unit


def test_the_unit_grants_the_capability_needed_for_port_53():
    assert "AmbientCapabilities=CAP_NET_BIND_SERVICE" in service_linux.render_unit(
        LINUX_EXECUTABLE
    )


def test_the_unit_starts_after_resolved_because_it_reconfigures_it():
    assert "After=network-online.target systemd-resolved.service" in service_linux.render_unit(
        LINUX_EXECUTABLE
    )


def test_the_install_plan_reloads_enables_and_starts():
    plan = service_linux.plan_install(LINUX_EXECUTABLE)
    assert plan.unit == EXPECTED_UNIT
    assert plan.commands == (
        ("systemctl", "daemon-reload"),
        ("systemctl", "enable", "hostbridge.service"),
        ("systemctl", "start", "hostbridge.service"),
    )


def test_the_uninstall_plan_stops_disables_and_reloads():
    assert service_linux.plan_uninstall().commands == (
        ("systemctl", "stop", "hostbridge.service"),
        ("systemctl", "disable", "hostbridge.service"),
        ("systemctl", "daemon-reload"),
    )


def test_an_empty_plan_reports_itself_as_empty():
    assert service_linux.ServicePlan().empty
    assert service_win.ServicePlan(commands=()).empty
