"""Capturing the resolvers the machine was already using.

Called once, **before** any rule of ours exists, so what we capture is the untouched
configuration. Everything we cannot resolve locally is forwarded to these.

On Windows this asks PowerShell for a property rather than parsing ``ipconfig /all``: that
command's output is localised, and on a Russian Windows a prose parser returns nothing while
looking like it worked. We already pay for PowerShell in :mod:`system.nrpt_win` and this runs
once at start-up.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from core import log
from system.run import CommandError, run

IS_WINDOWS = os.name == "nt"

RESOLV_CONF = Path("/etc/resolv.conf")

TIMEOUT = 20.0

_NAMESERVER = re.compile(r"^\s*nameserver\s+(\S+)", re.MULTILINE)


def _windows_servers() -> list[str]:
    argv = (
        "powershell",
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        # Only interfaces that are up and actually have servers configured. The property is
        # requested by name, so nothing here depends on the display language.
        "Get-DnsClientServerAddress -ErrorAction SilentlyContinue | "
        "Where-Object { $_.ServerAddresses } | "
        "Select-Object -ExpandProperty ServerAddresses",
    )
    try:
        completed = run(argv, timeout=TIMEOUT)
    except CommandError as exc:
        log.warn(f"dnsservers: {exc}")
        return []
    if not completed.ok:
        log.warn(f"dnsservers: powershell exited {completed.returncode}")
        return []
    return [line.strip() for line in completed.stdout.splitlines() if line.strip()]


def parse_resolv_conf(text: str) -> list[str]:
    """Pull the nameservers out of a ``resolv.conf``. Pure, so it is unit-tested."""
    return [match.group(1) for match in _NAMESERVER.finditer(text)]


def _posix_servers() -> list[str]:
    found: list[str] = []
    try:
        found = parse_resolv_conf(RESOLV_CONF.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        found = []

    # Under systemd-resolved, resolv.conf points at the 127.0.0.53 stub and the real
    # servers are only visible through resolvectl -- so the loopback entry we would
    # otherwise capture is useless and the stub has to be asked directly.
    if not found or all(entry.startswith("127.") for entry in found):
        try:
            completed = run(["resolvectl", "status"], timeout=TIMEOUT)
        except CommandError:
            return found
        if completed.ok:
            found.extend(_parse_resolvectl(completed.stdout))
    return found


def _parse_resolvectl(text: str) -> list[str]:
    """Read ``Current DNS Server`` and ``DNS Servers`` out of ``resolvectl status``."""
    servers: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        for prefix in ("Current DNS Server:", "DNS Servers:"):
            if stripped.startswith(prefix):
                servers.extend(stripped[len(prefix) :].split())
    return servers


def capture() -> list[str]:
    """The resolvers currently configured on this machine, unfiltered.

    Loop protection -- discarding anything that points back at us -- lives in
    :func:`resolver.upstream.sanitise_upstreams`, so that it applies to a hand-configured
    list just as much as to a captured one.
    """
    servers = _windows_servers() if IS_WINDOWS else _posix_servers()
    log.write(f"dnsservers: captured {servers or 'nothing'}")
    return servers
