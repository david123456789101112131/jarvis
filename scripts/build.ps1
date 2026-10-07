<#
.SYNOPSIS
    Builds JARVIS for Windows: the program folder dist\JARVIS (JARVIS.exe and jarvis-cli.exe) and, when
    Inno Setup 6 is installed, the installer dist\JARVIS-Setup-<version>.exe. Needs `setup.bat -Dev`
    first (it installs PyInstaller and the test tools into .venv).

.PARAMETER SkipTests
    Do not run the test suite before building.

.PARAMETER SkipInstaller
    Build the program folder only.
#>
param([switch]$SkipTests, [switch]$SkipInstaller)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

function Write-Step([string]$Text) { Write-Host "`n==> $Text" -ForegroundColor Cyan }

function Invoke-Native([string]$What, [string]$Exe, [string[]]$Arguments) {
    & $Exe @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$What failed (exit code $LASTEXITCODE)." }
}

# True when the command succeeds; its output (and errors) are not shown.
function Test-Native([string]$Exe, [string[]]$Arguments) {
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { & $Exe @Arguments 2>&1 | Out-Null; return ($LASTEXITCODE -eq 0) }
    catch { return $false }
    finally { $ErrorActionPreference = $previous }
}

$python = Join-Path $Root '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) { throw 'Run "setup.bat -Dev" first: it creates .venv with the build tools.' }
if (-not (Test-Native $python @('-c', 'import PyInstaller'))) { throw 'PyInstaller is missing: run "setup.bat -Dev".' }
$version = (& $python -c 'import jarvis; print(jarvis.__version__)').Trim()
Write-Host "Building JARVIS $version" -ForegroundColor Cyan

if (-not $SkipTests) {
    Write-Step 'Running the tests'
    Invoke-Native 'The tests' $python @('-m', 'pytest', 'tests', '-q', '-p', 'no:cacheprovider')
}

Write-Step 'Building the program folder (PyInstaller)'
Invoke-Native 'PyInstaller' $python @('-m', 'PyInstaller', 'packaging\jarvis.spec', '--noconfirm', '--clean')
$cli = Join-Path $Root 'dist\JARVIS\jarvis-cli.exe'

Write-Step 'Checking the built JARVIS (real commands in a temporary JARVIS)'
Invoke-Native 'The check of the built JARVIS' $cli @('check', '--full')

if ($SkipInstaller) { exit 0 }
# Inno Setup 6.3 or newer (7 works too): ISCC.exe on PATH, else the newest "Inno Setup N" folder.
$iscc = Get-Command ISCC.exe -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source -First 1
if (-not $iscc) {
    $roots = @(${env:ProgramFiles(x86)}, $env:ProgramFiles, (Join-Path $env:LOCALAPPDATA 'Programs')) | Where-Object { $_ }
    $iscc = $roots | ForEach-Object { Get-ChildItem -Path $_ -Filter 'Inno Setup *' -Directory -ErrorAction SilentlyContinue } |
        Sort-Object { [int]('0' + ($_.Name -replace '\D', '')) } -Descending |
        ForEach-Object { Join-Path $_.FullName 'ISCC.exe' } | Where-Object { Test-Path $_ } | Select-Object -First 1
}
if (-not $iscc) {
    Write-Host "`nInno Setup is not installed, so there is no installer. The program folder is dist\JARVIS." -ForegroundColor Yellow
    Write-Host 'To make the installer: winget install --id JRSoftware.InnoSetup --exact, then run this script again.'
    exit 0
}
Write-Step 'Building the installer (Inno Setup)'
Invoke-Native 'Inno Setup' $iscc @("/DAppVersion=$version", 'packaging\jarvis.iss')
Write-Host "`nDone: dist\JARVIS-Setup-$version.exe" -ForegroundColor Green
