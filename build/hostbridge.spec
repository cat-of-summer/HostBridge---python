# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the hostbridge binaries.

Artifacts are named ``hostbridge-<os>-<arch>`` so a GitHub release can carry Windows and
Linux builds side by side. Set HOSTBRIDGE_ARTIFACT_NAME to override.

Two decisions here are load-bearing:

* **onedir, not onefile.** The release step in ``.github/workflows/ci-cd.yml`` stages assets
  behind ``[ -f "$f" ] || continue``, so a directory never becomes a release asset -- the
  build script zips this output. onefile would also be wrong for the service: its
  bootloader unpacks to a temporary directory and re-execs a child process, which confuses
  the Windows SCM and strands ``_MEI…`` directories when the service is killed hard.

* **Two executables from one Analysis.** ``hostbridge`` is windowed, because a GUI that
  flashes a console window on every subprocess is unusable; ``hostbridge-cli`` is a console
  build of the same entry point so that ``--repair`` and ``--status`` can print somewhere a
  user can read. Sharing the Analysis means the Python payload is collected once.
"""

import os
import platform
import sys
from pathlib import Path

ROOT = Path(os.path.dirname(os.path.abspath(SPEC))).parent

PROJECT = "hostbridge"

OS_NAMES = {
    "win32": "windows",
    "cygwin": "windows",
    "darwin": "macos",
    "linux": "linux",
}

ARCH_NAMES = {
    "amd64": "x64",
    "x86_64": "x64",
    "x64": "x64",
    "i386": "x86",
    "i686": "x86",
    "x86": "x86",
    "aarch64": "arm64",
    "arm64": "arm64",
    "armv7l": "arm",
}

PACKAGES = ("app", "client", "core", "daemon", "discover", "resolver", "system", "ui")


def resolve_os():
    for prefix, name in OS_NAMES.items():
        if sys.platform.startswith(prefix):
            return name
    return sys.platform


def resolve_arch():
    machine = platform.machine().lower()
    return ARCH_NAMES.get(machine, machine or "unknown")


ARTIFACT_NAME = (
    os.environ.get("HOSTBRIDGE_ARTIFACT_NAME")
    or f"{PROJECT}-{resolve_os()}-{resolve_arch()}"
)

# Everything looked up through core.paths.resource_dir has to be bundled, because those
# directories do not exist next to the collected binary.
DATAS = []
DATAS += [(str(p), "lang") for p in sorted((ROOT / "lang").glob("*.json"))]
DATAS += [(str(p), "assets") for p in sorted((ROOT / "assets").glob("*")) if p.is_file()]


def discover_modules():
    """Every first-party module, walked from the tree rather than listed by hand.

    Imports are deferred into functions throughout this codebase for start-up time, which
    means PyInstaller's static analysis cannot see most of them. Enumerating the tree keeps
    that list from drifting as milestones land.
    """
    found = []
    for package in PACKAGES:
        directory = ROOT / package
        if not directory.is_dir():
            continue
        found.append(package)
        for path in sorted(directory.rglob("*.py")):
            relative = path.relative_to(ROOT).with_suffix("")
            parts = relative.parts
            if parts[-1] == "__init__":
                parts = parts[:-1]
            if parts:
                found.append(".".join(parts))
    return sorted(set(found))


HIDDEN = discover_modules()
HIDDEN += ["dnslib", "dnslib.dns", "dnslib.server"]
if sys.platform.startswith("win"):
    # The SCM handshake. Without a dispatcher the service fails with error 1053.
    HIDDEN += ["win32serviceutil", "win32service", "win32event", "servicemanager"]

EXCLUDES = [
    "tkinter",
    "test",
    "lib2to3",
    "pydoc_data",
    "numpy",
    "PIL",
    "pytest",
    "setuptools",
    # Qt modules this application never touches. Keeping only QtCore/QtGui/QtWidgets/QtSvg
    # is worth well over a hundred megabytes in the collected output.
    "PySide6.Qt3DAnimation",
    "PySide6.Qt3DCore",
    "PySide6.Qt3DExtras",
    "PySide6.Qt3DInput",
    "PySide6.Qt3DLogic",
    "PySide6.Qt3DRender",
    "PySide6.QtBluetooth",
    "PySide6.QtCharts",
    "PySide6.QtDataVisualization",
    "PySide6.QtDesigner",
    "PySide6.QtHelp",
    "PySide6.QtMultimedia",
    "PySide6.QtMultimediaWidgets",
    "PySide6.QtNfc",
    "PySide6.QtOpenGL",
    "PySide6.QtOpenGLWidgets",
    "PySide6.QtPdf",
    "PySide6.QtPdfWidgets",
    "PySide6.QtPositioning",
    "PySide6.QtQml",
    "PySide6.QtQuick",
    "PySide6.QtQuick3D",
    "PySide6.QtQuickControls2",
    "PySide6.QtQuickWidgets",
    "PySide6.QtRemoteObjects",
    "PySide6.QtScxml",
    "PySide6.QtSensors",
    "PySide6.QtSerialPort",
    "PySide6.QtSpatialAudio",
    "PySide6.QtSql",
    "PySide6.QtStateMachine",
    "PySide6.QtTest",
    "PySide6.QtTextToSpeech",
    "PySide6.QtWebChannel",
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineQuick",
    "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebSockets",
]

ICON = ROOT / "assets" / f"{PROJECT}.ico"
ICON_ARG = str(ICON) if ICON.exists() else None

analysis = Analysis(
    [str(ROOT / "main.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=DATAS,
    hiddenimports=HIDDEN,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
)

pyz = PYZ(analysis.pure, analysis.zipped_data)

windowed = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name=PROJECT,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=ICON_ARG,
)

console = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name=f"{PROJECT}-cli",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=ICON_ARG,
)

COLLECT(
    windowed,
    console,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=ARTIFACT_NAME,
)
