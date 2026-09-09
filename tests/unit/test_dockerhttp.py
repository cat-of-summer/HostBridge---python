r"""The Docker client's addressing and request shaping, without an engine.

The parts worth pinning here are the ones a machine without Docker still gets wrong: which
address is chosen, and what the event request asks for. The transport itself is exercised
against a real socket in ``tests/integration/test_docker_client.py``.
"""

from __future__ import annotations

import asyncio
import json
from urllib.parse import parse_qs, urlsplit

import pytest

from discover import dockerhttp

# ---- where the engine is ------------------------------------------------------------


def test_the_setting_wins_over_the_environment(monkeypatch):
    """The daemon may run as a service under an account whose environment nobody has seen,
    while config.json is what the user can actually edit."""
    monkeypatch.setenv("DOCKER_HOST", "tcp://10.0.0.1:2375")
    assert dockerhttp.endpoint("unix:///run/user/docker.sock") == "unix:///run/user/docker.sock"


def test_the_environment_wins_over_the_platform_default(monkeypatch):
    monkeypatch.setenv("DOCKER_HOST", "tcp://10.0.0.1:2375")
    assert dockerhttp.endpoint() == "tcp://10.0.0.1:2375"


def test_a_blank_setting_is_not_an_address(monkeypatch):
    monkeypatch.setenv("DOCKER_HOST", "tcp://10.0.0.1:2375")
    assert dockerhttp.endpoint("   ") == "tcp://10.0.0.1:2375"


def test_the_platform_default_is_the_socket_or_the_pipe(monkeypatch):
    monkeypatch.delenv("DOCKER_HOST", raising=False)

    monkeypatch.setattr(dockerhttp.os, "name", "posix")
    assert dockerhttp.endpoint() == f"unix://{dockerhttp.UNIX_SOCKET}"

    monkeypatch.setattr(dockerhttp.os, "name", "nt")
    assert dockerhttp.endpoint() == dockerhttp.WINDOWS_PIPE


# ---- the event request --------------------------------------------------------------


def test_the_event_stream_asks_only_for_container_events():
    """A busy machine emits image, network and volume events by the hundred; none of them
    can change a domain, and every one we ask for is a full container listing."""
    query = parse_qs(urlsplit(dockerhttp._events_path(None)).query)
    filters = json.loads(query["filters"][0])
    assert filters["type"] == ["container"]
    assert "start" in filters["event"] and "die" in filters["event"]


def test_the_since_window_is_a_whole_number_of_seconds():
    path = dockerhttp._events_path(1757000000.75)
    assert "since=1757000001" in path or "since=1757000000" in path
    assert "." not in urlsplit(path).query.split("since=")[1]


def test_every_path_carries_the_pinned_api_version():
    assert dockerhttp._events_path(None).startswith(f"/{dockerhttp.API_VERSION}/events")


# ---- failure is not fatal -----------------------------------------------------------


def test_an_unreachable_engine_raises_rather_than_hanging(monkeypatch):
    monkeypatch.setenv("DOCKER_HOST", "unix:///nonexistent/hostbridge-test.sock")
    with pytest.raises(dockerhttp.DockerUnavailable):
        asyncio.run(dockerhttp.containers(timeout=0.5))


def test_ping_answers_false_instead_of_raising(monkeypatch):
    """A developer machine without Docker is ordinary, so the question has an answer."""
    monkeypatch.setenv("DOCKER_HOST", "unix:///nonexistent/hostbridge-test.sock")
    assert asyncio.run(dockerhttp.ping(timeout=0.5)) is False
