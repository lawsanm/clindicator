# Sets up Claude RGB Status: Python venv + dependencies, Claude Code hooks (settings backed up first),
# optional start at login, then starts the tray app. Safe to run again (no duplicate hooks).
#
#   powershell -ExecutionPolicy Bypass -File scripts\install.ps1 [-Autostart] [-NoStart]
param([switch]$Autostart, [switch]$NoStart)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$py = Join-Path $root ".venv\Scripts\python.exe"

if (-not (Test-Path $py)) {
    if (Get-Command uv -ErrorAction SilentlyContinue) { uv venv .venv } else { py -3 -m venv .venv }
}
if (Get-Command uv -ErrorAction SilentlyContinue) {
    uv pip install --python $py -e .
} else {
    & $py -m pip install --upgrade pip
    & $py -m pip install -e .
}

$installArgs = @("-m", "claude_rgb_status", "install")
if ($Autostart) { $installArgs += "--autostart" }
& $py @installArgs
if ($LASTEXITCODE -ne 0) { throw "hook installation failed" }

if (-not $NoStart) {
    & $py -m claude_rgb_status quit | Out-Null   # restart if an older copy is running
    Start-Process -FilePath (Join-Path $root ".venv\Scripts\pythonw.exe") -ArgumentList "-m", "claude_rgb_status", "run" -WorkingDirectory $root
    Write-Host "Claude RGB Status is running (look for the round icon in the tray)."
}
