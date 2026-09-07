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

( cd dist && "$python" -m zipfile -c "${artifact}.zip" "$artifact" )

# No checksum file is written: the release pipeline records its own digest for every
# asset, and a second one maintained here would only ever be the one that goes stale.

echo
echo "Artifacts in $root/dist:"
ls -la dist/
