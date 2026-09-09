"""The daemon lifecycle, and above all its ordering.

The property under test is the one the whole design rests on: **nothing touches the
machine's name-resolution policy until the sockets are bound.** If port 53 is taken, the
process must stop with the machine byte-identical to a moment earlier.
"""

from __future__ import annotations

import asyncio
import socket

import pytest

from core.config import DaemonSettings
from core.model import Domain
from core.store import DomainStore
from daemon import netstate
from daemon.policy import Policy, PolicyError, PolicyState
from daemon.runner import Runner
from resolver.server import BindError

pytestmark = pytest.mark.network


def _free_port() -> int:
    """A port free for **both** protocols, which is what the resolver needs.

    Probing only UDP is not enough and produced a real flake: the two protocols have
    separate port spaces, so the kernel would happily hand back a UDP port whose TCP side
    another still-running test was listening on, and the bind failed there instead.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as datagram:
        datagram.bind(("127.0.0.1", 0))
        port = datagram.getsockname()[1]
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as stream:
            stream.bind(("127.0.0.1", port))
        return port


class RecordingPolicy(Policy):
    """Records what it was asked to do, and when, without touching anything."""

    name = "recording"

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[str] = []
        self.applied: list[tuple[str, ...]] = []
        self.removed: list[tuple[str, ...]] = []
        self.flushes = 0
        self._fail = fail

    def present(self) -> list[str]:
        self.calls.append("present")
        return []

    def apply(self, namespaces, servers) -> PolicyState:  # noqa: ARG002
        self.calls.append("apply")
        if self._fail:
            raise PolicyError("not elevated")
        self.applied.append(tuple(namespaces))
        return PolicyState(namespaces=tuple(namespaces))

    def remove(self, state: PolicyState) -> None:
        self.calls.append("remove")
        self.removed.append(tuple(state.namespaces))

    def remove_all(self) -> None:
        self.calls.append("remove_all")

    def flush(self) -> bool:
        self.calls.append("flush")
        self.flushes += 1
        return True


@pytest.fixture
def settings() -> DaemonSettings:
    return DaemonSettings(
        listen_address="127.0.0.1",
        listen_port=_free_port(),
        listen_ipv6=False,
        upstreams=["192.0.2.1"],
        # Off by default so no test reaches the developer's real Traefik or Docker: a
        # started daemon would otherwise import whatever that machine happens to be
        # running into the test store. The tests that want a source switch it on.
        traefik_enabled=False,
        docker_enabled=False,
    )


@pytest.fixture
def store() -> DomainStore:
    store = DomainStore()
    store.add(Domain(name="shop.test"))
    store.add(Domain(name="*.dobroedelo.ru"))
    return store


def _run(coro):
    return asyncio.run(coro)


# ---- ordering ---------------------------------------------------------------------


def test_the_sockets_are_bound_before_any_rule_is_applied(settings, store, monkeypatch):
    """A bind failure must leave the machine untouched, which only holds in this order."""
    order: list[str] = []

    class OrderingPolicy(RecordingPolicy):
        def apply(self, namespaces, servers):
            order.append("apply")
            return super().apply(namespaces, servers)

    policy = OrderingPolicy()
    runner = Runner(settings=settings, store=store, policy=policy)

    import daemon.runner as runner_module

    real_bind = runner_module.bind_sockets

    def _spy(addresses, port):
        order.append("bind")
        return real_bind(addresses, port)

    # Patched in the runner's own namespace: it imported the name directly, so patching
    # resolver.server would have no effect and the test would silently prove nothing.
    monkeypatch.setattr(runner_module, "bind_sockets", _spy)

    async def scenario():
        await runner.start()
        await runner.stop()

    _run(scenario())
    assert order == ["bind", "apply"]


def test_a_taken_port_stops_the_run_without_touching_policy(settings, store):
    policy = RecordingPolicy()
    port = settings.listen_port

    blocker_udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    blocker_udp.bind(("127.0.0.1", port))
    try:
        runner = Runner(settings=settings, store=store, policy=policy)
        with pytest.raises(BindError):
            _run(runner.start())

        assert "apply" not in policy.calls, "the machine must be untouched after a bind failure"
        assert netstate.read() is None, "no state may be recorded for a run that never started"
    finally:
        blocker_udp.close()


def test_netstate_records_the_intent_before_it_is_confirmed(settings, store):
    policy = RecordingPolicy()
    runner = Runner(settings=settings, store=store, policy=policy)

    async def scenario():
        await runner.start()
        state = netstate.read()
        assert state is not None
        # Intent is a superset of confirmed, always: a crash mid-apply must leave nothing
        # orphaned that repair cannot find.
        assert set(state.confirmed) <= set(state.intent)
        assert sorted(state.intent) == [".dobroedelo.ru", "shop.test"]
        await runner.stop()

    _run(scenario())


# ---- policy failure is not fatal --------------------------------------------------


def test_the_resolver_still_runs_when_the_policy_cannot_be_applied(settings, store):
    """This is what an unelevated --daemon-foreground looks like, and it must still serve."""
    policy = RecordingPolicy(fail=True)
    runner = Runner(settings=settings, store=store, policy=policy)

    async def scenario():
        await runner.start()
        assert runner.server is not None
        assert runner.resolver is not None
        assert runner.policy_error == "not elevated"
        assert runner.applied.namespaces == ()
        await runner.stop()

    _run(scenario())


# ---- shutdown ---------------------------------------------------------------------


def test_shutdown_removes_exactly_what_was_applied(settings, store):
    policy = RecordingPolicy()
    runner = Runner(settings=settings, store=store, policy=policy)

    async def scenario():
        await runner.start()
        await runner.stop()

    _run(scenario())
    assert policy.applied == [(".dobroedelo.ru", "shop.test")]
    assert policy.removed == [(".dobroedelo.ru", "shop.test")]


def test_a_clean_shutdown_leaves_no_state_file(settings, store):
    runner = Runner(settings=settings, store=store, policy=RecordingPolicy())

    async def scenario():
        await runner.start()
        await runner.stop()

    _run(scenario())
    assert netstate.read() is None


def test_an_unclean_previous_run_is_cleaned_up_before_anything_is_applied(settings, store):
    """The failure mode this guards against is a rule pointing at a socket nobody holds."""
    stale = netstate.NetState.start(
        mechanism="recording", listen=["127.0.0.1"], port=53, upstreams=[]
    )
    stale.pid = 999_999  # not a live process
    stale.confirmed = ["ghost.test"]
    stale.clean_shutdown = False
    netstate.write(stale)

    policy = RecordingPolicy()
    runner = Runner(settings=settings, store=store, policy=policy)

    async def scenario():
        await runner.start()
        await runner.stop()

    _run(scenario())
    assert "remove_all" in policy.calls
    assert policy.calls.index("remove_all") < policy.calls.index("apply")


def test_a_live_previous_daemon_is_left_alone(settings, store):
    """Two daemons is a mistake, but the second must not tear down the first's rules."""
    import os

    live = netstate.NetState.start(
        mechanism="recording", listen=["127.0.0.1"], port=53, upstreams=[]
    )
    live.pid = os.getppid() or os.getpid()
    live.confirmed = ["other.test"]
    netstate.write(live)

    policy = RecordingPolicy()
    runner = Runner(settings=settings, store=store, policy=policy)
    runner.clear_leftovers()

    assert "remove_all" not in policy.calls


# ---- upstreams --------------------------------------------------------------------


def test_our_own_address_is_never_used_as_an_upstream(store):
    """Forwarding to ourselves is an instant loop that takes name resolution with it."""
    settings = DaemonSettings(
        listen_address="127.0.0.1", listen_port=_free_port(), upstreams=["127.0.0.1", "::1"]
    )
    runner = Runner(settings=settings, store=store, policy=RecordingPolicy())
    captured = runner.capture_upstreams()

    assert "127.0.0.1" not in captured
    assert captured == ["1.1.1.1", "8.8.8.8"], "falls back rather than looping"


def test_configured_upstreams_win_over_capture(store):
    settings = DaemonSettings(listen_port=_free_port(), upstreams=["192.0.2.53"])
    runner = Runner(settings=settings, store=store, policy=RecordingPolicy())
    # No subprocess is allowed by the autouse guard, so reaching capture() would fail here.
    assert runner.capture_upstreams() == ["192.0.2.53"]


# ---- reload -----------------------------------------------------------------------


def test_reload_reclaims_only_the_difference(settings, store):
    policy = RecordingPolicy()
    runner = Runner(settings=settings, store=store, policy=policy)

    async def scenario():
        await runner.start()
        before = len(policy.applied)

        store.add(Domain(name="new.test"))
        runner.reload()

        assert len(policy.applied) == before + 1
        assert "new.test" in policy.applied[-1]
        await runner.stop()

    _run(scenario())


def test_reload_without_a_namespace_change_does_not_touch_policy(settings, store):
    policy = RecordingPolicy()
    runner = Runner(settings=settings, store=store, policy=policy)

    async def scenario():
        await runner.start()
        before = len(policy.applied)

        # A note is not a namespace, so nothing should be re-registered.
        target = store.load().domains[0]
        store.update(target.id, note="just a note")
        runner.reload()

        assert len(policy.applied) == before
        await runner.stop()

    _run(scenario())


# ---- Traefik import ---------------------------------------------------------------


def test_an_unreachable_traefik_never_empties_the_store(settings, store, monkeypatch):
    """The flaw in hosts.bat, as a regression test.

    That script treated any exception from the API as "no routers" and deleted every entry
    it managed, so one HTTP hiccup took the user's domains down for a poll cycle.
    """
    from core.model import SOURCE_TRAEFIK, Domain
    from discover import traefikapi

    store.reconcile(SOURCE_TRAEFIK, [Domain(name="etm39.ru", source=SOURCE_TRAEFIK, owner="r1")])
    before = [d.name for d in store.load().domains]

    async def _explode(*_args, **_kwargs):
        raise traefikapi.TraefikUnavailable("connection refused")

    monkeypatch.setattr(traefikapi, "import_domains", _explode)
    runner = Runner(settings=settings, store=store, policy=RecordingPolicy())

    assert _run(runner.poll_traefik()) is False
    assert [d.name for d in store.load().domains] == before
    assert runner.traefik_ok is False


def test_a_successful_poll_imports_routers(settings, store, monkeypatch):
    from discover import traefikapi
    from discover.traefikapi import routers_to_domains

    routers = [
        {"name": "web@docker", "rule": "Host(`etm39.ru`)", "status": "enabled"},
        {"name": "api@docker", "rule": "Host(`api.dobroedelo.ru`)", "status": "enabled"},
    ]

    async def _ok(*_args, **_kwargs):
        return routers_to_domains(routers)

    monkeypatch.setattr(traefikapi, "import_domains", _ok)
    runner = Runner(settings=settings, store=store, policy=RecordingPolicy())

    assert _run(runner.poll_traefik()) is True
    assert sorted(d.name for d in store.load().domains) == [
        "*.dobroedelo.ru",
        "api.dobroedelo.ru",
        "etm39.ru",
        "shop.test",
    ]


def test_an_unchanged_poll_does_not_rewrite_anything(settings, store, monkeypatch):
    """A ten-second poll must be free when nothing moved."""
    from discover import traefikapi
    from discover.traefikapi import routers_to_domains

    routers = [{"name": "web@docker", "rule": "Host(`etm39.ru`)", "status": "enabled"}]

    async def _ok(*_args, **_kwargs):
        return routers_to_domains(routers)

    monkeypatch.setattr(traefikapi, "import_domains", _ok)
    runner = Runner(settings=settings, store=store, policy=RecordingPolicy())

    assert _run(runner.poll_traefik()) is True
    assert _run(runner.poll_traefik()) is False


# ---- Docker import -----------------------------------------------------------------


def _container(name: str, host: str):
    return {
        "Id": f"{name}0123456789ab",
        "Names": [f"/{name}"],
        "Image": "nginx",
        "State": "running",
        "Labels": {"hostbridge.enable": "true", "hostbridge.domain": host},
    }


def test_an_unreachable_docker_never_empties_the_store(settings, store, monkeypatch):
    """The same property proven for Traefik, and for the same reason.

    Docker Desktop restarting is ordinary. If a failed listing were read as "no containers",
    every discovered domain would disappear until the engine came back.
    """
    from core.model import SOURCE_DOCKER, Domain
    from discover import dockerhttp

    store.reconcile(
        SOURCE_DOCKER, [Domain(name="web.test", source=SOURCE_DOCKER, owner="host:web.test")]
    )
    before = [d.name for d in store.load().domains]

    async def _explode(*_args, **_kwargs):
        raise dockerhttp.DockerUnavailable("the engine is not running")

    monkeypatch.setattr(dockerhttp, "containers", _explode)
    runner = Runner(settings=settings, store=store, policy=RecordingPolicy())

    assert _run(runner.poll_docker()) is False
    assert [d.name for d in store.load().domains] == before
    assert runner.docker_ok is False


def test_a_successful_scan_imports_labelled_containers(settings, store, monkeypatch):
    from discover import dockerhttp

    async def _ok(*_args, **_kwargs):
        return [_container("web", "web.test"), _container("api", "api.test")]

    monkeypatch.setattr(dockerhttp, "containers", _ok)
    runner = Runner(settings=settings, store=store, policy=RecordingPolicy())

    assert _run(runner.poll_docker()) is True
    assert sorted(d.name for d in store.load().domains) == [
        "*.dobroedelo.ru",
        "api.test",
        "shop.test",
        "web.test",
    ]


def test_an_unchanged_scan_does_not_rewrite_anything(settings, store, monkeypatch):
    """The event stream fires a scan per container during a compose up; an idle one is free."""
    from discover import dockerhttp

    async def _ok(*_args, **_kwargs):
        return [_container("web", "web.test")]

    monkeypatch.setattr(dockerhttp, "containers", _ok)
    runner = Runner(settings=settings, store=store, policy=RecordingPolicy())

    assert _run(runner.poll_docker()) is True
    assert _run(runner.poll_docker()) is False


def test_a_stopped_container_takes_its_domain_with_it(settings, store, monkeypatch):
    from discover import dockerhttp

    listing = [_container("web", "web.test"), _container("api", "api.test")]

    async def _ok(*_args, **_kwargs):
        return listing

    monkeypatch.setattr(dockerhttp, "containers", _ok)
    runner = Runner(settings=settings, store=store, policy=RecordingPolicy())
    assert _run(runner.poll_docker()) is True

    listing.pop()
    assert _run(runner.poll_docker()) is True
    assert "api.test" not in [d.name for d in store.load().domains]


def test_docker_does_not_touch_what_traefik_owns(settings, store, monkeypatch):
    """The anti-clobber rule, across two live sources rather than one.

    Both importers run in the same daemon against the same file, and each pass must confine
    itself to records carrying its own source.
    """
    from core.model import SOURCE_TRAEFIK, Domain
    from discover import dockerhttp

    store.reconcile(
        SOURCE_TRAEFIK, [Domain(name="etm39.ru", source=SOURCE_TRAEFIK, owner="host:etm39.ru")]
    )

    async def _ok(*_args, **_kwargs):
        return [_container("web", "web.test")]

    monkeypatch.setattr(dockerhttp, "containers", _ok)
    runner = Runner(settings=settings, store=store, policy=RecordingPolicy())
    _run(runner.poll_docker())

    kept = next(d for d in store.load().domains if d.name == "etm39.ru")
    assert kept.source == SOURCE_TRAEFIK


def test_the_docker_loop_is_only_started_when_it_is_wanted(settings, store, monkeypatch):
    """A machine that has switched Docker off should not open a connection per backoff."""
    from discover import dockerhttp

    calls = []

    async def _count(*_args, **_kwargs):
        calls.append(1)
        raise dockerhttp.DockerUnavailable("no")

    monkeypatch.setattr(dockerhttp, "containers", _count)

    async def scenario():
        runner = Runner(
            settings=settings, store=store, policy=RecordingPolicy(), control_enabled=False
        )
        await runner.start()
        task = asyncio.get_running_loop().create_task(runner.serve_forever())
        await asyncio.sleep(0.1)
        runner.request_stop()
        await task
        await runner.stop()

    _run(scenario())
    assert calls == []


# ---- filesystem permissions --------------------------------------------------------


def test_the_state_directory_is_hardened_once_and_not_per_write(
    settings, store, monkeypatch, no_acl
):
    """Regression, and one only a Windows runner could see.

    Tightening an ACL is a ``chmod`` on POSIX and an ``icacls`` *process* on Windows, so a
    daemon that hardened on every store write would spawn one per Docker event. It hardens
    the directory once at start instead; the store writes that follow must add nothing.
    """
    from core.paths import machine_home
    from system import secure

    monkeypatch.setattr(secure, "IS_WINDOWS", True)
    # _current_user() reads the environment and raises PermissionWarning when it finds
    # nothing, which the runner suppresses -- so without this the test would pass on a
    # container by never reaching the call it is about.
    monkeypatch.setenv("USERNAME", "dev")
    monkeypatch.delenv("USERDOMAIN", raising=False)
    runner = Runner(
        settings=settings, store=store, policy=RecordingPolicy(), control_enabled=False
    )

    async def scenario():
        await runner.start()
        store.add(Domain(name="written.test"))
        runner.reload()
        store.add(Domain(name="written-again.test"))
        runner.reload()
        await runner.stop()

    _run(scenario())

    hardened = [call for call in no_acl if call[1] == str(machine_home())]
    assert len(hardened) == 1, f"the state directory was hardened {len(hardened)} times"
    assert no_acl == hardened, f"something else was hardened as well: {no_acl}"


def test_the_token_file_is_hardened_because_it_is_the_one_real_secret(
    settings, store, monkeypatch, no_acl
):
    """``daemon.json`` carries the control-API bearer token.

    Anything that can read it can add a resolution rule, so this one file is hardened
    individually despite the cost -- and that must not be lost to an optimisation aimed at
    the store.
    """
    from core.paths import daemon_file
    from system import secure

    monkeypatch.setattr(secure, "IS_WINDOWS", True)
    monkeypatch.setenv("USERNAME", "dev")
    monkeypatch.delenv("USERDOMAIN", raising=False)
    runner = Runner(settings=settings, store=store, policy=RecordingPolicy())

    async def scenario():
        await runner.start()
        await runner.stop()

    _run(scenario())
    assert any(call[1] == str(daemon_file()) for call in no_acl), no_acl


# ---- reaching the resolver at all --------------------------------------------------


def test_a_filtered_loopback_adds_the_address_the_filter_permits(settings, store, monkeypatch):
    """The case that made everything look right while nothing resolved.

    A VPN with DNS-leak protection blocks port 53 for every destination, loopback included,
    and permits its own tunnel interface. Listening on the tunnel's local address as well
    is what lets a query arrive at all -- and loopback stays first, so the pair works
    whether the tunnel is up or not.
    """
    from system import reachability

    monkeypatch.setattr(
        reachability, "filtered", lambda address, _port, **_k: address == "127.0.0.1"
    )
    monkeypatch.setattr(reachability, "outbound_address", lambda: "172.18.0.1")

    runner = Runner(settings=settings, store=store, policy=RecordingPolicy())
    assert runner.detect_filter_bypass() == ["172.18.0.1"]
    runner.bypass_listen = ["172.18.0.1"]

    assert runner.listen_addresses()[0] == "127.0.0.1", "loopback must stay the first server"
    assert "172.18.0.1" in runner.listen_addresses()
    assert runner.filtered_primary is True


def test_an_unfiltered_machine_binds_nothing_extra(settings, store, monkeypatch):
    """The ordinary case: no VPN, no workaround, no extra listener facing anywhere."""
    from system import reachability

    monkeypatch.setattr(reachability, "filtered", lambda *_a, **_k: False)
    runner = Runner(settings=settings, store=store, policy=RecordingPolicy())

    assert runner.detect_filter_bypass() == []
    assert runner.filtered_primary is False
    assert runner.listen_addresses() == ["127.0.0.1"]


def test_the_workaround_can_be_switched_off(settings, store, monkeypatch):
    """The extra address is not loopback, so refusing it outright has to be possible."""
    from system import reachability

    monkeypatch.setattr(
        reachability, "filtered", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("probed"))
    )
    settings.bypass_dns_filter = False
    runner = Runner(settings=settings, store=store, policy=RecordingPolicy())
    assert runner.detect_filter_bypass() == []


def test_the_extra_address_never_becomes_its_own_upstream(settings, store, monkeypatch):
    """Otherwise the resolver would forward to itself and every query would hang."""
    from system import reachability

    monkeypatch.setattr(
        reachability, "filtered", lambda address, _port, **_k: address == "127.0.0.1"
    )
    monkeypatch.setattr(reachability, "outbound_address", lambda: "172.18.0.1")

    settings.upstreams = ["172.18.0.1", "8.8.8.8"]
    runner = Runner(settings=settings, store=store, policy=RecordingPolicy())

    # Exercised without binding: the tunnel address does not exist on a build machine, and
    # what is under test is the ordering -- the bypass address must be known before the
    # upstreams are sanitised, or the resolver would list itself as its own upstream.
    runner.bypass_listen = runner.detect_filter_bypass()
    assert runner.bypass_listen == ["172.18.0.1"]
    assert runner.capture_upstreams() == ["8.8.8.8"]


# ---- belonging to the application --------------------------------------------------


def test_the_daemon_shuts_itself_down_when_its_owner_is_gone(settings, store, monkeypatch):
    """Closing the application must put the machine back as it was.

    Through the ordinary stop path, so the rules are removed and the cache flushed -- and
    it covers the window being killed rather than quitting politely, which no menu item
    can.
    """
    import daemon.runner as runner_module

    policy = RecordingPolicy()
    runner = Runner(
        settings=settings, store=store, policy=policy, control_enabled=False, owner_pid=4242
    )
    monkeypatch.setattr(runner_module, "OWNER_POLL_SECONDS", 0.05)
    monkeypatch.setattr(runner_module.netstate, "pid_alive", lambda pid: pid != 4242)

    async def scenario():
        await runner.start()
        await asyncio.wait_for(runner.serve_forever(), 5)
        await runner.stop()

    _run(scenario())
    assert policy.removed == [(".dobroedelo.ru", "shop.test")], "rules must be taken back down"
    assert netstate.read() is None


def test_a_daemon_with_no_owner_keeps_running(settings, store, monkeypatch):
    """A service belongs to the machine and must outlive every window."""
    import daemon.runner as runner_module

    monkeypatch.setattr(runner_module, "OWNER_POLL_SECONDS", 0.05)
    monkeypatch.setattr(runner_module.netstate, "pid_alive", lambda _pid: False)
    runner = Runner(settings=settings, store=store, policy=RecordingPolicy(), control_enabled=False)

    async def scenario():
        await runner.start()
        task = asyncio.get_running_loop().create_task(runner.serve_forever())
        await asyncio.sleep(0.3)
        still_running = not task.done()
        runner.request_stop()
        await task
        await runner.stop()
        return still_running

    assert _run(scenario()) is True


def test_a_living_owner_does_not_stop_anything(settings, store, monkeypatch):
    import daemon.runner as runner_module

    monkeypatch.setattr(runner_module, "OWNER_POLL_SECONDS", 0.05)
    monkeypatch.setattr(runner_module.netstate, "pid_alive", lambda _pid: True)
    runner = Runner(
        settings=settings, store=store, policy=RecordingPolicy(), control_enabled=False,
        owner_pid=4242,
    )

    async def scenario():
        await runner.start()
        task = asyncio.get_running_loop().create_task(runner.serve_forever())
        await asyncio.sleep(0.3)
        still_running = not task.done()
        runner.request_stop()
        await task
        await runner.stop()
        return still_running

    assert _run(scenario()) is True


# ---- the log view's source ---------------------------------------------------------


def test_log_lines_reach_the_event_stream(settings, store):
    """The window's log view had no source at all.

    Nothing published ``kind="log"``, so it showed the interface's own start-up messages
    and then sat there looking frozen while the daemon worked perfectly. This is the wire
    that feeds it.
    """
    from core import log as log_module

    runner = Runner(settings=settings, store=store, policy=RecordingPolicy())

    async def scenario():
        await runner.start()
        log_module.warn("something worth seeing")
        seen = [e.to_dict() for e in runner.bus.history() if e.kind == "log"]
        await runner.stop()
        return seen

    seen = _run(scenario())
    assert any(e["message"] == "something worth seeing" and e["level"] == "warn" for e in seen)


def test_the_sink_is_detached_on_shutdown(settings, store):
    """A daemon that stopped must not keep a dead bus wired into the process-wide logger."""
    from core import log as log_module

    runner = Runner(settings=settings, store=store, policy=RecordingPolicy())

    async def scenario():
        await runner.start()
        await runner.stop()

    _run(scenario())
    before = len(tuple(runner.bus.history()))
    log_module.write("after the daemon is gone")
    assert len(tuple(runner.bus.history())) == before


def test_a_sink_that_throws_does_not_lose_the_line(tmp_path):
    """Logging never raises: a daemon dying over a lost diagnostic line is the worse bug."""
    from core import log as log_module

    def _bad(_level, _message):
        raise RuntimeError("subscriber is broken")

    log_module.add_sink(_bad)
    try:
        log_module.write("still written")
    finally:
        log_module.remove_sink(_bad)

    from core.paths import log_file

    assert "still written" in log_file().read_text(encoding="utf-8")
