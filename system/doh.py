r"""Is a browser resolving names over HTTPS, behind our back?

This is the failure mode with the worst symptom in the whole project: the resolver runs, the
rules are registered, ``nslookup`` answers correctly, the application shows everything green
-- and one browser still cannot open the domain, because it never asked the operating system.
It sent the query to a DoH endpoint over port 443 instead.

Nothing here changes a setting. Browser configuration belongs to the user, and a tool that
edited Chrome's ``Local State`` behind their back would deserve everything it got. We read,
we report, and the interface says which switch to flip.

**Chrome and Edge** keep it in ``Local State`` as ``dns_over_https.mode``:

* ``off`` -- fine.
* ``automatic`` -- also fine, and worth stating because it looks alarming. Chrome upgrades
  only when the system resolver is a *known* DoH provider; ``127.0.0.1`` is not on that
  list, so it stays on the system path. This is why the default configuration works.
* ``secure`` -- the problem. Every name goes to the configured endpoint, and we do not exist.

**Firefox** uses ``network.trr.mode`` in ``prefs.js``:

* ``0`` and ``5`` -- off, fine.
* ``2`` -- "TRR first". Sounds like it falls back to us, and does not: the fallback is for
  network *failures*, and a DoH server answering NXDOMAIN for ``shop.test`` is a perfectly
  successful response. So this breaks us too.
* ``3`` -- TRR only. Breaks us, obviously.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

#: Chrome's values, as they appear in Local State.
CHROME_SAFE_MODES = ("off", "automatic")

#: network.trr.mode values that leave the system resolver in charge.
FIREFOX_SAFE_MODES = (0, 5)

_TRR_MODE = re.compile(r'user_pref\(\s*"network\.trr\.mode"\s*,\s*(\d+)\s*\)')


@dataclass(frozen=True)
class Finding:
    """One browser profile and what it is set to."""

    browser: str
    profile: str
    mode: str
    secure: bool
    """True when this setting stops the browser from using our resolver."""

    path: str = ""


def _local_app_data() -> Path | None:
    value = os.environ.get("LOCALAPPDATA", "")
    return Path(value) if value else None


def _chromium_roots() -> Iterator[tuple[str, Path]]:
    """Where each Chromium browser keeps ``Local State`` on this platform."""
    if os.name == "nt":
        base = _local_app_data()
        if base is None:
            return
        yield "Chrome", base / "Google" / "Chrome" / "User Data"
        yield "Edge", base / "Microsoft" / "Edge" / "User Data"
        yield "Brave", base / "BraveSoftware" / "Brave-Browser" / "User Data"
        return

    config = Path.home() / ".config"
    yield "Chrome", config / "google-chrome"
    yield "Chromium", config / "chromium"
    yield "Edge", config / "microsoft-edge"
    yield "Brave", config / "BraveSoftware" / "Brave-Browser"


def _firefox_roots() -> Iterator[Path]:
    if os.name == "nt":
        appdata = os.environ.get("APPDATA", "")
        if appdata:
            yield Path(appdata) / "Mozilla" / "Firefox" / "Profiles"
        return
    yield Path.home() / ".mozilla" / "firefox"
    # Flatpak and snap keep their own home, and a user with both gets two answers.
    yield Path.home() / ".var" / "app" / "org.mozilla.firefox" / ".mozilla" / "firefox"
    yield Path.home() / "snap" / "firefox" / "common" / ".mozilla" / "firefox"


def read_chromium(name: str, root: Path) -> Finding | None:
    path = root / "Local State"
    try:
        raw = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None

    section = raw.get("dns_over_https")
    mode = section.get("mode") if isinstance(section, dict) else None
    if not isinstance(mode, str) or not mode:
        # Absent means the browser has never been told otherwise, which is "automatic".
        mode = "automatic"

    return Finding(
        browser=name,
        profile="",
        mode=mode,
        secure=mode not in CHROME_SAFE_MODES,
        path=str(path),
    )


def read_firefox_profile(path: Path) -> Finding | None:
    """Read one profile's ``prefs.js``.

    A missing preference means the default, and Firefox's default is off. Only an explicit
    ``user_pref`` is reported, so an untouched profile produces nothing rather than noise.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    match = _TRR_MODE.search(text)
    if match is None:
        return None

    mode = int(match.group(1))
    return Finding(
        browser="Firefox",
        profile=path.parent.name,
        mode=str(mode),
        secure=mode not in FIREFOX_SAFE_MODES,
        path=str(path),
    )


def scan() -> list[Finding]:
    """Every browser profile we can read, whether or not it is a problem.

    Safe settings are returned too, because "we looked at Chrome and it is fine" is a more
    useful thing for a diagnostics panel to say than silence, which is indistinguishable
    from not having looked.
    """
    found: list[Finding] = []

    for name, root in _chromium_roots():
        if not root.is_dir():
            continue
        finding = read_chromium(name, root)
        if finding is not None:
            found.append(finding)

    for root in _firefox_roots():
        if not root.is_dir():
            continue
        for prefs in sorted(root.glob("*/prefs.js")):
            finding = read_firefox_profile(prefs)
            if finding is not None:
                found.append(finding)

    return found


def problems(findings: list[Finding] | None = None) -> list[Finding]:
    return [f for f in (scan() if findings is None else findings) if f.secure]
