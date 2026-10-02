# Claude RGB Status

Your laptop keyboard shows what Claude Code is doing, so you can minimize the terminal and still notice
when Claude finishes, needs permission, or asks you something. A tray icon, optional notifications and a
sound mirror the same status.

| Keyboard | Meaning |
|---|---|
| **Blue** | Claude is processing your request or running tools |
| **Amber, slow pulse** | Waiting for your permission (incl. plan approval) |
| **Purple, slow pulse** | Waiting for your answer or other input (`AskUserQuestion`, MCP input) |
| **Green** | Claude finished this response — check it. Stays until you acknowledge or work resumes. *Not* a claim that the task or tests succeeded |
| **Red** | The turn ended with an API error (rate limit, auth, overload, …) |
| **Dim white** | Connected and idle |
| **Dim orange** | No active session, Claude exited unexpectedly, or status unknown |
| *Restore profile* | App paused or closed (default static white — set your usual look in Settings) |

Colors, intensity, pulse/static and brightness are configurable.

## Compatibility (checked on this machine)

- **Verified on real hardware:** Lenovo Legion Slim 5 16AHP9 (83DH), keyboard controller USB `048D:C994`,
  Windows 11 25H2, Claude Code 2.1.280 (native `claude.exe` in Windows Terminal).
- **Why not OpenRGB:** OpenRGB's Lenovo 4-zone detector stops at the 2023 models; it does not detect `C994`.
  Lenovo publishes no lighting SDK for these keyboards, and the keyboard exposes no Windows Dynamic Lighting
  (LampArray) interface.
- **What is used instead:** the 4-zone keyboard's own HID feature report (`CC 16 effect speed brightness RGB×4`),
  the same packet OpenRGB sends to the 2020–2023 Legion 4-zone keyboards and that L5P-Keyboard-RGB uses for
  `C994`. It is written only to the `FF89/00CC` HID collection of allowlisted Legion/LOQ/IdeaPad 4-zone PIDs.
  No firmware, embedded-controller or driver changes; no admin rights.
- Other keyboards: choose **None** as the device to use the tray icon and notifications only.
- **Fantech Atom Pro (checked 2026-10-03, USB `3151:6000` / Bluetooth "Atom Pro 5.0"):** not controllable.
  OpenRGB's Royuan driver covers other `3151` PIDs but not `6000`, the keyboard has no Dynamic Lighting
  interface, and Fantech publishes no SDK. It works with the tray icon and notifications only.

## Setup

Requires Windows 10/11 and Python ≥ 3.11 (`uv` is used if present).

```powershell
cd D:\Project\clindicator
powershell -ExecutionPolicy Bypass -File scripts\install.ps1            # add -Autostart to start at login
```

This creates `.venv`, installs `pystray` and `Pillow`, backs up `~/.claude/settings.json`, merges the hooks
(other settings and hooks are untouched; re-running never duplicates them), and starts the tray app.
Running Claude Code sessions pick up the hooks automatically. Hooks only run in folders you have trusted
in Claude Code.

| Task | Command (from the project folder) |
|---|---|
| Start the app | `powershell -File scripts\run.ps1` |
| Quit (restores lighting) | `.venv\Scripts\python -m claude_rgb_status quit` or tray → Quit |
| Status | `.venv\Scripts\python -m claude_rgb_status status` |
| Hook check | `.venv\Scripts\python -m claude_rgb_status hooks-status` |
| Hardware color test | `.venv\Scripts\python -m claude_rgb_status hwtest --start-delay 5` (app closed) |
| Scripted demo | `.venv\Scripts\python -m claude_rgb_status simulate demo` (also `permission`, `question`, `error`, `subagent`) |
| Uninstall | `powershell -ExecutionPolicy Bypass -File scripts\uninstall.ps1` (add `-Purge` to delete config/logs/backups) |

Uninstall removes only this app's hook entries and its start-at-login entry, and restores pre-existing
empty hook lists exactly. Settings backups are kept in `%LOCALAPPDATA%\ClaudeRGBStatus\backups\`.

## Using it

- **Left-click the tray icon** to acknowledge green/red (back to dim white); otherwise it opens Diagnostics.
  The icon may sit under the taskbar's **^** overflow; drag it onto the taskbar to keep it visible.
- **Right-click** for: current status and session, Acknowledge, Clear stuck request, Pause/Resume lighting,
  **Track session** (most recent session, all sessions with requests first, or pin one session), **Test colors**,
  Re-apply lighting, notifications, sound, start at login, Settings, Diagnostics, Quit.
- **Settings:** keyboard selector (only the selected keyboard is written), hardware brightness, pulse speed
  (slow speeds only), per-status color / intensity / pulse with Test buttons, restore profile, behavior.
- **Diagnostics:** receiver, hooks, keyboard connection and errors, conflicting software, sessions, and the
  recent state transitions.

## How it works

```
Claude Code hooks ──► hook_relay.py ──UDP 127.0.0.1──► receiver ─► state machine ─► lighting thread ─► keyboard
 (async, exec form)   stdlib only, exit 0               token check   per session     dedupe, reconnect  └► tray / toasts
```

- **Hooks** (`SessionStart/End`, `UserPromptSubmit`, `Pre/PostToolUse(+Failure)`, `PostToolBatch`,
  `PermissionRequest`, `PermissionDenied`, `Notification`, `Stop`, `StopFailure`, `SubagentStart/Stop`,
  `Pre/PostCompact`, `Elicitation(Result)`) run `python -I -S hook_relay.py` asynchronously, so Claude never
  waits. The relay always exits 0 with no output: it can't block Claude, approve or deny anything, or answer
  questions, and it is silent when the app isn't running.
- **Privacy:** prompts, tool inputs/outputs, messages and notification text never leave the relay. Only status
  metadata is sent (event, session id, project folder, tool name, ids, a CRC32 of the tool input for matching).
  Logs and `status.json` contain only that metadata.
- **State machine** (`state.py`): one per session, with spawn-time timestamps, tool-call correlation
  (`tool_use_id`, or agent + tool + input hash because `PermissionRequest` carries no id) and per-agent scopes.
  Permission/question states clear only on evidence about that request or on turn-level evidence (`Stop`,
  `StopFailure`, new prompt, `idle_prompt`, `SessionEnd`); unrelated parallel tool events never clear them.
  Late and duplicate events are ignored, subagent stops never finish the parent, and work resuming after
  `Stop` returns to blue. A session counts as disconnected only when its `claude.exe` process is gone.
- **Lighting:** pulses use the keyboard's own slow breathing effect (no host animation). Writes happen only on
  change; the device is reopened after write failures, after sleep, and when Lenovo Vantage's lighting
  service (re)starts, then the current color is re-applied.

## Manual test with real Claude Code

1. App running; keyboard dim orange (or dim white). Open a new terminal in a trusted project and run `claude`
   → **dim white**.
2. Ask "list the files here and summarize them" → **blue** while working → **green** when the reply ends.
   Left-click the tray icon → **dim white**.
3. Ask "run `git status` with Bash" in default permission mode (no allow rule for it) → **amber pulse** while the
   prompt is shown (toast after ~0.3 s) → approve → **blue** → **green**.
4. Ask "use AskUserQuestion to ask me to pick between two options" → **purple pulse** → answer → **blue** → **green**.
5. Start a second `claude` in another folder. Tray → Track session: switch between "Most recent session",
   "All sessions (requests first)" and a pinned session; the tooltip names the project shown.
6. Start a longer task, then close that terminal window → within ~3 s the session is marked disconnected
   (**dim orange** if it was the one shown).
7. Tray → Pause lighting → restore profile; Resume → status color. Quit → restore profile.
8. Error (hard to trigger on purpose): `python -m claude_rgb_status simulate error` → **red** until acknowledged.

## Limitations

- **Plain-text questions:** a question written in Claude's normal reply ends the turn like any other reply, so it
  shows **green**. If the reply's last lines end with "?", the tooltip adds "reply may end with a question";
  this is a hint, not detection. Structured questions (`AskUserQuestion`, MCP input) show purple.
- **Interrupts and manual denials fire no hook.** Pressing Esc, or rejecting a permission prompt, leaves the
  last state (blue/amber) until the next event: a new prompt, or Claude's `idle_prompt` notice (~60 s after it
  goes idle, only if you haven't typed), which turns it green. Tray → *Clear stuck request* is a manual override.
- **Restore is a profile, not the previous state:** the controller can't report its current lighting, so pause/quit
  apply your configured restore profile. Lenovo Vantage can also re-apply its own lighting at times (it is
  detected and reported, never disabled); the app re-applies its color after sleep and after Vantage restarts.
  Use *Re-apply lighting* if another tool changed the keyboard.
- **Fn+Space:** the protocol has no brightness-off level, so the next status change turns the backlight back on.
  Pause the app to keep it off.
- **Timing:** the keyboard follows Claude with ~0.1–0.3 s delay; permission/question wait 0.3 s (configurable)
  so requests answered instantly by another hook don't flash.
- **Green with background work:** green means the main agent finished responding; background tasks or subagents
  may still be running (shown in the tooltip). Their permission requests still show amber.
- **Paths:** hooks call the base Python (`…\Python314\python.exe`) and this folder's `hook_relay.py`. If either
  moves, re-run the installer (or uninstall), otherwise Claude shows "hook error" notices.
- **Write frequency:** the app writes only on status changes. Lenovo doesn't document whether the controller
  stores settings in flash; L5P-Keyboard-RGB drives the same controller with continuous software animations,
  which suggests writes are not persisted per change.
- Logoff/shutdown restore is best effort. Windows Focus Assist can hide notifications.
- WSL is not used on this machine, so no WSL bridge is included. Cloud sessions don't read `~/.claude/settings.json`.

## Files

- Code: `src/claude_rgb_status/` (`hook_relay.py`, `state.py`, `core.py`, `lighting.py`, `rgb/`, `tray.py`, `ui.py`,
  `hooks_install.py`). Tests: `tests/` (`.venv\Scripts\python -m pytest`).
- Config: `%APPDATA%\ClaudeRGBStatus\config.json`. Runtime data, logs, backups:
  `%LOCALAPPDATA%\ClaudeRGBStatus\`.
- Plan and verification checklist: `CHECKLIST.md`.
