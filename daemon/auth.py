"""Who may talk to the control API, and how they prove it.

This application exists to convince browsers to send traffic to 127.0.0.1, which makes its
own control port an unusually attractive DNS-rebinding target: a page the user visits could
resolve an attacker-controlled name to loopback and then talk to us. So the checks below are
not only about the token.

Four rules, in this order, and the first three run **before** the token is compared so a
browser-shaped request is turned away without revealing anything:

1. an ``Origin`` header at all -- **any** value -- is refused. No legitimate client of ours
   sends one; only a browser does. A custom ``Authorization`` header cannot be sent
   cross-origin without a preflight, and we never answer one, so this closes the simple
   path outright rather than trying to judge which origins are acceptable.
2. ``Sec-Fetch-Site`` or ``Sec-Fetch-Mode`` present -- same reasoning, and it catches a
   ``no-cors`` fetch that carries no ``Origin``.
3. ``Host`` must be one of the loopback authorities we actually bound. A rebinding attack
   arrives with the attacker's hostname there.
4. the bearer token, compared with :func:`hmac.compare_digest`. A timing oracle on a token
   that lets the holder rewrite name resolution is worth closing.
"""

from __future__ import annotations

import contextlib
import hmac
import os
import secrets
from dataclasses import asdict, dataclass
from typing import Any

from core.jsonio import read_json, write_json_atomic
from core.model import utc_now
from core.paths import daemon_file
from core.version import API_VERSION, __version__

TOKEN_BYTES = 32

#: Sent by our own clients and checked on every request, so a version mismatch is reported
#: as such instead of failing later on a missing JSON field.
API_HEADER = "x-hostbridge-api"


@dataclass(frozen=True)
class DaemonInfo:
    api_version: int
    app_version: str
    pid: int
    port: int
    token: str
    started_at: str

    @property
    def authorities(self) -> set[str]:
        return {f"127.0.0.1:{self.port}", f"[::1]:{self.port}", f"localhost:{self.port}"}


def generate_token() -> str:
    return secrets.token_urlsafe(TOKEN_BYTES)


def write(port: int, token: str) -> DaemonInfo:
    """Publish the connection details.

    Written **last**, after the API is listening, so its existence is the readiness signal a
    client can poll for. Hardened, because unlike every other file we write this one holds a
    secret -- see :func:`core.jsonio.write_json_atomic`.
    """
    info = DaemonInfo(
        api_version=API_VERSION,
        app_version=__version__,
        pid=os.getpid(),
        port=port,
        token=token,
        started_at=utc_now(),
    )
    write_json_atomic(daemon_file(), asdict(info), harden=True)
    return info


def read() -> DaemonInfo | None:
    raw = read_json(daemon_file())
    if not isinstance(raw, dict):
        return None
    try:
        return DaemonInfo(
            api_version=int(raw["api_version"]),
            app_version=str(raw.get("app_version", "")),
            pid=int(raw.get("pid", 0)),
            port=int(raw["port"]),
            token=str(raw["token"]),
            started_at=str(raw.get("started_at", "")),
        )
    except (KeyError, TypeError, ValueError):
        return None


def clear() -> None:
    with contextlib.suppress(OSError):
        daemon_file().unlink()


@dataclass(frozen=True)
class AuthResult:
    ok: bool
    status: int = 200
    reason: str = ""

    @classmethod
    def allow(cls) -> AuthResult:
        return cls(ok=True)

    @classmethod
    def deny(cls, status: int, reason: str) -> AuthResult:
        return cls(ok=False, status=status, reason=reason)


def authorise(
    headers: dict[str, str],
    token: str,
    authorities: set[str],
    *,
    require_token: bool = True,
) -> AuthResult:
    """Decide whether one request may proceed. See the module docstring for the order."""
    if "origin" in headers:
        return AuthResult.deny(
            403, "an Origin header means a browser, and browsers are not clients"
        )

    if "sec-fetch-site" in headers or "sec-fetch-mode" in headers:
        return AuthResult.deny(403, "a fetch-metadata header means a browser")

    host = headers.get("host", "")
    if host and host.lower() not in authorities:
        return AuthResult.deny(403, f"unexpected Host: {host}")

    sent_version = headers.get(API_HEADER)
    if sent_version is not None and sent_version.strip() != str(API_VERSION):
        return AuthResult.deny(409, f"this daemon speaks control API {API_VERSION}")

    if not require_token:
        return AuthResult.allow()

    authorization = headers.get("authorization", "")
    scheme, _, presented = authorization.partition(" ")
    if scheme.lower() != "bearer" or not presented:
        return AuthResult.deny(401, "a bearer token is required")
    if not hmac.compare_digest(presented.strip(), token):
        return AuthResult.deny(401, "the token is not the one this daemon published")

    return AuthResult.allow()


def health_payload(extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """The one response served without a token, so a client can detect a version mismatch."""
    payload = {"api_version": API_VERSION, "app_version": __version__, "pid": os.getpid()}
    payload.update(extra or {})
    return payload
