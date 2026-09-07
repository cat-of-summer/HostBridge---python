<#
.SYNOPSIS
    Build the hostbridge binaries into dist/ and zip them for release.

.DESCRIPTION
    Twin of build/build.sh for developers who would rather not open Git Bash. CI always
    uses the shell script, because every workflow step runs under `shell: bash`.

.PARAMETER SkipTests
    Build without running the test suite.

.PARAMETER Python
    Use a specific interpreter instead of probing for one.
#>
[CmdletBinding()]
param(
    [switch]$SkipTests,
    [string]$Python = ""
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

if (-not $Python) {
    foreach ($candidate in @("python", "python3", "py")) {
        $found = Get-Command $candidate -ErrorAction SilentlyContinue
        if ($found) { $Python = $found.Source; break }
    }
}
if (-not $Python) { throw "Python not found. Install Python 3.10+ and retry." }

Write-Output "Python: $(& $Python --version)"

$scratch = Join-Path $root "build\__pycache__"
$env:PYTHONPYCACHEPREFIX = $scratch

# Editable so PyInstaller and pytest both work against this source tree rather than an
# installed copy. Dependencies come from pyproject.toml, the single place they live.
& $Python -m pip install --upgrade --quiet -e ".[dev]"
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }

if (-not $SkipTests) {
    & $Python -m pytest -m "not docker and not admin" -q
    if ($LASTEXITCODE -ne 0) { throw "tests failed" }
}

if (Test-Path "dist") { Remove-Item -Recurse -Force "dist" }
& $Python -m PyInstaller --clean --noconfirm --distpath dist --workpath $scratch build\hostbridge.spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

$artifact = $env:HOSTBRIDGE_ARTIFACT_NAME
if (-not $artifact) {
    $arch = switch -Wildcard ($env:PROCESSOR_ARCHITECTURE) {
        "AMD64" { "x64" }
        "ARM64" { "arm64" }
        "x86"   { "x86" }
        default { $env:PROCESSOR_ARCHITECTURE.ToLower() }
    }
    $artifact = "hostbridge-windows-$arch"
}

$collected = Join-Path "dist" $artifact
if (-not (Test-Path $collected)) { throw "PyInstaller produced no $collected" }

$archive = "$collected.zip"
Compress-Archive -Path $collected -DestinationPath $archive -Force
# No checksum file is written: the release pipeline records its own digest for every
# asset, and a second one maintained here would only ever be the one that goes stale.

Write-Output ""
Write-Output "Artifacts in $root\dist:"
Get-ChildItem dist | Format-Table -AutoSize Name, Length
