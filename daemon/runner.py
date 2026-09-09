"""The daemon's lifecycle, and the ordering guarantees that make it safe.

The order below is the whole safety story, and it is asserted by a test rather than left to
care:

1. capture the machine's resolvers -- **before** any rule of ours exists, so what we capture
   is the untouched configuration;
2. **bind the sockets** -- if port 53 is taken the process stops here and the machine's name
   resolution is byte-identical to a moment ago;
3. write ``netstate.json`` holding what we are *about* to claim;
4. only then apply the policy.

Reverse on the way out. A crash skips steps 4 and 5, and the state file plus the marker on
every rule are what let ``--repair`` clean up afterwards.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import socket

from core import log
from core.config import DaemonSettings
from core.events import Bus
from core.match import Zone
from core.paths import machine_home
from core.store import DomainStore
from daemon import netstate
from daemon.policy import Policy, PolicyError, PolicyState, detect
from resolver.cache import Cache
from resolver.server import BindError, Resolver, Server, bind_sockets
from resolver.upstream import Upstream, sanitise_upstreams
from system import secure
from ui.i18n import t

#: Gives the interpreter a bytecode boundary to raise KeyboardInterrupt at. Without it a
#: Proactor loop parked in GetQueuedCompletionStatus does not notice Ctrl+C promptly, and
#: --daemon-foreground needs Ctrl+Break or a kill to stop.
HEARTBEAT_SECONDS = 0.2

#: How often the store file is checked for an edit made by another process.
STORE_POLL_SECONDS = 1.0

#: How often the owning process is checked. Two seconds is imperceptible on the way out
#: and costs one cheap handle query.
OWNER_POLL_SECONDS = 2.0


class Runner:
    """Owns the sockets, the resolver and the applied policy for one daemon run."""

    def __init__(
        self,
        settings: DaemonSettings | None = None,
        store: DomainStore | None = None,
        policy: Policy | None = None,
        *,
        control_enabled: bool = True,
        owner_pid: int = 0,
    ) -> None:
        self.settings = settings or DaemonSettings.load()
        self.store = store or DomainStore()
        self.policy = policy or detect(self.settings.excluded_adapters)

        self.resolver: Resolver | None = None
        self.server: Server | None = None
        self.applied = PolicyState()
        self.upstreams: list[str] = []
        self.policy_error: str = ""

        self.bypass_listen: list[str] = []
        """Extra addresses bound because loopback's DNS port is filtered. Usually empty."""

        self.filtered_primary = False
        """Set when a packet filter forbids reaching our main listen address."""

        self.traefik_ok = True
        """Starts optimistic so the first failure is what gets logged, not the first poll."""

        self.docker_ok = True
        """The same, for the engine: a machine without Docker logs one line, not one a poll."""

        self.control_enabled = control_enabled
        self.api = None
        self.bus = Bus()

        self.owner_pid = owner_pid
        """The process this daemon belongs to, or 0 when it belongs to nobody.

        Set when the window started us: closing the application must put the machine back
        as it was, and a resolver left running with rules applied and no interface to
        manage it is exactly the orphan ``--repair`` exists to mop up. Watching the owner
        turns that into an ordinary shutdown instead. A service passes 0 -- it outlives
        every window by design.
        """

        self._stop = asyncio.Event()
        self._sockets: tuple[list[socket.socket], list[socket.socket]] = ([], [])

    # ---- addresses ----------------------------------------------------------------

    def listen_addresses(self) -> list[str]:
        addresses = [self.settings.listen_address]
        if self.settings.listen_ipv6:
            addresses.append("::1")
        addresses.extend(self.settings.extra_listen)
        addresses.extend(self.bypass_listen)
        return list(dict.fromkeys(addresses))

    def detect_filter_bypass(self) -> list[str]:
        """Find an address a DNS-blocking VPN filter would let a query through on.

        Loopback stays first in the list either way. The policy hands every listen address
        to the resolver table as a name server, so a client tries them in turn -- which
        means the pair keeps working whether the tunnel is up or down, instead of trading
        one broken state for another.
        """
        if not self.settings.bypass_dns_filter:
            return []

        from system import reachability

        primary = self.settings.listen_address
        port = self.settings.listen_port
        self.filtered_primary = reachability.filtered(primary, port)
        if not self.filtered_primary:
            return []

        extra = reachability.bypass_for(primary, port)
        if not extra:
            log.warn(t("error.dns_filtered", address=primary, port=port))
            return []

        log.warn(t("error.dns_filtered_bypass", address=primary, port=port, bypass=extra))
        return [extra]

    # ---- start ---------------------------------------------------------------------

    def capture_upstreams(self) -> list[str]:
        from system import dnsservers

        raw = self.settings.upstreams or dnsservers.capture()
        clean = sanitise_upstreams(raw, self.listen_addresses())
        if not clean:
            log.warn("runner: nothing usable was captured; falling back to public resolvers")
            clean = sanitise_upstreams(self.settings.fallback_upstreams, self.listen_addresses())
        return clean

    def clear_leftovers(self) -> None:
        """Undo a previous run that did not shut down cleanly.

        Running before anything is applied, because a stale rule pointing at a socket
        nobody is listening on is the one failure mode that makes a namespace stop
        resolving entirely.
        """
        previous = netstate.read()
        if previous is None:
            return
        if previous.clean_shutdown:
            netstate.clear()
            return
        if netstate.pid_alive(previous.pid) and previous.pid != os.getpid():
            log.warn(f"runner: another daemon appears to be running as pid {previous.pid}")
            return

        log.warn("runner: previous run did not shut down cleanly; clearing its rules")
        with contextlib.suppress(PolicyError):
            self.policy.remove_all()
        netstate.clear()

    def build_zone(self) -> Zone:
        snapshot = self.store.load()
        return Zone.from_domains(snapshot.domains)

    async def start(self) -> None:
        with contextlib.suppress(secure.PermissionWarning, OSError):
            machine_home().mkdir(parents=True, exist_ok=True)
            secure.harden_dir(machine_home())

        self.clear_leftovers()
        # Before the upstreams are captured, so an address we are about to listen on cannot
        # also end up in the upstream list and turn the resolver into its own client.
        self.bypass_listen = self.detect_filter_bypass()
        self.upstreams = self.capture_upstreams()

        zone = self.build_zone()
        cache = Cache()
        upstream = Upstream(self.upstreams, cache, timeout=2.0)
        self.resolver = Resolver(zone, cache, upstream, self.settings.local_ttl)

        # Bind first. A failure here must leave the machine untouched.
        udp, tcp = bind_sockets(self.listen_addresses(), self.settings.listen_port)
        self._sockets = (udp, tcp)

        state = netstate.NetState.start(
            mechanism=self.policy.name,
            listen=self.listen_addresses(),
            port=self.settings.listen_port,
            upstreams=self.upstreams,
        )
        state.intent = list(zone.namespaces())
        netstate.write(state)

        self.server = Server(self.resolver, udp, tcp)
        await self.server.start()

        self.apply_policy(zone, state)

        if self.control_enabled:
            from daemon.api import ControlApi

            self.api = ControlApi(self, self.bus)
            await self.api.start()
            # Only once the API exists, because a line published to nobody is just work.
            log.add_sink(self._publish_log)

    def apply_policy(self, zone: Zone, state: netstate.NetState) -> None:
        namespaces = list(zone.namespaces())
        if not namespaces:
            self.applied = PolicyState()
            state.confirmed = []
            netstate.write(state)
            return

        try:
            self.applied = self.policy.apply(namespaces, self.listen_addresses())
            self.policy_error = ""
        except PolicyError as exc:
            # Not fatal. The resolver is already listening and answers a direct query, which
            # is exactly the state an unelevated --daemon-foreground lands in; saying so is
            # more useful than refusing to run.
            self.policy_error = str(exc)
            log.warn(f"runner: {t('error.policy_failed', error=exc)}")
            self.applied = PolicyState()

        state.confirmed = list(self.applied.namespaces)
        state.links = list(self.applied.links)
        netstate.write(state)
        self.policy.flush()

    # ---- reload --------------------------------------------------------------------

    def reload(self) -> None:
        """Recompile the zone and re-claim the difference. Cheap enough to call freely."""
        if self.resolver is None:
            return
        zone = self.build_zone()
        before = self.resolver.zone.namespaces()
        self.resolver.apply(zone)
        if zone.namespaces() == before:
            return

        state = netstate.read() or netstate.NetState.start(
            mechanism=self.policy.name,
            listen=self.listen_addresses(),
            port=self.settings.listen_port,
            upstreams=self.upstreams,
        )
        state.intent = sorted(set(state.intent) | set(zone.namespaces()))
        netstate.write(state)
        self.apply_policy(zone, state)

    def _publish_log(self, level: str, message: str) -> None:
        """Put every log line on the event stream, so the window's log view has a source.

        It had none: nothing ever published ``kind="log"``, so the view showed the
        interface's own start-up messages and then sat there, looking frozen while the
        daemon worked perfectly.
        """
        self.bus.publish("log", level=level, message=message)

    # ---- discovery -------------------------------------------------------------------

    def _announce(self, source: str, outcome) -> None:
        """Tell the subscribers a sync pass changed something.

        Without this an import is invisible to an open window until the user does something
        that happens to refresh it -- the very case Docker makes common, because a
        ``compose up`` rewrites the list while nobody is touching the keyboard.
        """
        self.bus.publish(
            "domains",
            action="sync",
            source=source,
            added=list(outcome.added),
            updated=list(outcome.updated),
            removed=list(outcome.removed),
        )

    # ---- Traefik ---------------------------------------------------------------------

    async def poll_traefik(self) -> bool:
        """One import pass. Returns whether the store changed.

        The property that matters, and the one ``hosts.bat`` did not have: **an unreachable
        Traefik is not an empty Traefik.** That script treated any exception as "no routers"
        and removed every managed entry, so a momentary HTTP hiccup took all the user's
        domains down for a poll cycle. Here an error leaves the records exactly as they are.
        """
        from core.model import SOURCE_TRAEFIK
        from discover.traefikapi import TraefikUnavailable, import_domains

        try:
            result = await import_domains(self.settings.traefik_api)
        except TraefikUnavailable as exc:
            if self.traefik_ok:
                log.warn(f"traefik: {exc}")
            self.traefik_ok = False
            return False

        if not self.traefik_ok:
            log.write("traefik: reachable again")
        self.traefik_ok = True

        outcome = self.store.reconcile(SOURCE_TRAEFIK, result.domains)
        if not outcome.changed:
            return False

        log.write(
            f"traefik: +{len(outcome.added)} ~{len(outcome.updated)} -{len(outcome.removed)}"
        )
        self.reload()
        self._announce("traefik", outcome)
        return True

    async def _traefik_loop(self) -> None:
        interval = max(1, self.settings.traefik_poll_seconds)
        while not self._stop.is_set():
            try:
                await self.poll_traefik()
            except Exception as exc:  # noqa: BLE001 - a poll must never kill the daemon
                log.error(f"traefik: poll failed: {exc!r}")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), interval)

    # ---- Docker ----------------------------------------------------------------------

    async def poll_docker(self) -> bool:
        """One scan of the running containers. Returns whether the store changed.

        Same rule as Traefik: an unreachable engine is not an empty engine, so a failure
        leaves the records exactly as they are rather than deleting them all.
        """
        from core.model import SOURCE_DOCKER
        from discover.dockerhttp import DockerUnavailable, containers
        from discover.labels import containers_to_domains

        try:
            listing = await containers(host=self.settings.docker_host)
        except DockerUnavailable as exc:
            if self.docker_ok:
                log.warn(f"docker: {exc}")
            self.docker_ok = False
            return False

        if not self.docker_ok:
            log.write("docker: reachable again")
        self.docker_ok = True

        result = containers_to_domains(listing)
        if result.skipped_invalid:
            log.warn(f"docker: skipped unusable names {result.skipped_invalid}")

        outcome = self.store.reconcile(SOURCE_DOCKER, result.domains)
        if not outcome.changed:
            return False

        log.write(
            f"docker: +{len(outcome.added)} ~{len(outcome.updated)} -{len(outcome.removed)}"
        )
        self.reload()
        self._announce("docker", outcome)
        return True

    async def _docker_loop(self) -> None:
        """Scan, then follow the event stream, reconnecting for as long as we are running.

        A full scan runs on **every** reconnection and not only at the start. The ``since``
        window can still miss events across an engine restart, and a domain list that has
        silently drifted is worse than one redundant listing.
        """
        from discover.dockerhttp import BACKOFF_SECONDS, DockerUnavailable, events

        attempt = 0
        while not self._stop.is_set():
            try:
                await self.poll_docker()
                if not self.docker_ok:
                    raise DockerUnavailable("not reachable")

                attempt = 0
                async for _event in events(self._stop, host=self.settings.docker_host):
                    if self._stop.is_set():
                        return
                    await self.poll_docker()
            except DockerUnavailable:
                pass
            except Exception as exc:  # noqa: BLE001 - a poll must never kill the daemon
                log.error(f"docker: {exc!r}")

            if self._stop.is_set():
                return
            delay = BACKOFF_SECONDS[min(attempt, len(BACKOFF_SECONDS) - 1)]
            attempt += 1
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), delay)

    # ---- watching the store ----------------------------------------------------------

    def _store_stamp(self) -> tuple[float, int]:
        try:
            info = self.store.path.stat()
        except OSError:
            return (0.0, 0)
        return (info.st_mtime, info.st_size)

    async def _store_watch_loop(self) -> None:
        """Notice edits made by another process and reload the zone.

        Until the control API lands, the console screen writes to ``domains.json``
        directly, and a running daemon would otherwise keep serving the old zone -- so a
        domain toggled in the console would appear to do nothing. A stat every second is
        far cheaper than the confusion that causes.

        The store is written with an atomic replace, so a reader sees either the whole old
        file or the whole new one; there is no torn read to guard against.
        """
        known = self._store_stamp()
        while not self._stop.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), STORE_POLL_SECONDS)
            if self._stop.is_set():
                return
            current = self._store_stamp()
            if current != known:
                known = current
                try:
                    self.reload()
                except Exception as exc:  # noqa: BLE001 - a bad edit must not kill the daemon
                    log.error(f"runner: reloading the store failed: {exc!r}")

    # ---- the owning process ----------------------------------------------------------

    async def _owner_watch_loop(self) -> None:
        """Stop when the process that started us goes away.

        Through the ordinary stop path rather than by exiting: that is what removes the
        resolution rules and flushes the cache, so closing the application really does put
        the machine back as it was -- including when the window crashes rather than
        quitting politely, which is the case a "quit" menu item cannot cover.
        """
        while not self._stop.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), OWNER_POLL_SECONDS)
            if self._stop.is_set():
                return
            if not netstate.pid_alive(self.owner_pid):
                log.write(f"runner: owner {self.owner_pid} is gone; shutting down")
                self.request_stop()
                return

    # ---- stop ----------------------------------------------------------------------

    def request_stop(self) -> None:
        self._stop.set()

    async def serve_forever(self) -> None:
        loop = asyncio.get_running_loop()
        tasks = [loop.create_task(self._heartbeat()), loop.create_task(self._store_watch_loop())]
        if self.settings.traefik_enabled:
            tasks.append(loop.create_task(self._traefik_loop()))
        if self.owner_pid:
            tasks.append(loop.create_task(self._owner_watch_loop()))
        if self.settings.docker_enabled:
            tasks.append(loop.create_task(self._docker_loop()))
        try:
            await self._stop.wait()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _heartbeat(self) -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_SECONDS)

    async def stop(self) -> None:
        log.remove_sink(self._publish_log)

        if self.api is not None:
            await self.api.stop()
            self.api = None

        if self.server is not None:
            await self.server.stop()
            self.server = None

        if self.applied.namespaces or self.applied.links:
            try:
                self.policy.remove(self.applied)
            except PolicyError as exc:
                log.error(f"runner: could not remove rules on shutdown: {exc}")
            else:
                self.applied = PolicyState()

        # Through the policy rather than dnsflush directly: it is the one seam the runner
        # is given, and reaching around it would make this path untestable.
        self.policy.flush()

        state = netstate.read()
        if state is not None:
            state.clean_shutdown = True
            state.confirmed = []
            netstate.write(state)
        netstate.clear()
        log.write("runner: stopped")


def install_signal_handlers(loop: asyncio.AbstractEventLoop, runner: Runner) -> None:
    """Ask the loop to stop on Ctrl+C or a termination request.

    ``loop.add_signal_handler`` raises NotImplementedError on Windows' Proactor loop, so
    there the classic ``signal.signal`` is used with a thread-safe hand-off. SIGBREAK is
    included because a console Ctrl+Break and the service control manager's shutdown both
    surface there.
    """
    names = [signal.SIGINT, signal.SIGTERM]
    breaksig = getattr(signal, "SIGBREAK", None)
    if breaksig is not None:
        names.append(breaksig)

    if os.name != "nt":
        for name in names:
            with contextlib.suppress(NotImplementedError, ValueError, AttributeError):
                loop.add_signal_handler(name, runner.request_stop)
        return

    def _handler(_signum, _frame) -> None:
        loop.call_soon_threadsafe(runner.request_stop)

    for name in names:
        with contextlib.suppress(ValueError, OSError, AttributeError):
            signal.signal(name, _handler)


async def run_forever(runner: Runner) -> int:
    loop = asyncio.get_running_loop()
    install_signal_handlers(loop, runner)
    try:
        await runner.start()
    except BindError as exc:
        from app.output import emit

        if exc.in_use:
            emit(t("error.bind_in_use", address=exc.address, port=exc.port), error=True)
        elif exc.denied:
            emit(t("error.bind_denied", address=exc.address, port=exc.port), error=True)
        else:
            emit(t("error.bind_failed", address=exc.address, port=exc.port, error=exc.cause),
                 error=True)
        return 3

    try:
        await runner.serve_forever()
    finally:
        await runner.stop()
    return 0
