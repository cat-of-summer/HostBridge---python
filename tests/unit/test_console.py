"""The console screen, tested without a terminal.

The rendering is a pure function so it can be compared as strings, and the key loop takes
its input function as an argument so a scripted sequence can drive it. Neither test needs a
TTY, which matters because the whole suite runs in a container.
"""

from __future__ import annotations

import sys

import pytest

from core.model import SOURCE_MANUAL, SOURCE_TRAEFIK, Domain
from core.store import DomainStore
from ui import console, screen
from ui.console import View, render


class FakeSurface:
    """Records frames instead of writing escape codes to a terminal."""

    def __init__(self) -> None:
        self.frames: list[list[str]] = []
        self.resets = 0

    def paint(self, lines: list[str]) -> None:
        self.frames.append(lines)

    def reset(self) -> None:
        self.resets += 1


def _plain(lines: list[str]) -> list[str]:
    """Strip ANSI so a test asserts on text rather than on colour."""
    import re

    return [re.sub(r"\033\[[0-9;]*m", "", line) for line in lines]


@pytest.fixture
def store() -> DomainStore:
    store = DomainStore()
    store.add(Domain(name="etm39.ru", address="127.0.0.1", source=SOURCE_MANUAL))
    store.reconcile(
        SOURCE_TRAEFIK,
        [Domain(name="api.shop.test", address="127.0.0.1", source=SOURCE_TRAEFIK, owner="r1")],
    )
    return store


def _keys(sequence):
    """An input function that replays a sequence and then quits."""
    remaining = list(sequence)

    def _next() -> str:
        return remaining.pop(0) if remaining else "q"

    return _next


# ---- rendering ---------------------------------------------------------------------


def test_the_cursor_marks_the_selected_row(store):
    lines = _plain(render(View.load(store), cursor=0, columns=100))
    body = [line for line in lines if "etm39.ru" in line or "api.shop.test" in line]
    assert body[0].lstrip().startswith("▸"), "the first row is selected"
    assert not body[1].lstrip().startswith("▸")


def test_enabled_and_disabled_use_different_marks(store):
    target = store.load().domains[0]
    store.toggle(target.id, enabled=False)

    lines = _plain(render(View.load(store), cursor=0, columns=100))
    row = next(line for line in lines if target.name in line)
    assert "○" in row
    assert "●" not in row


def test_the_source_column_is_shown_on_a_wide_terminal(store):
    lines = _plain(render(View.load(store), cursor=0, columns=100))
    assert any("traefik" in line for line in lines)
    assert any("manual" in line for line in lines)


def test_a_narrow_terminal_drops_the_columns_rather_than_wrapping(store):
    """A line longer than the terminal wraps, and that breaks the cursor-up arithmetic."""
    lines = render(View.load(store), cursor=0, columns=40)
    for line in _plain(lines):
        assert len(line) <= 40, f"line would wrap: {line!r}"


def test_a_long_name_is_truncated_not_wrapped():
    store = DomainStore()
    store.add(Domain(name="a" * 80 + ".test"))
    for line in _plain(render(View.load(store), cursor=0, columns=60)):
        assert len(line) <= 60


def test_an_empty_store_says_so_instead_of_showing_nothing():
    lines = _plain(render(View.load(DomainStore()), cursor=0, columns=100))
    assert any("No domains yet" in line for line in lines)


def test_a_note_is_rendered(store):
    lines = _plain(render(View.load(store), cursor=0, note="etm39.ru on", columns=100))
    assert any("etm39.ru on" in line for line in lines)


def test_an_error_note_loses_its_marker(store):
    lines = _plain(render(View.load(store), cursor=0, note="!something broke", columns=100))
    assert any(line.strip() == "something broke" for line in lines)


def test_the_frame_height_is_stable_so_the_repaint_lands(store):
    """draw() moves the cursor up by the previous height; a changing height would drift."""
    first = render(View.load(store), cursor=0, columns=100)
    second = render(View.load(store), cursor=1, note="something", columns=100)
    assert len(first) == len(second)


# ---- the key loop ------------------------------------------------------------------


def test_q_quits_immediately(store):
    surface = FakeSurface()
    assert console.run(store, keys=_keys(["q"]), surface=surface) == 0
    assert len(surface.frames) == 1


def test_the_arrows_move_the_cursor_and_wrap(store):
    surface = FakeSurface()
    console.run(store, keys=_keys(["down", "down", "up", "q"]), surface=surface)

    selected = []
    for frame in surface.frames:
        row = next(line for line in _plain(frame) if line.lstrip().startswith("▸"))
        # " ▸ ● name ..." -- the pointer and the on/off mark come first.
        selected.append(row.split()[2])
    # Two domains, sorted: api.shop.test then etm39.ru. Down twice wraps back to the first.
    assert selected == ["api.shop.test", "etm39.ru", "api.shop.test", "etm39.ru"]


def test_space_toggles_the_selected_domain(store):
    before = {d.name: d.enabled for d in store.load().domains}
    assert all(before.values())

    console.run(store, keys=_keys(["space", "q"]), surface=FakeSurface())

    after = {d.name: d.enabled for d in store.load().domains}
    assert after["api.shop.test"] is False
    assert after["etm39.ru"] is True, "only the selected row is touched"


def test_the_toggle_is_reported_in_the_note(store):
    surface = FakeSurface()
    console.run(store, keys=_keys(["space", "q"]), surface=surface)
    assert any("api.shop.test off" in line for line in _plain(surface.frames[-1]))


def test_delete_asks_first_and_a_refusal_keeps_the_domain(store, monkeypatch):
    monkeypatch.setattr(console, "ask_yes", lambda _prompt: False)
    console.run(store, keys=_keys(["d", "q"]), surface=FakeSurface())
    assert len(store.load().domains) == 2


def test_delete_removes_the_domain_once_confirmed(store, monkeypatch):
    monkeypatch.setattr(console, "ask_yes", lambda _prompt: True)
    console.run(store, keys=_keys(["d", "q"]), surface=FakeSurface())
    assert [d.name for d in store.load().domains] == ["etm39.ru"]


def test_adding_a_domain_reads_a_line_and_saves_it(store, monkeypatch):
    answers = iter(["shop.test", "127.0.0.9"])
    monkeypatch.setattr(console, "ask_line", lambda _prompt, default="": next(answers))

    surface = FakeSurface()
    console.run(store, keys=_keys(["a", "q"]), surface=surface)

    created = store.load().by_name("shop.test")
    assert created is not None
    assert created.address == "127.0.0.9"
    assert created.source == SOURCE_MANUAL
    assert surface.resets == 1, "text entry must leave the repainting block"


def test_an_invalid_name_is_reported_and_nothing_is_saved(store, monkeypatch):
    monkeypatch.setattr(console, "ask_line", lambda _prompt, default="": "not a domain")

    surface = FakeSurface()
    console.run(store, keys=_keys(["a", "q"]), surface=surface)

    assert len(store.load().domains) == 2
    assert any("not allowed" in line for line in _plain(surface.frames[-1]))


def test_a_warning_asks_for_confirmation_and_a_refusal_cancels(store, monkeypatch):
    """.local warns but must not be blocked outright -- the user gets the choice."""
    monkeypatch.setattr(console, "ask_line", lambda _prompt, default="": "shop.local")
    monkeypatch.setattr(console, "ask_yes", lambda _prompt: False)

    console.run(store, keys=_keys(["a", "q"]), surface=FakeSurface())
    assert store.load().by_name("shop.local") is None


def test_a_warning_accepted_saves_the_domain(store, monkeypatch, capsys):
    answers = iter(["shop.local", "127.0.0.1"])
    monkeypatch.setattr(console, "ask_line", lambda _prompt, default="": next(answers))
    monkeypatch.setattr(console, "ask_yes", lambda _prompt: True)

    console.run(store, keys=_keys(["a", "q"]), surface=FakeSurface())
    assert store.load().by_name("shop.local") is not None
    assert "mDNS" in capsys.readouterr().out, "the warning has to be shown, not just counted"


def test_a_duplicate_name_is_reported(store, monkeypatch):
    monkeypatch.setattr(console, "ask_line", lambda _prompt, default="": "etm39.ru")

    surface = FakeSurface()
    console.run(store, keys=_keys(["a", "q"]), surface=surface)
    assert any("already in the list" in line for line in _plain(surface.frames[-1]))


def test_keys_that_need_a_selection_do_nothing_on_an_empty_store():
    empty = DomainStore()
    surface = FakeSurface()
    assert console.run(empty, keys=_keys(["space", "d", "e", "down", "up", "q"]),
                       surface=surface) == 0


def test_ctrl_c_leaves_the_screen_without_an_error(store):
    def _interrupt() -> str:
        raise KeyboardInterrupt

    assert console.run(store, keys=_interrupt, surface=FakeSurface()) == 0


# ---- the non-interactive fallback --------------------------------------------------


def test_without_a_tty_the_plain_listing_is_printed_and_no_key_is_read(store, monkeypatch, capsys):
    """A piped or CI-invoked run must not block forever waiting for a keypress."""

    def _explode() -> str:
        raise AssertionError("read_key must not be called without a terminal")

    monkeypatch.setattr(screen, "interactive", lambda: False)
    monkeypatch.setattr(console, "read_key", _explode)

    assert console.main(store) == 0
    printed = capsys.readouterr().out
    assert "etm39.ru" in printed
    assert "Resolver" in printed


# ---- the screen primitives ---------------------------------------------------------


def test_draw_moves_the_cursor_up_by_the_previous_height(capsys):
    height = screen.draw(["one", "two"], previous_height=0)
    assert height == 2
    first = capsys.readouterr().out
    assert not first.startswith("\033["), "nothing to move up over on the first paint"

    screen.draw(["one", "two"], previous_height=2)
    second = capsys.readouterr().out
    assert second.startswith("\033[2A")


def test_each_drawn_line_is_erased_before_it_is_written(capsys):
    """Without the erase, a shorter line leaves the tail of the longer one behind."""
    screen.draw(["short"], previous_height=0)
    assert "\r\033[K" in capsys.readouterr().out


def test_the_surface_tracks_its_height_across_paints(capsys):
    surface = screen.Surface()
    surface.paint(["a", "b", "c"])
    capsys.readouterr()
    surface.paint(["a", "b", "c"])
    assert capsys.readouterr().out.startswith("\033[3A")


def test_reset_makes_the_next_paint_start_fresh(capsys):
    surface = screen.Surface()
    surface.paint(["a", "b"])
    surface.reset()
    capsys.readouterr()
    surface.paint(["a", "b"])
    assert not capsys.readouterr().out.startswith("\033[")


def test_interactive_is_false_when_a_stream_is_missing(monkeypatch):
    """The windowed build has no standard streams; calling isatty() on one would crash."""
    monkeypatch.setattr(sys, "stdin", None)
    assert screen.interactive() is False


def test_interactive_is_false_on_a_closed_stream(monkeypatch, tmp_path):
    handle = (tmp_path / "closed").open("w")
    handle.close()
    monkeypatch.setattr(sys, "stdout", handle)
    assert screen.interactive() is False


def test_enable_ansi_is_a_no_op_off_windows():
    # Must not raise, and the autouse guard proves it starts no process.
    screen.enable_ansi()
