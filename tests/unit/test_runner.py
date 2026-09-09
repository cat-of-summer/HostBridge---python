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
