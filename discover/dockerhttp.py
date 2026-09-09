r"""Talking to the Docker engine over whichever transport this machine offers.

Raw HTTP over the socket rather than the ``docker`` SDK. The SDK is synchronous -- its
``events()`` is a blocking generator that would need a thread and a queue to reach the
daemon's single event loop -- and it drags in ``requests``, ``urllib3`` and pywin32 for the
named pipe. We need four endpoints, and the transport is three lines per platform:

* Linux and macOS: ``create_unix_connection`` on ``/var/run/docker.sock``
* Windows: ``create_pipe_connection`` on ``\\.\pipe\docker_engine``. asyncio speaks named
  pipes natively on the Proactor loop, which is precisely why pywin32 is not needed here.
* ``DOCKER_HOST=tcp://…``: an ordinary ``open_connection``.

Docker being absent is never an error. A developer machine without it is ordinary, and the
resolver has nothing to do with containers.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import quote, urlsplit

from core import log
from discover.httpjson import HttpError, build_request, read_body, read_head

UNIX_SOCKET = "/var/run/docker.sock"
WINDOWS_PIPE = r"\\.\pipe\docker_engine"

#: Docker's own API version negotiation. Pinned low enough that any engine from the last
#: several years answers, and high enough for the filters we use.
API_VERSION = "v1.41"

DEFAULT_TIMEOUT = 5.0

#: The event stream is open indefinitely and speaks only when something happens, so a read
#: timeout here bounds how quickly a cancelled poll notices, not how long it may be quiet.
STREAM_POLL_SECONDS = 1.0

#: The daemon reconnects with a widening gap. Capped low: a Docker Desktop restart is
#: ordinary and the domains should come back within seconds of it returning.
BACKOFF_SECONDS = (1.0, 2.0, 4.0, 8.0, 16.0, 30.0)


class DockerUnavailable(Exception):
    """The engine is not reachable. Never fatal."""


def endpoint(host: str = "") -> str:
    """Where the engine is: the setting, then ``DOCKER_HOST``, then the platform default.

    The configured value wins over the environment because the daemon may run as a service
    under an account whose environment nobody has ever seen, while ``config.json`` is what
    the user can actually edit.
    """
    if host.strip():
        return host.strip()
    configured = os.environ.get("DOCKER_HOST", "").strip()
    if configured:
        return configured
    return WINDOWS_PIPE if os.name == "nt" else f"unix://{UNIX_SOCKET}"


async def _connect(target: str, timeout: float):
    """Open a connection to the engine, whichever kind of address this is."""
    loop = asyncio.get_running_loop()

    if target.startswith("tcp://") or target.startswith("http://"):
        parts = urlsplit(target if "//" in target else f"tcp://{target}")
        host, port = parts.hostname or "127.0.0.1", parts.port or 2375
        return await asyncio.wait_for(asyncio.open_connection(host, port), timeout)

    if target.startswith("npipe://") or target.startswith(r"\\"):
        path = target.removeprefix("npipe://").replace("/", "\\")
        # create_pipe_connection returns (transport, protocol), not a reader/writer pair,
        # so the streams have to be assembled by hand.
        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        transport, _ = await loop.create_pipe_connection(lambda: protocol, path)
        writer = asyncio.StreamWriter(transport, protocol, reader, loop)
        return reader, writer

    path = target.removeprefix("unix://")
    return await asyncio.wait_for(asyncio.open_unix_connection(path), timeout)


async def _open(path: str, timeout: float, host: str = ""):
    target = endpoint(host)
    try:
        reader, writer = await _connect(target, timeout)
    except (TimeoutError, OSError, NotImplementedError, AttributeError) as exc:
        raise DockerUnavailable(f"{target}: {exc}") from exc

    # The Host header is ignored over a socket or a pipe, but HTTP/1.1 requires one.
    writer.write(build_request("GET", path, "docker"))
    await writer.drain()
    return reader, writer


async def get_json(path: str, *, timeout: float = DEFAULT_TIMEOUT, host: str = "") -> Any:
    """GET one endpoint and parse the answer."""
    reader, writer = await _open(f"/{API_VERSION}{path}", timeout, host)
    try:
        status, headers = await read_head(reader, timeout)
        body = await read_body(reader, headers, timeout)
    except (HttpError, TimeoutError, OSError, asyncio.IncompleteReadError) as exc:
        raise DockerUnavailable(str(exc)) from exc
    finally:
        writer.close()
        with contextlib.suppress(OSError, asyncio.CancelledError):
            await writer.wait_closed()

    if status != 200:
        raise DockerUnavailable(f"{path} returned HTTP {status}")
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise DockerUnavailable(f"{path} did not return JSON: {exc}") from exc


async def containers(*, timeout: float = DEFAULT_TIMEOUT, host: str = "") -> list[Any]:
    """Every running container, with its labels."""
    found = await get_json("/containers/json", timeout=timeout, host=host)
    return found if isinstance(found, list) else []


async def ping(*, timeout: float = DEFAULT_TIMEOUT, host: str = "") -> bool:
    try:
        await get_json("/version", timeout=timeout, host=host)
    except DockerUnavailable:
        return False
    return True


def _events_path(since: float | None) -> str:
    filters = quote(
        json.dumps(
            {
                "type": ["container"],
                "event": ["start", "die", "destroy", "health_status", "update"],
            }
        )
    )
    path = f"/{API_VERSION}/events?filters={filters}"
    if since is not None:
        path += f"&since={since:.0f}"
    return path


async def events(
    stop: asyncio.Event, since: float | None = None, *, host: str = ""
) -> AsyncIterator[dict]:
    """Follow the container event stream until ``stop`` is set.

    The caller is expected to run a full ``containers()`` scan on every reconnection as
    well as trusting ``since``: that window can still miss events across an engine restart,
    and a stale domain list is worse than one redundant scan.
    """
    reader, writer = await _open(_events_path(since), STREAM_POLL_SECONDS, host)
    try:
        status, headers = await read_head(reader, STREAM_POLL_SECONDS)
        if status != 200:
            raise DockerUnavailable(f"/events returned HTTP {status}")
        chunked = "chunked" in headers.get("transfer-encoding", "").lower()

        while not stop.is_set():
            try:
                line = await asyncio.wait_for(reader.readline(), STREAM_POLL_SECONDS)
            except TimeoutError:
                # Ordinary: the engine speaks only when something happens.
                continue
            if not line:
                return
            text = line.strip()
            if not text:
                continue
            if chunked and not text.startswith(b"{"):
                # A chunk-length line rather than a payload.
                continue
            try:
                yield json.loads(text.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                continue
    except (HttpError, OSError, asyncio.IncompleteReadError) as exc:
        raise DockerUnavailable(str(exc)) from exc
    finally:
        writer.close()
        with contextlib.suppress(OSError, asyncio.CancelledError):
            await writer.wait_closed()
        log.write("docker: event stream closed")
