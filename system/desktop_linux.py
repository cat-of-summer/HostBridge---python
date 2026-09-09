"""Desktop entries: the application menu, and starting with the session.

Written as a planner like its siblings -- it renders text and names paths, and a separate
step puts them on disk -- so the exact bytes that land in a user's home can be compared in
a golden test without creating anything.

**Per-user, not system-wide.** ``/usr/share/applications`` would need root, and the entry
launches the *window*, which runs as the user and never elevates. Asking for a password to
add a menu shortcut would be the wrong trade; ``~/.local/share/applications`` needs none and
is what the XDG spec has for exactly this.

Autostart is a second copy of the same entry in ``~/.config/autostart``. That is the whole
mechanism -- every desktop that follows the XDG autostart spec reads that directory, so
there is nothing per-desktop to detect. It starts the window, not the resolver: the resolver
belongs to the systemd unit, which is what survives a reboot with no one logged in.
"""

from __future__ import annotations

import os
import shlex
from dataclasses import dataclass, field
from pathlib import Path

APPLICATION_ID = "hostbridge"
ENTRY_NAME = f"{APPLICATION_ID}.desktop"

#: The keys are fixed by the XDG Desktop Entry specification; the values are ours.
ENTRY_TEMPLATE = """\
[Desktop Entry]
Type=Application
Version=1.0
Name=HostBridge
GenericName=Local DNS manager
Comment=Local DNS for development domains
Exec={exec_line}
{icon_line}Terminal=false
Categories=Development;Network;Utility;
Keywords=dns;domain;docker;traefik;
StartupNotify=true
StartupWMClass=hostbridge
X-GNOME-Autostart-enabled=true
"""


@dataclass
class DesktopPlan:
    """Files to write and files to remove. Nothing here touches the disk."""

    write: list[tuple[Path, str]] = field(default_factory=list)
    remove: list[Path] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.write and not self.remove


def _home(variable: str, fallback: str) -> Path:
    """An XDG base directory, honouring the environment as the spec requires.

    A relative value in the variable is ignored rather than resolved against the working
    directory: the spec says so, and a stray entry written into whatever directory the app
    happened to start in would be invisible and never cleaned up.
    """
    configured = os.environ.get(variable, "").strip()
    if configured and configured.startswith("/"):
        return Path(configured)
    return Path.home() / fallback


def applications_dir() -> Path:
    return _home("XDG_DATA_HOME", ".local/share") / "applications"


def autostart_dir() -> Path:
    return _home("XDG_CONFIG_HOME", ".config") / "autostart"


def render_entry(executable: str, icon: str = "") -> str:
    """The entry's text.

    ``Exec`` is quoted through :func:`shlex.quote` because the spec's own escaping rules and
    the shell's disagree, and a path with a space in it -- an AppImage in ``~/My Apps``, say
    -- otherwise produces an entry the desktop silently refuses to launch.
    """
    icon_line = f"Icon={icon}\n" if icon else ""
    return ENTRY_TEMPLATE.format(exec_line=shlex.quote(executable), icon_line=icon_line)


def plan_install(executable: str, *, icon: str = "", autostart: bool = True) -> DesktopPlan:
    text = render_entry(executable, icon)
    plan = DesktopPlan(write=[(applications_dir() / ENTRY_NAME, text)])
    if autostart:
        plan.write.append((autostart_dir() / ENTRY_NAME, text))
    return plan


def plan_uninstall() -> DesktopPlan:
    return DesktopPlan(remove=[applications_dir() / ENTRY_NAME, autostart_dir() / ENTRY_NAME])


def autostart_enabled() -> bool:
    return (autostart_dir() / ENTRY_NAME).is_file()


def installed() -> bool:
    return (applications_dir() / ENTRY_NAME).is_file()


def execute(plan: DesktopPlan) -> list[Path]:
    """Carry out a plan, returning the paths that ended up changed.

    Entries are written with a trailing newline and mode 0644 -- a desktop entry that is not
    world-readable is skipped silently by some launchers, which is a maddening thing to
    debug for the sake of a permission nobody wanted.
    """
    touched: list[Path] = []
    for path, text in plan.write:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
        os.chmod(path, 0o644)
        touched.append(path)
    for path in plan.remove:
        try:
            path.unlink()
        except FileNotFoundError:
            continue
        except OSError:
            continue
        touched.append(path)
    return touched
