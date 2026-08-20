# Zero-admin meeting recorder - setup for Windows.
#
#   powershell -ExecutionPolicy Bypass -File setup.ps1
#
# Creates .venv, installs numpy and PyAudioWPatch, and checks devices and
# permissions. Runs as an ordinary user: no elevated shell, no UAC prompt.

Set-Location -Path $PSScriptRoot

$python = $null
foreach ($candidate in @("py", "python", "python3")) {
    $found = Get-Command $candidate -ErrorAction SilentlyContinue
    if ($found) { $python = $candidate; break }
}

if (-not $python) {
    Write-Host "Python 3.9+ is required but was not found on PATH."
    Write-Host "Install it from https://www.python.org/downloads/windows/"
    Write-Host "(tick 'Add python.exe to PATH'; no administrator rights needed)."
    exit 1
}

& $python bootstrap.py @args
exit $LASTEXITCODE
