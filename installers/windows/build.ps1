<#
.SYNOPSIS
    Build the USB Doctor AI Windows application and installer.

.DESCRIPTION
    Produces:
        dist\USBDoctorAI\USBDoctorAI.exe   portable single-folder build
        dist\USBDoctorAI.exe               portable one-file build
        dist\USBDoctorAI-Setup-1.0.0.exe   Inno Setup installer

    Requirements
        Python 3.9+ 64-bit on PATH (py -3)
        Inno Setup 6 (https://jrsoftware.org/isdl.php) for the installer step
        Optional: signtool.exe (Windows SDK) + a code-signing certificate

.PARAMETER SkipInstaller
    Build only the application, skip Inno Setup.

.PARAMETER Sign
    Sign the produced binaries. Requires $env:CODESIGN_CERT to be set to the
    certificate thumbprint or a full signtool /f argument set.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File installers\windows\build.ps1
#>
[CmdletBinding()]
param(
    [switch]$SkipInstaller,
    [switch]$Sign
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Resolve-Path (Join-Path $PSScriptRoot "..\..")
Set-Location $ProjectRoot

function Write-Step($message) {
    Write-Host ""
    Write-Host "==> $message" -ForegroundColor Cyan
}

function Assert-Command($name, $hint) {
    if (-not (Get-Command $name -ErrorAction SilentlyContinue)) {
        throw "Required command '$name' was not found. $hint"
    }
}

Write-Step "Checking the toolchain"
Assert-Command "py" "Install Python 3.9+ (64-bit) from python.org and ensure the 'py' launcher is on PATH."
$pythonVersion = & py -3 -c "import sys; print('.'.join(map(str, sys.version_info[:2])))"
Write-Host "Python $pythonVersion"

Write-Step "Creating an isolated build environment"
if (-not (Test-Path ".venv-build")) {
    & py -3 -m venv .venv-build
}
$venvPython = Join-Path $ProjectRoot ".venv-build\Scripts\python.exe"
& $venvPython -m pip install --upgrade pip --quiet
& $venvPython -m pip install --quiet "PySide6-Essentials>=6.5" "pyinstaller>=6.0" "Pillow>=10.0"

Write-Step "Running the test suite (a broken build must not ship)"
& $venvPython -m pip install --quiet pytest
& $venvPython -m pytest tests -q
if ($LASTEXITCODE -ne 0) {
    throw "Tests failed — fix them before packaging."
}

Write-Step "Generating icons"
& $venvPython tools\make_icons.py

Write-Step "Building the executable"
if (Test-Path "build") { Remove-Item "build" -Recurse -Force }
if (Test-Path "dist") { Remove-Item "dist" -Recurse -Force }
& $venvPython -m PyInstaller installers\USBDoctorAI.spec --noconfirm --clean
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed." }

Write-Host ""
Write-Host "Built:" -ForegroundColor Green
Get-ChildItem dist -Filter "USBDoctorAI*" | ForEach-Object {
    Write-Host ("  {0}  ({1:N1} MB)" -f $_.Name, ((Get-ChildItem $_.FullName -Recurse -File -ErrorAction SilentlyContinue | Measure-Object Length -Sum).Sum / 1MB))
}

# Smoke test: the CLI must answer without a GUI session.
Write-Step "Smoke-testing the frozen binary"
$probe = & "dist\USBDoctorAI\USBDoctorAI.exe" status --json 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Warning "The frozen binary did not return a status; inspect it manually before shipping."
    Write-Host $probe
} else {
    Write-Host "Frozen binary responded correctly."
}

if ($Sign) {
    Write-Step "Signing binaries"
    $signtool = Get-Command signtool.exe -ErrorAction SilentlyContinue
    if (-not $signtool) {
        throw "signtool.exe not found. Install the Windows SDK, or omit -Sign."
    }
    if (-not $env:CODESIGN_CERT) {
        throw "Set `$env:CODESIGN_CERT to the certificate thumbprint (or full /f argument) before signing."
    }
    $files = @("dist\USBDoctorAI\USBDoctorAI.exe", "dist\USBDoctorAI.exe")
    foreach ($file in $files) {
        if (Test-Path $file) {
            & signtool.exe sign /tr http://timestamp.digicert.com /td sha256 /fd sha256 `
                /sha1 $env:CODESIGN_CERT $file
            & signtool.exe verify /pa $file
        }
    }
}

if (-not $SkipInstaller) {
    Write-Step "Building the installer"
    $iscc = @(
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
    ) | Where-Object { Test-Path $_ } | Select-Object -First 1

    if (-not $iscc) {
        Write-Warning "Inno Setup 6 was not found, so no installer was produced."
        Write-Host "Install it from https://jrsoftware.org/isdl.php and re-run, or pass -SkipInstaller next time."
        Write-Host "The portable builds in dist\ are complete and usable."
    } else {
        & $iscc "installers\windows\usbdoctor.iss"
        if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed." }
        Get-ChildItem dist -Filter "*Setup*.exe" | ForEach-Object {
            Write-Host ("  installer: {0}  ({1:N1} MB)" -f $_.Name, ($_.Length / 1MB)) -ForegroundColor Green
        }
    }
}

Write-Step "Done"
Write-Host "Portable: dist\USBDoctorAI\USBDoctorAI.exe"
Write-Host "CLI     : dist\USBDoctorAI\USBDoctorAI.exe status"
