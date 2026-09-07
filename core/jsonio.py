"""Crash-safe JSON reads and writes.

The write is atomic in the only sense that matters here: a reader either sees the whole
previous file or the whole new one, never a truncated mixture. Power loss halfway through
rewriting ``domains.json`` must not cost the user their domain list, and losing
``netstate.json`` would cost them the record of which resolution rules to undo.

``utf-8-sig`` on read because a file a user has opened in Notepad comes back with a BOM,
and ``json.load`` rejects it.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from system import secure


def read_json(path: str | os.PathLike[str], default: Any = None) -> Any:
    """Return the parsed contents, or ``default`` when the file is missing or unreadable."""
    target = Path(path)
    try:
        with target.open("r", encoding="utf-8-sig") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return default


def write_json_atomic(path: str | os.PathLike[str], obj: Any, *, harden: bool = True) -> None:
    """Write ``obj`` as JSON, replacing ``path`` in one step.

    ``fsync`` before ``replace`` is what makes this survive a power cut rather than merely
    a crash: without it the rename can reach the disk before the data it points at.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(obj, indent=2, ensure_ascii=False)

    handle_fd, tmp_name = tempfile.mkstemp(
        dir=str(target.parent), prefix=f"{target.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(handle_fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise

    if harden:
        with contextlib.suppress(secure.PermissionWarning, OSError):
            secure.harden_file(target)
