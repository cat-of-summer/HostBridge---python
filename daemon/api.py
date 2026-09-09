"""The loopback control API the console and the GUI talk to.

Hand-written over ``asyncio.start_server`` rather than built on ``http.server``: the live
log is a server-sent event stream fed by events that originate in the resolver's own event
loop, and in-loop that is an ``asyncio.Queue`` per subscriber and a three-line fan-out.
From a thread it would need a cross-thread bridge and a shutdown race nobody enjoys.
``http.server`` would also log every request to ``sys.stderr``, which in a windowed build is
the null device at best.

The surface is nine routes, so the request reader is a readline for the request line, a
readline loop for the headers, and a bounded read for the body.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from core import log
from core.events import Bus
from core.model import SOURCE_MANUAL, Domain
from core.store import StoreLocked, StoreReadOnly
from daemon import auth
from daemon.status import collect as collect_status

MAX_HEADER_BYTES = 64 * 1024
MAX_BODY_BYTES = 1024 * 1024

REQUEST_TIMEOUT = 30.0

#: A comment line often enough that a dead peer is noticed, rare enough to be invisible.
SSE_KEEPALIVE_SECONDS = 15.0


@dataclass
class Request:
    method: str
    path: str
    query: dict[str, list[str]] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""

    def json(self) -> Any:
        if not self.body:
            return {}
        try:
            return json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return None


@dataclass
class Response:
    status: int = 200
    payload: Any = None
    reason: str = ""

    def render(self) -> bytes:
        body = json.dumps(
            self.payload if self.payload is not None else {"error": self.reason},
            ensure_ascii=False,
        ).encode("utf-8")
        head = (
            f"HTTP/1.1 {self.status} {_reason(self.status)}\r\n"
            "Content-Type: application/json; charset=utf-8\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Cache-Control: no-store\r\n"
            "Connection: close\r\n"
            "\r\n"
        )
        return head.encode("latin-1") + body


_REASONS = {
    200: "OK",
    201: "Created",
    204: "No Content",
    400: "Bad Request",
    401: "Unauthorized",
    403: "Forbidden",
    404: "Not Found",
    405: "Method Not Allowed",
    409: "Conflict",
    413: "Payload Too Large",
    500: "Internal Server Error",
}


def _reason(status: int) -> str:
    return _REASONS.get(status, "Unknown")


class ControlApi:
    """Serves the control surface on an ephemeral loopback port."""

    def __init__(self, runner, bus: Bus | None = None) -> None:
        self.runner = runner
        self.bus = bus or Bus()
        self.token = auth.generate_token()
        self.port = 0
        self._server: asyncio.AbstractServer | None = None
        self._authorities: set[str] = set()
        self._streams: set[asyncio.Task] = set()

    # ---- lifecycle ----------------------------------------------------------------

    async def start(self) -> int:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        sock.listen(16)
        sock.setblocking(False)
        self.port = sock.getsockname()[1]
        self._authorities = {
            f"127.0.0.1:{self.port}",
            f"localhost:{self.port}",
            f"[::1]:{self.port}",
        }
        self._server = await asyncio.start_server(self._handle, sock=sock)

        # Published last, once we are actually listening: its existence is what a client
        # polls for to know the daemon is ready.
        auth.write(self.port, self.token)
        log.write(f"api: listening on 127.0.0.1:{self.port}")
        return self.port

    async def stop(self) -> None:
        self.bus.close()
        for task in tuple(self._streams):
            task.cancel()
        if self._streams:
            await asyncio.gather(*self._streams, return_exceptions=True)
        self._streams.clear()

        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
            self._server = None
        auth.clear()

    # ---- the wire -----------------------------------------------------------------

    async def _read_request(self, reader: asyncio.StreamReader) -> Request | None:
        line = await asyncio.wait_for(reader.readline(), REQUEST_TIMEOUT)
        if not line:
            return None
        parts = line.decode("latin-1").split()
        if len(parts) < 2:
            return None
        method, target = parts[0].upper(), parts[1]

        headers: dict[str, str] = {}
        read = len(line)
        while True:
            header_line = await asyncio.wait_for(reader.readline(), REQUEST_TIMEOUT)
            read += len(header_line)
            if header_line in (b"\r\n", b"\n", b""):
                break
            if read > MAX_HEADER_BYTES:
                return None
            name, separator, value = header_line.decode("latin-1").partition(":")
            if separator:
                headers[name.strip().lower()] = value.strip()

        body = b""
        length = headers.get("content-length")
        if length is not None and length.isdigit():
            size = int(length)
            if size > MAX_BODY_BYTES:
                return Request(method=method, path="/__too_large__", headers=headers)
            with contextlib.suppress(asyncio.IncompleteReadError):
                body = await asyncio.wait_for(reader.readexactly(size), REQUEST_TIMEOUT)

        split = urlsplit(target)
        return Request(
            method=method,
            path=unquote(split.path),
            query=parse_qs(split.query),
            headers=headers,
            body=body,
        )

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request = await self._read_request(reader)
            if request is None:
                return
            if request.path == "/__too_large__":
                writer.write(Response(413, reason="body too large").render())
                await writer.drain()
                return

            # /v1/health is the one route served without a token, so a client can find out
            # it is talking to a daemon of the wrong version instead of just failing.
            needs_token = request.path != "/v1/health"
            verdict = auth.authorise(
                request.headers, self.token, self._authorities, require_token=needs_token
            )
            if not verdict.ok:
                log.warn(f"api: refused {request.method} {request.path}: {verdict.reason}")
                writer.write(Response(verdict.status, reason=verdict.reason).render())
                await writer.drain()
                return

            if request.method == "GET" and request.path == "/v1/events":
                await self._stream_events(writer)
                return

            response = await self._route(request)
            writer.write(response.render())
            await writer.drain()
        except (TimeoutError, ConnectionError, OSError):
            return
        except Exception as exc:  # noqa: BLE001 - one bad request must not stop the server
            log.error(f"api: {exc!r}")
            with contextlib.suppress(Exception):
                writer.write(Response(500, reason=str(exc)).render())
                await writer.drain()
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    # ---- routes -------------------------------------------------------------------

    async def _route(self, request: Request) -> Response:
        path, method = request.path, request.method

        if path == "/v1/health":
            return Response(200, auth.health_payload({"mode": self.runner.policy.name}))

        if path == "/v1/status":
            return Response(200, self._status_payload())

        if path == "/v1/domains":
            if method == "GET":
                return Response(200, self._domains_payload())
            if method == "POST":
                return self._create(request)
            return Response(405, reason="GET or POST")

        if path.startswith("/v1/domains/"):
            rest = path[len("/v1/domains/") :]
            identifier, _, action = rest.partition("/")
            if action == "toggle" and method == "POST":
                return self._toggle(identifier, request)
            if action:
                return Response(404, reason="no such action")
            if method == "PATCH":
                return self._update(identifier, request)
            if method == "DELETE":
                return self._delete(identifier)
            return Response(405, reason="PATCH or DELETE")

        if path == "/v1/traefik/refresh" and method == "POST":
            changed = await self.runner.poll_traefik()
            return Response(200, {"changed": changed, "reachable": self.runner.traefik_ok})

        if path == "/v1/settings":
            if method == "GET":
                return Response(200, self._settings_payload())
            if method == "PATCH":
                return self._update_settings(request)
            return Response(405, reason="GET or PATCH")

        if path == "/v1/docker/refresh" and method == "POST":
            changed = await self.runner.poll_docker()
            return Response(200, {"changed": changed, "reachable": self.runner.docker_ok})

        if path == "/v1/docker/containers" and method == "GET":
            return Response(200, await self._containers_payload())

        if path == "/v1/reapply" and method == "POST":
            self.runner.reload()
            return Response(200, self._status_payload())

        return Response(404, reason=f"no route for {method} {path}")

    # ---- payloads -----------------------------------------------------------------

    def _domains_payload(self) -> dict[str, Any]:
        snapshot = self.runner.store.load()
        return {
            "read_only": snapshot.read_only,
            "domains": [domain.to_dict() for domain in snapshot.domains],
        }

    #: Settings the window may change. Everything else in DaemonSettings is either derived
    #: or dangerous to edit from a form, and an allow-list means a new field is invisible
    #: until someone decides it should be editable rather than the other way round.
    EDITABLE = (
        "listen_address",
        "listen_port",
        "upstreams",
        "local_ttl",
        "traefik_enabled",
        "traefik_api",
        "traefik_poll_seconds",
        "docker_enabled",
        "docker_host",
        "bypass_dns_filter",
    )

    #: Changing these rebinds sockets or re-captures the upstream list, neither of which can
    #: be done under a running resolver without a gap. The window says so instead of
    #: pretending the change took effect.
    NEEDS_RESTART = ("listen_address", "listen_port", "upstreams")

    def _settings_payload(self) -> dict[str, Any]:
        settings = self.runner.settings
        return {
            "settings": {name: getattr(settings, name) for name in self.EDITABLE},
            "needs_restart": list(self.NEEDS_RESTART),
        }

    def _update_settings(self, request: Request) -> Response:
        """Write the daemon's settings file, because the window cannot.

        ``settings.json`` lives in the machine directory, whose permissions are tightened to
        whoever runs the daemon -- SYSTEM, when it is a service. The window runs as the user
        and would simply be refused, so it asks us and we write.
        """
        body = request.json()
        if not isinstance(body, dict):
            return Response(400, reason="a JSON object was expected")

        settings = self.runner.settings
        changed: list[str] = []
        for name, value in body.items():
            if name not in self.EDITABLE:
                return Response(400, reason=f"{name} is not editable")
            current = getattr(settings, name)
            if not isinstance(value, type(current)) and not (
                isinstance(current, list) and isinstance(value, list)
            ):
                return Response(400, reason=f"{name} has the wrong type")
            if value != current:
                setattr(settings, name, value)
                changed.append(name)

        if not changed:
            return Response(200, {"changed": [], **self._settings_payload()})

        try:
            settings.save()
        except OSError as exc:
            return Response(500, reason=str(exc))

        log.write(f"api: settings changed: {', '.join(sorted(changed))}")
        self.bus.publish("settings", changed=sorted(changed))
        return Response(
            200,
            {
                "changed": sorted(changed),
                "restart_required": sorted(set(changed) & set(self.NEEDS_RESTART)),
                **self._settings_payload(),
            },
        )

    async def _containers_payload(self) -> dict[str, Any]:
        """What the Docker tab draws.

        Every running container is listed, not only the labelled ones: the tab's purpose is
        partly to show the user which containers *could* be given a domain, and a list that
        silently omits them cannot do that.
        """
        from discover import labels as labelmod
        from discover.dockerhttp import DockerUnavailable, containers

        try:
            listing = await containers(host=self.runner.settings.docker_host)
        except DockerUnavailable as exc:
            return {"reachable": False, "error": str(exc), "containers": []}

        rows = []
        for container in listing:
            found = labelmod.labels_of(container)
            names = labelmod.wanted_names(found)
            rows.append(
                {
                    "id": str(container.get("Id", ""))[:12],
                    "name": labelmod.container_name(container),
                    "image": str(container.get("Image", "")),
                    "state": str(container.get("State", "")),
                    "status": str(container.get("Status", "")),
                    "names": names,
                    "address": labelmod.address_of(found) if names else "",
                }
            )
        rows.sort(key=lambda row: row["name"])
        return {"reachable": True, "error": "", "containers": rows}

    def _status_payload(self) -> dict[str, Any]:
        status = collect_status(self.runner.store)
        resolver = self.runner.resolver
        return {
            "running": True,
            "mechanism": self.runner.policy.name,
            "listen": self.runner.listen_addresses(),
            "port": self.runner.settings.listen_port,
            "upstreams": self.runner.upstreams,
            "claimed": list(self.runner.applied.namespaces),
            "policy_error": self.runner.policy_error,
            "filtered_primary": self.runner.filtered_primary,
            "bypass_listen": list(self.runner.bypass_listen),
            "traefik_ok": self.runner.traefik_ok,
            "docker_ok": self.runner.docker_ok,
            "domains_total": status.total,
            "domains_enabled": status.enabled,
            "queries": getattr(resolver, "queries", 0),
            "answered_locally": getattr(resolver, "answered_locally", 0),
            "forwarded": getattr(resolver, "forwarded", 0),
        }

    def _after_change(self, kind: str, **payload: Any) -> None:
        self.runner.reload()
        self.bus.publish("domains", action=kind, **payload)

    # ---- mutations ----------------------------------------------------------------

    def _create(self, request: Request) -> Response:
        body = request.json()
        if not isinstance(body, dict):
            return Response(400, reason="a JSON object is required")

        from core.match import validate

        checked = validate(str(body.get("name", "")))
        if not checked.ok:
            return Response(
                400,
                {
                    "error": checked.error.key,
                    "params": checked.error.params,
                    "warnings": [w.key for w in checked.warnings],
                },
            )

        domain = Domain(
            name=checked.name,
            address=str(body.get("address") or "127.0.0.1"),
            note=str(body.get("note") or ""),
            enabled=bool(body.get("enabled", True)),
            source=SOURCE_MANUAL,
        )
        try:
            created = self.runner.store.add(domain)
        except ValueError as exc:
            return Response(409, reason=f"{exc} already exists")
        except (StoreReadOnly, StoreLocked) as exc:
            return Response(409, reason=str(exc))

        self._after_change("created", id=created.id, name=created.name)
        return Response(
            201,
            {
                "domain": created.to_dict(),
                "warnings": [{"key": w.key, "params": w.params} for w in checked.warnings],
            },
        )

    def _update(self, identifier: str, request: Request) -> Response:
        body = request.json()
        if not isinstance(body, dict):
            return Response(400, reason="a JSON object is required")

        changes: dict[str, Any] = {}
        for key in ("address", "note"):
            if key in body:
                changes[key] = str(body[key])
        if "enabled" in body:
            changes["enabled"] = bool(body["enabled"])
        if "name" in body:
            from core.match import validate

            checked = validate(str(body["name"]))
            if not checked.ok:
                return Response(400, {"error": checked.error.key, "params": checked.error.params})
            changes["name"] = checked.name

        if not changes:
            return Response(400, reason="nothing to change")

        try:
            updated = self.runner.store.update(identifier, **changes)
        except (StoreReadOnly, StoreLocked) as exc:
            return Response(409, reason=str(exc))
        if updated is None:
            return Response(404, reason="no such domain")

        self._after_change("updated", id=updated.id, name=updated.name)
        return Response(200, {"domain": updated.to_dict()})

    def _toggle(self, identifier: str, request: Request) -> Response:
        body = request.json()
        wanted = body.get("enabled") if isinstance(body, dict) else None
        try:
            updated = self.runner.store.toggle(
                identifier, enabled=None if wanted is None else bool(wanted)
            )
        except (StoreReadOnly, StoreLocked) as exc:
            return Response(409, reason=str(exc))
        if updated is None:
            return Response(404, reason="no such domain")

        self._after_change("toggled", id=updated.id, name=updated.name, enabled=updated.enabled)
        return Response(200, {"domain": updated.to_dict()})

    def _delete(self, identifier: str) -> Response:
        try:
            removed = self.runner.store.delete(identifier)
        except (StoreReadOnly, StoreLocked) as exc:
            return Response(409, reason=str(exc))
        if not removed:
            return Response(404, reason="no such domain")

        self._after_change("deleted", id=identifier)
        return Response(200, {"deleted": identifier})

    # ---- server-sent events -------------------------------------------------------

    async def _stream_events(self, writer: asyncio.StreamWriter) -> None:
        head = (
            "HTTP/1.1 200 OK\r\n"
            "Content-Type: text/event-stream; charset=utf-8\r\n"
            "Cache-Control: no-store\r\n"
            "X-Accel-Buffering: no\r\n"
            "Connection: close\r\n"
            "\r\n"
        )
        writer.write(head.encode("latin-1"))
        await writer.drain()

        task = asyncio.current_task()
        if task is not None:
            self._streams.add(task)
        try:
            async with self.bus.subscribe() as subscription:
                for event in self.bus.history():
                    writer.write(_sse(event.to_dict()))
                await writer.drain()

                while True:
                    try:
                        event = await asyncio.wait_for(
                            subscription.get(), SSE_KEEPALIVE_SECONDS
                        )
                    except TimeoutError:
                        # A comment line: keeps the connection warm and reveals a peer that
                        # went away without a FIN.
                        writer.write(b": keepalive\n\n")
                        await writer.drain()
                        continue
                    if event is None:
                        return
                    writer.write(_sse(event.to_dict()))
                    await writer.drain()
        except (ConnectionError, OSError, asyncio.CancelledError):
            return
        finally:
            if task is not None:
                self._streams.discard(task)


def _sse(payload: dict[str, Any]) -> bytes:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode()
