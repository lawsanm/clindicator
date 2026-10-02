# Removes only what Claude RGB Status added: its hook entries in ~/.claude/settings.json (backed up first)
# and its start-at-login entry. Closes the app first so the keyboard gets the restore profile.
# -Purge also deletes its config, logs and backups. The project folder itself is left alone.
#
#   powershell -ExecutionPolicy Bypass -File scripts\uninstall.ps1 [-Purge]
param([switch]$Purge)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$py = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { throw "venv not found at $py" }

& $py -m claude_rgb_status quit
$uninstallArgs = @("-m", "claude_rgb_status", "uninstall")
if ($Purge) { $uninstallArgs += "--purge" }
& $py @uninstallArgs
