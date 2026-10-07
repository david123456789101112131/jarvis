<#
.SYNOPSIS
    Sets JARVIS up from this folder: finds Python 3.11+ (64-bit), creates .venv, installs JARVIS with
    everything it uses (AI, voice, window, Windows integration), checks the computer and offers a
    desktop shortcut. Run it again at any time: it only adds what is missing.

.PARAMETER Dev
    Also install the developer tools (tests, lint, type checks, PyInstaller for scripts\build.ps1).

.PARAMETER BuildUI
    Rebuild the interface with Node.js (it ships already built in src\jarvis\ui\static).

.PARAMETER Yes
    Answer yes to the questions (Python install, desktop shortcut).

.EXAMPLE
    setup.bat
    setup.bat -Dev
#>
param([switch]$Dev, [switch]$BuildUI, [switch]$Yes)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

function Write-Step([string]$Text) { Write-Host "`n==> $Text" -ForegroundColor Cyan }

function Confirm-Step([string]$Question) {
    if ($Yes) { return $true }
    $answer = Read-Host "$Question [Y/n]"
    return ($answer -eq '' -or $answer -match '^[yY]')
}

function Invoke-Native([string]$What, [string]$Exe, [string[]]$Arguments) {
    & $Exe @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$What failed (exit code $LASTEXITCODE)." }
}

# A 64-bit Python 3.11-3.13 (the versions JARVIS is tested on): the py launcher, python on PATH, then
# the usual install folders. (No double quotes inside arguments: Windows PowerShell 5.1 drops them.)
function Find-Python {
    $probe = 'import struct, sys; print(sys.version_info[0] * 100 + sys.version_info[1], struct.calcsize(''P'') * 8, sys.executable)'
    $candidates = @()
    if (Get-Command py -ErrorAction SilentlyContinue) {
        foreach ($version in '3.13', '3.12', '3.11') { $candidates += , @('py', "-$version") }
    }
    $candidates += , @('python')
    foreach ($folder in "$env:LOCALAPPDATA\Programs\Python", $env:ProgramFiles) {
        foreach ($version in '313', '312', '311') {
            $exe = Join-Path $folder "Python$version\python.exe"
            if (Test-Path $exe) { $candidates += , @($exe) }
        }
    }
    foreach ($candidate in $candidates) {
        $exe = $candidate[0]
        $pyArgs = @($candidate | Select-Object -Skip 1) + @('-c', $probe)
        $previous = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        try { $out = & $exe @pyArgs 2>$null } catch { $out = $null } finally { $ErrorActionPreference = $previous }
        if ($LASTEXITCODE -ne 0 -or -not $out) { continue }
        $parts = "$out".Trim() -split ' ', 3
        if ($parts.Count -eq 3 -and [int]$parts[0] -ge 311 -and [int]$parts[0] -le 313 -and [int]$parts[1] -eq 64) {
            return $parts[2]
        }
    }
    return $null
}

Write-Host 'JARVIS setup' -ForegroundColor Cyan
Write-Step 'Looking for Python 3.11 or newer (64-bit)'
$python = Find-Python
if (-not $python) {
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if ($winget -and (Confirm-Step 'No 64-bit Python 3.11-3.13 was found. Install Python 3.12 from the winget catalog?')) {
        # winget shows Python's license terms and asks for your agreement itself.
        Invoke-Native 'Installing Python' 'winget' @('install', '--id', 'Python.Python.3.12', '--exact', '--scope', 'user')
        $env:Path = [Environment]::GetEnvironmentVariable('Path', 'User') + ';' + [Environment]::GetEnvironmentVariable('Path', 'Machine')
        $python = Find-Python
    }
    if (-not $python) {
        Write-Host 'Install Python 3.12 (64-bit) from https://www.python.org/downloads/windows/ and run setup.bat again.' -ForegroundColor Yellow
        exit 1
    }
}
Write-Host "Using $python"

$venvPython = Join-Path $Root '.venv\Scripts\python.exe'
if (-not (Test-Path $venvPython)) {
    Write-Step 'Creating the virtual environment (.venv)'
    Invoke-Native 'Creating .venv' $python @('-m', 'venv', (Join-Path $Root '.venv'))
}

Write-Step 'Installing JARVIS and the packages it uses (the first time takes a few minutes)'
Invoke-Native 'Updating pip' $venvPython @('-m', 'pip', 'install', '--upgrade', '--quiet', 'pip')
$extras = if ($Dev) { 'all,dev' } else { 'all' }
Invoke-Native 'Installing JARVIS' $venvPython @('-m', 'pip', 'install', '--quiet', '-e', ".[$extras]")
if ($Dev) {
    Write-Host 'The window tests use Microsoft Edge (or run: .venv\Scripts\python -m playwright install chromium).'
}

if ($BuildUI) {
    Write-Step 'Building the interface'
    if (-not (Get-Command npm -ErrorAction SilentlyContinue)) { throw 'Node.js (npm) is needed to build the interface: https://nodejs.org' }
    Push-Location (Join-Path $Root 'ui')
    try {
        Invoke-Native 'npm ci' 'npm.cmd' @('ci')
        Invoke-Native 'npm run build' 'npm.cmd' @('run', 'build')
    } finally { Pop-Location }
}

Write-Step 'Checking this computer'
& (Join-Path $Root '.venv\Scripts\jarvis.exe') check
$checkFailed = ($LASTEXITCODE -ne 0)

$desktop = [Environment]::GetFolderPath('Desktop')
$shortcut = Join-Path $desktop 'JARVIS.lnk'
if (-not (Test-Path $shortcut) -and (Confirm-Step 'Create a JARVIS shortcut on the desktop?')) {
    $shell = New-Object -ComObject WScript.Shell
    $link = $shell.CreateShortcut($shortcut)
    $link.TargetPath = Join-Path $Root '.venv\Scripts\pythonw.exe'
    $link.Arguments = '-m jarvis'
    $link.WorkingDirectory = $Root
    $link.IconLocation = Join-Path $Root 'src\jarvis\assets\jarvis.ico'
    $link.Description = 'JARVIS desktop AI assistant'
    $link.Save()
    Write-Host "Created $shortcut"
}

Write-Host ''
if ($checkFailed) {
    Write-Host 'JARVIS is installed, but the check above found a problem: see the lines marked FAIL.' -ForegroundColor Yellow
} else {
    Write-Host 'JARVIS is ready.' -ForegroundColor Green
}
Write-Host '  Start JARVIS:      run.bat (or the desktop shortcut)'
Write-Host '  Add an AI key:     jarvis keys set ANTHROPIC_API_KEY   (or in JARVIS: Settings > API keys)'
Write-Host '  Test everything:   jarvis check --full    (jarvis check --voice for the microphone and speakers)'
Write-Host '  (run the jarvis commands in this folder: jarvis.bat calls the one in .venv)'
