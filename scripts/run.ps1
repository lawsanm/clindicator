# Starts the tray app in the background (no console window).
$root = Split-Path -Parent $PSScriptRoot
Start-Process -FilePath (Join-Path $root ".venv\Scripts\pythonw.exe") -ArgumentList "-m", "claude_rgb_status", "run" -WorkingDirectory $root
