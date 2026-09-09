"""The Docker client against a real socket that answers like the engine does.

A fake engine rather than the real one, because CI has no Docker and the point here is the
wire, not the daemon: chunked bodies, a stream that speaks only when something happens, and
a peer that goes away mid-stream. All three are shapes the real engine produces and all
three broke a client of ours before -- the Traefik importer read a chunked body raw and got
the hex length glued to the front of the JSON.

Written as plain sync tests driving ``asyncio.run``, matching ``test_resolver.py``: it reads
the way the daemon actually starts and saves a dependency on pytest-asyncio.
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest

from discover import dockerhttp

pytestmark = pytest.mark.skipif(
    os.name == "nt", reason="unix sockets; the named pipe is exercised on the desktop"
)

CONTAINERS = [
    {
        "Id": "abc123def4567890",
        "Names": ["/shop"],
        "Image": "nginx",
        "State": "running",
        "Status": "Up 3 minutes",
        "Labels": {"hostbridge.enable": "true", "hostbridge.domain": "shop.test"},
    }
]


class FakeEngine:
    """Answers one request per connection, the way Docker does with ``Connection: close``."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.server: asyncio.AbstractServer | None = None
        self.requests: list[str] = []
        self.mode = "json"
        self.handlers: set[asyncio.Task] = set()

    async def start(self) -> None:
        self.server = await asyncio.start_unix_server(self._handle, path=self.path)

    async def stop(self) -> None:
        # Handlers are cancelled before the server is closed: since 3.12 ``wait_closed``
        # waits for every connection to finish, and a deliberately silent one never does.
        for task in tuple(self.handlers):
            task.cancel()
        if self.handlers:
            await asyncio.gather(*self.handlers, return_exceptions=True)
        self.handlers.clear()
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
            self.server = None

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self.handlers.add(task)
            task.add_done_callback(self.handlers.discard)

        line = await reader.readline()
        self.requests.append(line.decode("latin-1").strip())
        while True:
            header = await reader.readline()
            if header in (b"\r\n", b"\n", b""):
                break

        if self.mode == "chunked":
            body = json.dumps(CONTAINERS).encode("utf-8")
            writer.write(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: application/json\r\n"
                b"Transfer-Encoding: chunked\r\n\r\n"
            )
            # Split so the reassembly is actually exercised rather than trivially satisfied.
            half = len(body) // 2
            for piece in (body[:half], body[half:]):
                writer.write(f"{len(piece):x}\r\n".encode("ascii") + piece + b"\r\n")
            writer.write(b"0\r\n\r\n")
        elif self.mode == "stream":
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n")
            await writer.drain()
            await asyncio.sleep(0.2)
            for identifier in ("one", "two"):
                writer.write(json.dumps({"Action": "start", "id": identifier}).encode() + b"\n")
                await writer.drain()
                await asyncio.sleep(0.05)
            # Then go away, as a restarting engine does.
        elif self.mode == "quiet":
            # Headers, then silence -- what the stream looks like almost all of the time.
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n")
            await writer.drain()
            await asyncio.sleep(30)
        elif self.mode == "error":
            writer.write(b"HTTP/1.1 500 Server Error\r\nContent-Length: 2\r\n\r\n{}")
        else:
            body = json.dumps(CONTAINERS).encode("utf-8")
            writer.write(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: application/json\r\n"
                + f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
                + body
            )

        await writer.drain()
        writer.close()


@pytest.fixture
def engine(tmp_path, monkeypatch):
    # A short path: the sun_path field caps at ~104 bytes and a long tmp_path overflows it.
    path = str(tmp_path / "d.sock")
    fake = FakeEngine(path)
    monkeypatch.setenv("DOCKER_HOST", f"unix://{path}")
    return fake


def _with(fake: FakeEngine, coroutine):
    async def main():
        await fake.start()
        try:
            return await coroutine()
        finally:
            await fake.stop()

    return asyncio.run(main())


# ---- listing -------------------------------------------------------------------------


def test_a_listing_comes_back_parsed(engine):
    found = _with(engine, lambda: dockerhttp.containers(timeout=2.0))
    assert [c["Names"] for c in found] == [["/shop"]]


def test_the_request_carries_the_pinned_version(engine):
    _with(engine, lambda: dockerhttp.containers(timeout=2.0))
    assert engine.requests == [f"GET /{dockerhttp.API_VERSION}/containers/json HTTP/1.1"]


def test_a_chunked_body_is_reassembled(engine):
    """The failure that only showed against a live service: a body read raw arrives with the
    hex chunk length glued to the front of the JSON."""
    engine.mode = "chunked"
    found = _with(engine, lambda: dockerhttp.containers(timeout=2.0))
    assert found[0]["Id"] == "abc123def4567890"


def test_an_http_error_is_reported_as_unavailable(engine):
    engine.mode = "error"
    with pytest.raises(dockerhttp.DockerUnavailable):
        _with(engine, lambda: dockerhttp.containers(timeout=2.0))


def test_ping_says_yes_when_something_answers(engine):
    assert _with(engine, lambda: dockerhttp.ping(timeout=2.0)) is True


# ---- the event stream ------------------------------------------------------------------


def test_events_arrive_one_at_a_time(engine):
    engine.mode = "stream"

    async def read():
        stop = asyncio.Event()
        found = []
        async for event in dockerhttp.events(stop, host=""):
            found.append(event)
        return found

    found = _with(engine, read)
    assert [event["id"] for event in found] == ["one", "two"]


def test_a_quiet_stream_stops_when_asked(engine):
    """The stream is open indefinitely and silent most of the time.

    So the read timeout bounds how quickly a stopped daemon notices, not how long the engine
    may stay quiet -- without it, shutdown would wait for the next container event, which
    might be tomorrow.
    """
    engine.mode = "quiet"

    async def read():
        stop = asyncio.Event()
        found = []

        async def ask():
            await asyncio.sleep(0.05)
            stop.set()

        asyncio.get_running_loop().create_task(ask())
        started = asyncio.get_running_loop().time()
        async for event in dockerhttp.events(stop, host=""):
            found.append(event)
        return found, asyncio.get_running_loop().time() - started

    found, elapsed = _with(engine, read)
    assert found == []
    assert elapsed < dockerhttp.STREAM_POLL_SECONDS * 3, "shutdown waited on a silent engine"


def test_a_peer_that_goes_away_ends_the_stream_rather_than_raising(engine):
    """A Docker Desktop restart is ordinary; the caller reconnects with backoff."""
    engine.mode = "stream"

    async def read():
        stop = asyncio.Event()
        return [event async for event in dockerhttp.events(stop, host="")]

    assert len(_with(engine, read)) == 2
