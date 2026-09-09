#!/usr/bin/env bash
# Build the hostbridge binaries into dist/ and zip them for release.
#
#   SKIP_TESTS=true   build without running the test suite
#   PYTHON=python3.12 use a specific interpreter
#
# Must stay POSIX-runnable under Git Bash, because every step of the CI workflow uses
# `shell: bash` and that is Git Bash on windows-latest.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

python="${PYTHON:-}"
if [ -z "$python" ]; then
    for candidate in python3 python py; do
        if command -v "$candidate" >/dev/null 2>&1; then
            python="$candidate"
            break
        fi
    done
fi
[ -n "$python" ] || { echo "Python not found. Install Python 3.10+ and retry." >&2; exit 1; }

echo "Python: $("$python" --version)"

scratch="$root/build/__pycache__"
export PYTHONPYCACHEPREFIX="$scratch"

# Editable so PyInstaller and pytest both work against this source tree rather than an
# installed copy. Dependencies come from pyproject.toml, the single place they live.
"$python" -m pip install --upgrade --quiet -e ".[dev]"

if [ "${SKIP_TESTS:-false}" != "true" ]; then
    # Docker- and privilege-backed tests are excluded: a release runner has neither.
    "$python" -m pytest -m "not docker and not admin" -q
fi

rm -rf dist
"$python" -m PyInstaller --clean --noconfirm \
    --distpath dist --workpath "$scratch" build/hostbridge.spec

# The release step stages assets behind `[ -f "$f" ]`, so the collected directory has to
# become a single file. `zip` does not exist in Git Bash, hence the stdlib module -- one
# interpreter, both runners.
artifact="$("$python" -c "import os,sys,platform
os_names={'win32':'windows','cygwin':'windows','darwin':'macos','linux':'linux'}
arch_names={'amd64':'x64','x86_64':'x64','x64':'x64','i386':'x86','i686':'x86','x86':'x86','aarch64':'arm64','arm64':'arm64','armv7l':'arm'}
name=next((v for k,v in os_names.items() if sys.platform.startswith(k)), sys.platform)
arch=arch_names.get(platform.machine().lower(), platform.machine().lower() or 'unknown')
print(os.environ.get('HOSTBRIDGE_ARTIFACT_NAME') or f'hostbridge-{name}-{arch}')")"

[ -d "dist/$artifact" ] || { echo "PyInstaller produced no dist/$artifact" >&2; exit 1; }

# Qt has to be genuinely collected, and this check exists because the failure it catches is
# silent. On a machine without the Qt system libraries PyInstaller cannot introspect
# PySide6: it prints "failed to obtain Qt library info" as a *warning*, exits 0, and ships
# an archive with no platform plugin in it. The build looks fine and the GUI cannot start.
# Measured on a bare python:3.12-slim: 104 files and zero plugins, against 333 files and ten
# plugins once libegl1, libglib2.0-0, libxkbcommon0, libdbus-1-3, libfontconfig1 and
# libgssapi-krb5-2 are installed.
if ! find "dist/$artifact" -type d -name platforms -print -quit | grep -q .; then
    echo "The bundle contains no Qt platform plugin, so its GUI cannot start." >&2
    echo "On Linux install: libegl1 libgl1 libglib2.0-0 libxkbcommon0 libdbus-1-3 \\" >&2
    echo "                  libfontconfig1 libgssapi-krb5-2" >&2
    exit 1
fi

# The release step stages assets behind `[ -f "$f" ]`, so the collected directory has to
# become a single file.
#
# tar.gz on POSIX rather than zip, because zip does not carry the executable bit: unpacking
# a zip would leave the user with a binary they have to chmod +x before it runs. On Windows
# the bit does not exist and zip is what Explorer opens natively.
#
# No checksum file is written: the release pipeline records its own digest for every asset,
# and a second one maintained here would only ever be the one that goes stale.
"$python" - "$artifact" <<'PY'
import os
import pathlib
import sys
import tarfile
import zipfile

name = sys.argv[1]
dist = pathlib.Path("dist")
source = dist / name

if os.name == "nt":
    archive = dist / f"{name}.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
        for path in sorted(source.rglob("*")):
            if path.is_file():
                bundle.write(path, path.relative_to(dist).as_posix())
else:
    archive = dist / f"{name}.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        bundle.add(source, arcname=name)

print(f"packed {archive} ({archive.stat().st_size / 1048576:.1f} MiB)")
PY

echo
echo "Artifacts in $root/dist:"
ls -la dist/
