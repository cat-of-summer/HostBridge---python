"""The one way the interfaces reach the daemon.

Neither the console screen nor the GUI ever builds an HTTP request or opens the store for
writing. They hold a :class:`Bridge`, and it decides -- once, in :meth:`Bridge.connect` --
whether there is a daemon to talk to.

When there is not, it falls back to the store on disk -- and that fallback is writable,
which deserves its reasoning written down because the obvious instinct is to make it
read-only.

The hazard a read-only fallback would guard against is the store and the system's
resolution policy disagreeing. That cannot arise here: with no daemon there is no policy
applied, so there is nothing to disagree with; and a daemon that *is* running notices an
edit within a second through its store watcher. What the interfaces must not do is pretend
the change took effect, so :attr:`Snapshot.online` reports the resolver's real state and
both interfaces show it.
"""

from __future__ import annotations

import contextlib
import json
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from core.model import Domain
from core.store import DomainStore, StoreLocked, StoreReadOnly
from core.version import API_VERSION
from daemon import auth

TIMEOUT = 5.0

#: How long a stream read blocks before the loop looks at the stop flag again. Short on
#: purpose: it is the upper bound on how long closing the window waits for the thread.
STREAM_POLL_SECONDS = 1.0


class BridgeError(Exception):
    """Something the caller should show the user."""


class BridgeOffline(BridgeError):
    """There is no daemon to talk to."""


class BridgeReadOnly(BridgeError):
    """A mutation was attempted while offline."""


class BridgeConflict(BridgeError):
    """The name is already taken. Kept distinct so the interfaces can say which name."""

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.name = name


class BridgeVersionMismatch(BridgeError):
    """The running daemon speaks a different control API."""


@dataclass(frozen=True)
class Snapshot:
    online: bool
    read_only: bool
    domains: tuple[Domain, ...] = ()
    status: dict[str, Any] = field(default_factory=dict)

    def by_id(self, identifier: str) -> Domain | None:
        return next((d for d in self.domains if d.id == identifier), None)


def _to_domains(payload: Any) -> tuple[Domain, ...]:
    entries = payload.get("domains") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        return ()
    found = [Domain.from_dict(entry) for entry in entries]
    return tuple(domain for domain in found if domain is not None)


class LocalBackend:
    """Works the store directly, for when no daemon is listening.

    Every operation mirrors a route of the control API so the two backends are
    interchangeable from the caller's point of view.
    """

    online = False

    def __init__(self, store: DomainStore | None = None) -> None:
        self.store = store or DomainStore()

    def snapshot(self) -> Snapshot:
        loaded = self.store.load()
        return Snapshot(
            online=False,
            read_only=loaded.read_only,
            domains=tuple(loaded.domains),
            status={"running": False},
        )

    def call(self, method: str, path: str, body: Any = None) -> Any:
        from core.match import validate
        from core.model import SOURCE_MANUAL

        body = body or {}
        try:
            if method == "POST" and path == "/v1/domains":
                checked = validate(str(body.get("name", "")))
                if not checked.ok:
                    raise BridgeError(checked.error.key)
                created = self.store.add(
                    Domain(
                        name=checked.name,
                        address=str(body.get("address") or "127.0.0.1"),
                        note=str(body.get("note") or ""),
                        source=SOURCE_MANUAL,
                    )
                )
                return {
                    "domain": created.to_dict(),
                    "warnings": [{"key": w.key, "params": w.params} for w in checked.warnings],
                }

            if path.startswith("/v1/domains/"):
                rest = path[len("/v1/domains/") :]
                identifier, _, action = rest.partition("/")
                if action == "toggle":
                    wanted = body.get("enabled")
                    updated = self.store.toggle(
                        identifier, enabled=None if wanted is None else bool(wanted)
                    )
                elif method == "PATCH":
                    updated = self.store.update(identifier, **body)
                elif method == "DELETE":
                    if not self.store.delete(identifier):
                        raise BridgeError("no such domain")
                    return {"deleted": identifier}
                else:
                    raise BridgeError(f"unsupported offline: {method} {path}")

                if updated is None:
                    raise BridgeError("no such domain")
                return {"domain": updated.to_dict()}
        except ValueError as exc:
            raise BridgeConflict(str(exc)) from exc
        except StoreReadOnly as exc:
            raise BridgeReadOnly(str(exc)) from exc
        except StoreLocked as exc:
            raise BridgeError(str(exc)) from exc

        # Anything that genuinely needs a running resolver.
        raise BridgeOffline("the resolver is not running")

    def stream(self, stop: threading.Event) -> Iterator[dict[str, Any]]:  # noqa: ARG002
        return iter(())


class HttpBackend:
    online = True

    def __init__(self, info: auth.DaemonInfo) -> None:
        self.info = info

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.info.token}",
            auth.API_HEADER: str(API_VERSION),
            "Accept": "application/json",
            # Deliberately no Origin: the daemon refuses any request that carries one, and
            # a client that set it would lock itself out.
        }

    def _connect(self, timeout: float = TIMEOUT):
        import http.client

        return http.client.HTTPConnection("127.0.0.1", self.info.port, timeout=timeout)

    def call(self, method: str, path: str, body: Any = None) -> Any:
        connection = self._connect()
        try:
            payload = None
            headers = self._headers()
            if body is not None:
                payload = json.dumps(body).encode("utf-8")
                headers["Content-Type"] = "application/json"
            connection.request(method, path, body=payload, headers=headers)
            response = connection.getresponse()
            raw = response.read()
        except OSError as exc:
            raise BridgeOffline(str(exc)) from exc
        finally:
            with contextlib.suppress(Exception):
                connection.close()

        try:
            parsed = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, ValueError):
            parsed = {}

        error = str(parsed.get("error", ""))
        if response.status == 409 and "control API" in error:
            raise BridgeVersionMismatch(error)
        if response.status == 409 and error.endswith("already exists"):
            raise BridgeConflict(error.removesuffix(" already exists"))
        if response.status >= 400:
            raise BridgeError(str(parsed.get("error") or f"HTTP {response.status}"))
        return parsed

    def snapshot(self) -> Snapshot:
        domains = self.call("GET", "/v1/domains")
        status = self.call("GET", "/v1/status")
        return Snapshot(
            online=True,
            read_only=bool(domains.get("read_only")),
            domains=_to_domains(domains),
            status=status if isinstance(status, dict) else {},
        )

    def stream(self, stop: threading.Event) -> Iterator[dict[str, Any]]:
        """Yield events until ``stop`` is set or the daemon goes away.

        A blocking generator rather than a callback: the GUI runs it on a QThread and the
        console on a plain thread, and both end it by setting one event.

        The response body is read with ``select`` on the bare socket rather than through
        the file object ``http.client`` wraps around it, and that is not a style choice.
        A socket file wrapper latches the first timeout permanently: after one expiry every
        later read raises ``OSError: cannot read from timed out object``, whatever the
        socket then does. Since this stream is silent by design -- the daemon speaks only
        when something happens -- the first quiet second killed it, and the live log simply
        stopped a second after the window opened. ``select`` never touches the socket
        unless there is something to read, so no timeout is ever raised.

        The poll interval is the upper bound on how long closing the window waits for this
        thread, and a thread still running at teardown is the classic "QThread destroyed
        while still running" abort on exit.
        """
        import select
        import socket as socketlib

        try:
            sock = socketlib.create_connection(("127.0.0.1", self.info.port), timeout=TIMEOUT)
        except OSError:
            return

        try:
            head = f"GET /v1/events HTTP/1.1\r\nHost: 127.0.0.1:{self.info.port}\r\n"
            for name, value in self._headers().items():
                head += f"{name}: {value}\r\n"
            head += "Accept: text/event-stream\r\nConnection: close\r\n\r\n"
            sock.sendall(head.encode("latin-1"))

            sock.setblocking(False)
            buffer = b""
            headers_done = False

            while not stop.is_set():
                ready, _, _ = select.select([sock], [], [], STREAM_POLL_SECONDS)
                if not ready:
                    continue
                try:
                    chunk = sock.recv(65536)
                except (BlockingIOError, InterruptedError):
                    continue
                except OSError:
                    return
                if not chunk:
                    return
                buffer += chunk

                if not headers_done:
                    separator = buffer.find(b"\r\n\r\n")
                    if separator < 0:
                        continue
                    status = buffer[:separator].split(b"\r\n", 1)[0].split()
                    if len(status) < 2 or status[1] != b"200":
                        return
                    buffer = buffer[separator + 4 :]
                    headers_done = True

                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    text = line.decode("utf-8", "replace").strip()
                    # Blank lines separate events and a leading colon is a keepalive
                    # comment; both are ordinary traffic, not a fault.
                    if not text or text.startswith(":") or not text.startswith("data:"):
                        continue
                    with contextlib.suppress(ValueError):
                        yield json.loads(text[len("data:") :].strip())
        except OSError:
            return
        finally:
            with contextlib.suppress(OSError):
                sock.close()


class Bridge:
    """The facade. Construct with :meth:`connect`, which never raises."""

    def __init__(self, backend: LocalBackend | HttpBackend) -> None:
        self._backend = backend

    @classmethod
    def connect(cls, store: DomainStore | None = None) -> Bridge:
        """Find a daemon, or fall back to a read-only view of the store."""
        info = auth.read()
        if info is None:
            return cls(LocalBackend(store))
        if info.api_version != API_VERSION:
            # A daemon from another build. Reporting it as offline read-only is better than
            # sending it requests it will answer with 409 for every action.
            return cls(LocalBackend(store))

        backend = HttpBackend(info)
        try:
            backend.call("GET", "/v1/health")
        except BridgeError:
            return cls(LocalBackend(store))
        return cls(backend)

    @property
    def online(self) -> bool:
        return self._backend.online

    def snapshot(self) -> Snapshot:
        try:
            return self._backend.snapshot()
        except BridgeError:
            raise
        except OSError as exc:
            raise BridgeOffline(str(exc)) from exc

    # ---- mutations ----------------------------------------------------------------

    def add(self, name: str, address: str = "127.0.0.1", note: str = "") -> dict[str, Any]:
        return self._backend.call(
            "POST", "/v1/domains", {"name": name, "address": address, "note": note}
        )

    def update(self, identifier: str, **fields: Any) -> dict[str, Any]:
        return self._backend.call("PATCH", f"/v1/domains/{identifier}", fields)

    def toggle(self, identifier: str, enabled: bool | None = None) -> dict[str, Any]:
        body = {} if enabled is None else {"enabled": enabled}
        return self._backend.call("POST", f"/v1/domains/{identifier}/toggle", body)

    def delete(self, identifier: str) -> dict[str, Any]:
        return self._backend.call("DELETE", f"/v1/domains/{identifier}")

    def refresh_traefik(self) -> dict[str, Any]:
        return self._backend.call("POST", "/v1/traefik/refresh", {})

    def refresh_docker(self) -> dict[str, Any]:
        return self._backend.call("POST", "/v1/docker/refresh", {})

    def containers(self) -> dict[str, Any]:
        """The running containers as the daemon sees them.

        Asked of the daemon rather than of Docker directly: the GUI runs as the user, and on
        Windows the engine's named pipe is routinely readable only by the ``docker-users``
        group. The daemon is elevated and already connected.
        """
        return self._backend.call("GET", "/v1/docker/containers")

    def settings(self) -> dict[str, Any]:
        return self._backend.call("GET", "/v1/settings")

    def update_settings(self, **fields: Any) -> dict[str, Any]:
        """Change the daemon's settings.

        Through the daemon rather than by writing the file: ``settings.json`` lives in the
        machine directory, whose permissions belong to whoever runs the resolver -- SYSTEM,
        for a service -- and the window, running as the user, would simply be refused.
        """
        return self._backend.call("PATCH", "/v1/settings", fields)

    def reapply(self) -> dict[str, Any]:
        return self._backend.call("POST", "/v1/reapply", {})

    # ---- events -------------------------------------------------------------------

    def events(self, stop: threading.Event) -> Iterator[dict[str, Any]]:
        return self._backend.stream(stop)
