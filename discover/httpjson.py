"""A minimal async HTTP/1.1 GET returning parsed JSON.

Hand-written rather than ``urllib`` in a thread pool: the daemon is deliberately one
asyncio loop with no threads, and a blocking call in it would stall the resolver for as
long as an unreachable Traefik takes to time out. It is forty lines because we need exactly
one verb against one kind of endpoint.

Deliberately not a general HTTP client: no redirects, no keep-alive, no compression. It does
handle ``Transfer-Encoding: chunked``, and not as a nicety -- a real Traefik answers
``/api/http/routers`` chunked even when the request says ``Connection: close``, so a client
that reads the body raw gets the hex chunk length glued to the front of the JSON and fails
with a parse error that looks like the endpoint is broken.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

DEFAULT_TIMEOUT = 5.0

#: Enough for a large router table and small enough that a wrong endpoint answering with a
#: video stream cannot exhaust memory.
MAX_BODY = 8 * 1024 * 1024

MAX_HEADER_BYTES = 64 * 1024


class HttpError(Exception):
    """The endpoint was unreachable, refused, or answered with something unusable."""


@dataclass(frozen=True)
class Target:
    host: str
    port: int
    path: str


def parse_url(url: str, default_path: str = "/") -> Target:
    parts = urlsplit(url if "//" in url else f"http://{url}")
    if parts.scheme not in ("", "http"):
        # https would need a TLS context and a certificate decision; the Traefik API is a
        # loopback endpoint and does not need one.
        raise HttpError(f"unsupported scheme: {parts.scheme}")
    host = parts.hostname or "127.0.0.1"
    port = parts.port or 80
    path = parts.path or default_path
    if parts.query:
        path = f"{path}?{parts.query}"
    return Target(host=host, port=port, path=path)


async def _read_chunked(reader: asyncio.StreamReader, timeout: float) -> bytes:
    """Reassemble a chunked body.

    Each chunk is a hex length, optional extensions after a semicolon, CRLF, the bytes, and
    a trailing CRLF. A zero-length chunk ends the body; any trailer that follows is of no
    interest to us.
    """
    parts: list[bytes] = []
    total = 0
    while True:
        line = await asyncio.wait_for(reader.readline(), timeout)
        if not line:
            raise HttpError("the connection closed mid-chunk")
        size_text = line.split(b";", 1)[0].strip()
        try:
            size = int(size_text, 16)
        except ValueError as exc:
            raise HttpError(f"unreadable chunk length: {size_text!r}") from exc
        if size == 0:
            # Consume the trailer section up to the closing blank line.
            while True:
                trailer = await asyncio.wait_for(reader.readline(), timeout)
                if trailer in (b"\r\n", b"\n", b""):
                    break
            return b"".join(parts)

        total += size
        if total > MAX_BODY:
            raise HttpError("the response body is implausibly large")
        parts.append(await asyncio.wait_for(reader.readexactly(size), timeout))
        await asyncio.wait_for(reader.readexactly(2), timeout)  # the chunk's trailing CRLF


async def _read_body(
    reader: asyncio.StreamReader, headers: dict[str, str], timeout: float
) -> bytes:
    if "chunked" in headers.get("transfer-encoding", "").lower():
        return await _read_chunked(reader, timeout)

    length = headers.get("content-length")
    if length is not None and length.isdigit():
        size = min(int(length), MAX_BODY)
        try:
            return await asyncio.wait_for(reader.readexactly(size), timeout)
        except asyncio.IncompleteReadError as exc:
            return exc.partial

    # No framing at all: the server closes the connection to signal the end.
    return await asyncio.wait_for(reader.read(MAX_BODY), timeout)


async def get_json(url: str, *, timeout: float = DEFAULT_TIMEOUT) -> Any:
    """GET ``url`` and parse the body as JSON, or raise :class:`HttpError`."""
    target = parse_url(url)

    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(target.host, target.port), timeout
        )
    except (TimeoutError, OSError) as exc:
        raise HttpError(f"{target.host}:{target.port}: {exc}") from exc

    try:
        request = (
            f"GET {target.path} HTTP/1.1\r\n"
            f"Host: {target.host}:{target.port}\r\n"
            "Accept: application/json\r\n"
            "User-Agent: HostBridge\r\n"
            "Connection: close\r\n"
            "\r\n"
        )
        writer.write(request.encode("ascii"))
        await writer.drain()

        status_line = await asyncio.wait_for(reader.readline(), timeout)
        if not status_line:
            raise HttpError("the connection closed before any response")
        parts = status_line.decode("latin-1").split()
        if len(parts) < 2 or not parts[1].isdigit():
            raise HttpError(f"unreadable status line: {status_line!r}")
        status = int(parts[1])

        headers: dict[str, str] = {}
        read = len(status_line)
        while True:
            line = await asyncio.wait_for(reader.readline(), timeout)
            read += len(line)
            if line in (b"\r\n", b"\n", b""):
                break
            if read > MAX_HEADER_BYTES:
                raise HttpError("response headers are implausibly large")
            name, separator, value = line.decode("latin-1").partition(":")
            if separator:
                headers[name.strip().lower()] = value.strip()

        body = await _read_body(reader, headers, timeout)
    except (TimeoutError, asyncio.IncompleteReadError) as exc:
        raise HttpError(f"{target.host}:{target.port}: incomplete response ({exc})") from exc
    except OSError as exc:
        raise HttpError(f"{target.host}:{target.port}: {exc}") from exc
    finally:
        writer.close()
        with contextlib.suppress(OSError, asyncio.CancelledError):
            await writer.wait_closed()

    if status != 200:
        raise HttpError(f"{target.path} returned HTTP {status}")

    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise HttpError(f"{target.path} did not return JSON: {exc}") from exc
