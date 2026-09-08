"""The interactive domain screen for the console build.

Double-clicking ``hostbridge-cli.exe`` must not flash a window and vanish, which is what
happens when a console program prints and exits. This screen keeps the process alive and
repaints in place, in the shape the sibling ccas project established.

Two structural choices make it testable without a terminal:

* :func:`render` is a pure function of the data and the cursor, so what the screen looks
  like is asserted by comparing strings;
* the key loop takes its input function as an argument, so a test feeds it a scripted
  sequence of keys and inspects the store afterwards.

Text entry drops out of the repainting block deliberately. :func:`ui.screen.read_key` reads
one character and cannot edit a line, so adding a domain prints a prompt, calls ``input()``,
and then resets the surface -- ccas sent the user to the command line at this point instead,
which is worse when the console *is* the interface.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from client.api import Bridge, BridgeConflict, BridgeError
from core.match import validate
from core.model import Domain
from core.store import DomainStore
from ui.i18n import t
from ui.screen import (
    BOLD,
    CYAN,
    DIM,
    GREEN,
    RED,
    RESET,
    YELLOW,
    Surface,
    enable_ansi,
    interactive,
    read_key,
)
from ui.screen import width as terminal_width

ENABLED_MARK = "●"
DISABLED_MARK = "○"
CURSOR_MARK = "▸"

ADDRESS_WIDTH = 15
SOURCE_WIDTH = 8

#: Below this the columns stop being readable and the layout collapses to names only.
NARROW_WIDTH = 52


@dataclass
class View:
    """Everything the screen draws, gathered in one read."""

    domains: list[Domain]
    online: bool
    claimed: int = 0
    stale: bool = False

    @classmethod
    def load(cls, bridge: Bridge) -> View:
        snapshot = bridge.snapshot()
        status = snapshot.status or {}
        return cls(
            domains=sorted(snapshot.domains, key=lambda d: d.name),
            online=snapshot.online,
            claimed=len(status.get("claimed") or ()),
            stale=bool(status.get("stale")),
        )


def _truncate(text: str, limit: int) -> str:
    if limit <= 1 or len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _status_parts(view: View) -> tuple[str, str]:
    """The status as (colour, text), so the caller can measure it before painting."""
    if view.online:
        return GREEN, t("console.status_running", count=view.claimed)
    if view.stale:
        return YELLOW, t("console.status_stale")
    return DIM, t("console.status_stopped")


def _header(view: View, columns: int) -> str:
    from core.version import __version__

    title = f"HostBridge {__version__}"
    colour, text = _status_parts(view)

    # The header is measured like every other line: it wraps just as readily, and a wrapped
    # header throws off the cursor-up count for the whole screen below it.
    room = columns - len(title) - 3 - 2
    if room < 8:
        return f"{CYAN}{BOLD}{_truncate(title, columns)}{RESET}"
    return (
        f"{CYAN}{BOLD}{title}{RESET}   "
        f"{colour}{ENABLED_MARK} {_truncate(text, room)}{RESET}"
    )


def render(view: View, cursor: int, note: str = "", *, columns: int | None = None) -> list[str]:
    """The whole screen as a list of lines. Pure, so the tests compare it directly."""
    columns = columns or terminal_width()
    lines = [_header(view, columns), ""]

    if not view.domains:
        lines.append(f"  {DIM}{_truncate(t('console.empty'), columns - 2)}{RESET}")
    else:
        show_columns = columns >= NARROW_WIDTH

        # Everything except the name column is fixed width, so the name gets what is left.
        # Counted rather than estimated: " ▸ ● " is five visible cells, then two separators
        # of two spaces each around the address and source columns. An off-by-one here
        # makes the line wrap, and a wrapped line breaks draw()'s cursor-up arithmetic and
        # turns the screen into a scrolling mess.
        prefix = len(" ") + len(CURSOR_MARK) + len(" ") + len(ENABLED_MARK) + len(" ")
        tail = (2 + ADDRESS_WIDTH + 2 + SOURCE_WIDTH) if show_columns else 0
        name_width = max(8, columns - prefix - tail)

        for index, domain in enumerate(view.domains):
            pointer = CURSOR_MARK if index == cursor else " "
            mark = ENABLED_MARK if domain.enabled else DISABLED_MARK
            mark_colour = GREEN if domain.enabled else DIM
            name = _truncate(domain.name, name_width)

            body = f" {pointer} {mark_colour}{mark}{RESET} {name:<{name_width}}"
            if show_columns:
                source = t(f"console.source_{domain.source}")
                body += (
                    f"  {DIM}{domain.address:<{ADDRESS_WIDTH}}"
                    f"  {source:<{SOURCE_WIDTH}}{RESET}"
                )
            # The trailing padding of the last column is invisible but still occupies
            # cells, so it is what pushes a full-width row over the edge.
            body = body.rstrip()
            lines.append(f"{BOLD}{body}{RESET}" if index == cursor else body)

    lines.append("")
    # Truncated before the colour is added, not after: a line carrying escape sequences
    # cannot be cut by character count without slicing one of them in half.
    room = columns - 1
    lines.append(f" {DIM}{_truncate(t('console.keys_move'), room)}{RESET}")
    lines.append(f" {DIM}{_truncate(t('console.keys_act'), room)}{RESET}")
    if note:
        colour = RED if note.startswith("!") else GREEN
        lines.append(f" {colour}{_truncate(note.lstrip('!'), room)}{RESET}")
    else:
        lines.append("")
    return lines


# ---- text entry, outside the repainting block --------------------------------------


def ask_line(prompt: str, default: str = "") -> str | None:
    """Read one line. ``None`` when the user cancelled with an empty answer or Ctrl+C."""
    suffix = f" [{default}]" if default else ""
    try:
        answer = input(f" {prompt}{suffix}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    if not answer:
        return default or None
    return answer


def ask_yes(prompt: str) -> bool:
    try:
        answer = input(f" {prompt} [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return answer[:1] in ("y", "д")


def _describe(issue) -> str:
    return t(issue.key, **issue.params)


# ---- actions -----------------------------------------------------------------------


def add_domain(bridge: Bridge) -> str:
    """Prompt for a name and an address. Returns the note to show on the screen."""
    raw = ask_line(t("console.prompt_name"))
    if not raw:
        return t("console.cancelled")

    checked = validate(raw)
    if not checked.ok:
        return "!" + _describe(checked.error)

    for warning in checked.warnings:
        print(f" {YELLOW}{_describe(warning)}{RESET}")
    if checked.warnings and not ask_yes(t("console.confirm_warnings")):
        return t("console.cancelled")

    address = ask_line(t("console.prompt_address"), "127.0.0.1")
    if not address:
        return t("console.cancelled")

    try:
        bridge.add(checked.name, address)
    except BridgeConflict as exc:
        return "!" + t("console.duplicate", name=exc.name)
    except BridgeError as exc:
        return "!" + t("console.store_busy", error=exc)
    return t("console.added", name=checked.name)


def edit_domain(bridge: Bridge, domain: Domain) -> str:
    address = ask_line(t("console.prompt_address"), domain.address)
    if not address:
        return t("console.cancelled")
    if address == domain.address:
        return t("console.cancelled")
    try:
        bridge.update(domain.id, address=address)
    except BridgeError as exc:
        return "!" + t("console.store_busy", error=exc)
    return t("console.saved", name=domain.name)


def delete_domain(bridge: Bridge, domain: Domain) -> str:
    if not ask_yes(t("console.confirm_delete", name=domain.name)):
        return t("console.cancelled")
    try:
        bridge.delete(domain.id)
    except BridgeError as exc:
        return "!" + t("console.store_busy", error=exc)
    return t("console.deleted", name=domain.name)


def toggle_domain(bridge: Bridge, domain: Domain) -> str:
    try:
        answer = bridge.toggle(domain.id)
    except BridgeError as exc:
        return "!" + t("console.store_busy", error=exc)
    enabled = bool((answer.get("domain") or {}).get("enabled", not domain.enabled))
    key = "console.toggled_on" if enabled else "console.toggled_off"
    return t(key, name=domain.name)


# ---- the loop ----------------------------------------------------------------------


def run(
    store: DomainStore | None = None,
    *,
    bridge: Bridge | None = None,
    keys: Callable[[], str] = read_key,
    surface: Surface | None = None,
) -> int:
    """Paint the screen and handle keys until the user quits.

    ``keys`` is injected so a test can drive the whole loop with a scripted sequence.
    """
    bridge = bridge or Bridge.connect(store)
    surface = surface or Surface()
    enable_ansi()

    view = View.load(bridge)
    cursor = 0
    note = ""

    try:
        while True:
            cursor = max(0, min(cursor, max(0, len(view.domains) - 1)))
            surface.paint(render(view, cursor, note))
            note = ""

            key = keys()
            if key in ("q", "esc"):
                return 0

            if key == "up":
                if view.domains:
                    cursor = (cursor - 1) % len(view.domains)
                continue
            if key == "down":
                if view.domains:
                    cursor = (cursor + 1) % len(view.domains)
                continue
            if key == "r":
                view = View.load(bridge)
                note = t("console.refreshed")
                continue

            if key == "a":
                # Out of the repainting block: input() writes its own prompt and scrolls,
                # which would leave the surface's height arithmetic pointing at nothing.
                surface.reset()
                note = add_domain(bridge)
                view = View.load(bridge)
                continue

            if not view.domains:
                continue
            current = view.domains[cursor]

            if key == "space":
                note = toggle_domain(bridge, current)
                view = View.load(bridge)
            elif key == "e":
                surface.reset()
                note = edit_domain(bridge, current)
                view = View.load(bridge)
            elif key in ("d", "del"):
                surface.reset()
                note = delete_domain(bridge, current)
                view = View.load(bridge)
    except KeyboardInterrupt:
        print()
        return 0


def show_plain(store: DomainStore | None = None, bridge: Bridge | None = None) -> int:
    """The non-interactive fallback: print the state and return, never blocking on input.

    Reached when stdin or stdout is redirected. Without this branch a piped or CI-invoked
    run would hang forever waiting for a keypress nobody can send.
    """
    from app.output import emit
    from daemon.status import describe

    bridge = bridge or Bridge.connect(store)
    view = View.load(bridge)
    for line in describe():
        emit(line)
    if view.domains:
        emit("")
        for domain in view.domains:
            mark = "+" if domain.enabled else "-"
            emit(f"  {mark} {domain.name}  {domain.address}  {domain.source}")
    return 0


def main(store: DomainStore | None = None, bridge: Bridge | None = None) -> int:
    bridge = bridge or Bridge.connect(store)
    return run(bridge=bridge) if interactive() else show_plain(bridge=bridge)
