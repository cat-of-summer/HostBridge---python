"""Persisted settings, split by who owns them.

The split is forced by the process model rather than by taste. The daemon runs as SYSTEM
or root and has no meaningful home directory; the GUI runs as the interactive user and
cannot write to the machine directory. So preferences that only the GUI cares about live
in :class:`UserConfig` under ``core.paths.user_home``, and everything that changes what the
daemon actually does lives in :class:`DaemonSettings` under ``core.paths.machine_home``,
which the GUI edits over the control API rather than by touching the file.
"""

from __future__ import annotations

import contextlib
from dataclasses import asdict, dataclass, field

from core.jsonio import read_json, write_json_atomic
from core.paths import config_file, machine_home

#: Public resolvers used only when the captured system configuration turns out to be
#: unusable -- every entry pointed back at us, or there was nothing to capture.
DEFAULT_FALLBACK_UPSTREAMS = ("1.1.1.1", "8.8.8.8")

#: Short on purpose. One page load with thirty subresources still issues a single query,
#: while toggling a domain feels immediate even in caches we cannot reach into, such as
#: Chrome's own host cache -- which does honour TTL.
DEFAULT_LOCAL_TTL = 5


def settings_file():
    return machine_home() / "settings.json"


def _str_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item]


@dataclass
class UserConfig:
    """Interactive-user preferences. Losing this file costs nothing but convenience."""

    language: str = ""
    """Empty means "detect from the environment"."""

    theme: str = "dark"
    window_geometry: str = ""
    last_tab: int = 0

    @classmethod
    def load(cls) -> UserConfig:
        raw = read_json(config_file())
        if not isinstance(raw, dict):
            return cls()
        return cls(
            language=raw.get("language") if isinstance(raw.get("language"), str) else "",
            theme=raw.get("theme") if isinstance(raw.get("theme"), str) else "dark",
            window_geometry=(
                raw.get("window_geometry") if isinstance(raw.get("window_geometry"), str) else ""
            ),
            last_tab=raw.get("last_tab") if isinstance(raw.get("last_tab"), int) else 0,
        )

    def save(self) -> None:
        # Failing to save a preference must never take the GUI down with it.
        with contextlib.suppress(OSError):
            write_json_atomic(config_file(), asdict(self))


@dataclass
class DaemonSettings:
    """What the daemon does. Owned by the daemon, edited through the control API."""

    listen_address: str = "127.0.0.1"
    """Escape hatch when something holds the port exclusively: 127.0.0.2 works just as
    well, because Windows and Linux both treat the whole of 127.0.0.0/8 as loopback and
    the resolution rule simply points there instead."""

    listen_port: int = 53
    listen_ipv6: bool = True

    extra_listen: list[str] = field(default_factory=list)
    """Additional addresses to answer on, for containers that cannot reach the host's
    loopback -- typically the host's address on the ``vEthernet (WSL)`` bridge."""

    upstreams: list[str] = field(default_factory=list)
    """Empty means "capture the system's own resolvers at startup"."""

    fallback_upstreams: list[str] = field(default_factory=lambda: list(DEFAULT_FALLBACK_UPSTREAMS))
    local_ttl: int = DEFAULT_LOCAL_TTL

    traefik_enabled: bool = True
    traefik_api: str = "http://127.0.0.1:8080"
    traefik_poll_seconds: int = 10

    bypass_dns_filter: bool = True
    """Listen on an extra address when loopback's port 53 turns out to be filtered.

    A VPN with DNS-leak protection blocks port 53 for every destination, loopback included,
    while permitting its own tunnel interface -- so binding the tunnel's local address as
    well is what makes the resolver reachable at all. See :mod:`system.reachability`.

    Left switchable because the extra address is not loopback. In the case this is built
    for it is a point-to-point tunnel address that nothing else can route to, but on a
    machine where something *else* filters loopback and the default route is the local
    network, the address chosen would face that network. Set to false to forbid that
    outright and accept that the domains will not resolve while the filter is up."""

    docker_enabled: bool = True
    docker_host: str = ""
    """Empty means "the platform default": the named pipe on Windows, the unix socket
    elsewhere. ``DOCKER_HOST`` in the environment wins over both."""

    excluded_adapters: list[str] = field(default_factory=list)
    """Adapters to leave alone, by friendly name. VPN clients that enforce their own
    resolver are the usual reason to add one."""

    @classmethod
    def load(cls) -> DaemonSettings:
        raw = read_json(settings_file())
        if not isinstance(raw, dict):
            return cls()

        defaults = cls()
        loaded = cls(
            listen_address=(
                raw.get("listen_address")
                if isinstance(raw.get("listen_address"), str)
                else defaults.listen_address
            ),
            listen_port=(
                raw.get("listen_port")
                if isinstance(raw.get("listen_port"), int)
                else defaults.listen_port
            ),
            listen_ipv6=bool(raw.get("listen_ipv6", defaults.listen_ipv6)),
            extra_listen=_str_list(raw.get("extra_listen")),
            upstreams=_str_list(raw.get("upstreams")),
            fallback_upstreams=(
                _str_list(raw.get("fallback_upstreams")) or defaults.fallback_upstreams
            ),
            local_ttl=(
                raw.get("local_ttl")
                if isinstance(raw.get("local_ttl"), int)
                else defaults.local_ttl
            ),
            traefik_enabled=bool(raw.get("traefik_enabled", defaults.traefik_enabled)),
            traefik_api=(
                raw.get("traefik_api")
                if isinstance(raw.get("traefik_api"), str)
                else defaults.traefik_api
            ),
            traefik_poll_seconds=(
                raw.get("traefik_poll_seconds")
                if isinstance(raw.get("traefik_poll_seconds"), int)
                else defaults.traefik_poll_seconds
            ),
            bypass_dns_filter=bool(
                raw.get("bypass_dns_filter", defaults.bypass_dns_filter)
            ),
            docker_enabled=bool(raw.get("docker_enabled", defaults.docker_enabled)),
            docker_host=(
                raw.get("docker_host")
                if isinstance(raw.get("docker_host"), str)
                else defaults.docker_host
            ),
            excluded_adapters=_str_list(raw.get("excluded_adapters")),
        )
        # A zero or negative interval would turn a poller into a busy loop.
        loaded.traefik_poll_seconds = max(1, loaded.traefik_poll_seconds)
        loaded.local_ttl = max(0, loaded.local_ttl)
        return loaded

    def save(self) -> None:
        write_json_atomic(settings_file(), asdict(self))
