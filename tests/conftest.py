"""Test isolation.

Two things must be true of every test in this suite, and both are enforced here rather
than remembered by each author:

1. No test reads or writes the developer's real state directories.
2. No test starts a real external process. This application's whole job is rewriting the
   machine's name-resolution policy, so a stray ``netsh`` escaping from a unit test would
   reconfigure the DNS of whoever ran ``pytest``.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """Point both state directories at ``tmp_path`` and pin the language to English."""
    monkeypatch.setenv("HOSTBRIDGE_HOME", str(tmp_path / "user"))
    monkeypatch.setenv("HOSTBRIDGE_MACHINE_HOME", str(tmp_path / "machine"))
    monkeypatch.setenv("HOSTBRIDGE_LANG", "en")

    from ui import i18n

    i18n.set_language("en")
    yield
    i18n.set_language("en")


@pytest.fixture(autouse=True)
def no_subprocess(monkeypatch, request):
    """Make :func:`system.run._spawn` raise unless the test asked for ``allow_run``.

    Patching the single seam works no matter how a module imported ``run``, because ``run``
    resolves ``_spawn`` through the module globals at call time.
    """
    if "allow_run" in request.fixturenames:
        return

    from system import run as run_module

    def _refuse(argv, **_kwargs):
        raise AssertionError(
            f"a test tried to start a real process: {' '.join(argv)}. "
            "Request the 'allow_run' fixture if that is genuinely intended."
        )

    monkeypatch.setattr(run_module, "_spawn", _refuse)


@pytest.fixture
def allow_run():
    """Opt out of :func:`no_subprocess`. Pair with a marker such as ``admin``."""
    return True


@pytest.fixture
def fake_spawn(monkeypatch):
    """Replace the subprocess seam with a recorder returning a scripted result."""
    from system import run as run_module

    calls: list[tuple[str, ...]] = []

    class _Result:
        def __init__(self, returncode: int, stdout: str, stderr: str) -> None:
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def install(returncode: int = 0, stdout: str = "", stderr: str = ""):
        def _spawn(argv, **_kwargs):
            calls.append(tuple(argv))
            return _Result(returncode, stdout, stderr)

        monkeypatch.setattr(run_module, "_spawn", _spawn)
        return calls

    install.calls = calls
    return install
