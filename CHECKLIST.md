# Claude RGB Status — Plan & Checklist

Living document. `[x]` = done and verified, `[~]` = done but only simulated / partially verified, `[ ]` = open.
Last updated: 2026-10-03

---

## 1. Compatibility findings (inspected 2026-10-03)

| Item | Finding |
|---|---|
| OS | Windows 11 Home 25H2, build 26200, x64 |
| Laptop | Lenovo **Legion Slim 5 16AHP9** (MT 83DH), BIOS NRCN27WW |
| Claude Code | **2.1.280**, native `claude.exe` (npm-installed), running in **Windows Terminal** (not WSL, not IDE). Git Bash present |
| Keyboard RGB controller | USB `048D:C994` "ITE Device(8295)", HID collection `MI_00&Col03`, usage page `0xFF89`, usage `0x00CC`, 33-byte feature report → **Lenovo Legion 4-zone RGB** (2024 "regular + slim") |
| Other ITE device | `048D:C981` "ITE Device(8176)" = key input (not touched) |
| Read back current lighting? | **No.** `GetFeature(0xCC)` returns identity bytes (`8d 04 94 c9` = VID/PID), not colors → restore must use a configurable fallback profile |
| OpenRGB | Not installed. **Does not support `C994`**: master's 4-zone detector stops at 2023 PIDs (C955–C985); no MR adds it |
| Windows Dynamic Lighting (LampArray) | **Not available** (0 LampArray devices; no usage page 0x59 collection) |
| Manufacturer SDK | None published for Legion 4-zone keyboards. Lenovo Vantage (10.2606, gaming add-in running) controls it via the same HID interface, no public API |
| Community protocol | 33-byte feature report `CC 16 <effect> <speed 1-4> <brightness 1-2> <RGB×4 zones> 00 <waveLTR> <waveRTL> …`; effects static=1, breath=3, wave=4, smooth=6. Same packet OpenRGB sends to 2020–2023 Legion 4-zone keyboards; L5P-Keyboard-RGB explicitly lists `0xC994 // 2024` |
| Conflict | **Lenovo Vantage** (`LenovoVantage-(LenovoGamingSystemAddin)`) can re-apply its own lighting → detect & warn, never disable |
| Runtimes | Python 3.14.0 + tkinter 8.6, uv 0.11, Node, .NET. `python -I -S` starts in ~46 ms |
| Existing Claude settings | `~/.claude/settings.json` has **no hooks**; UTF-8 with non-ASCII chars (must be preserved) |

### Hooks facts verified in official docs (code.claude.com/docs/en/hooks)
- All candidate events exist: `UserPromptSubmit`, `PreToolUse`, `PermissionRequest`, `PermissionDenied` (auto-mode only), `PostToolUse`, `PostToolUseFailure` (`is_interrupt`), `PostToolBatch` (`tool_calls[]` with ids), `Notification`, `Stop` (`last_assistant_message`, `background_tasks`), `StopFailure` (`error`), `SubagentStart/Stop`, `SessionStart/End`, `PreCompact/PostCompact`, `Elicitation/ElicitationResult`.
- **`PermissionRequest` has no `tool_use_id`** → correlate by session + agent_id + tool_name + hash(tool_input).
- **`Stop` does not fire on user interrupt.** A manual permission denial fires neither `PostToolUse` nor `PostToolUseFailure`.
- `permission_prompt` notification fires only after ~6 s of no typing; `idle_prompt` = "finished ~60 s ago, no typing since" (not a question).
- Subagent tool events carry `agent_id`; `SubagentStop` also fires for internal agents (prompt suggestions, /btw).
- `http` hooks show a `hook error` notice on connection failure → rejected (noise whenever the app is closed).
- `async: true` command hooks never block Claude; exec form (`command` + `args`) spawns without a shell.
- Settings-file hook edits are picked up live by the file watcher.

---

## 2. Chosen approach

```
Claude Code ──(async exec-form command hook, ~0 ms added to Claude)──► hook/claude_rgb_hook.py
   stdlib-only, `python -I -S`; extracts metadata only; always exit 0, no stdout
        │ UDP datagram → 127.0.0.1:<port>, per-install token (never blocks, silent if app down)
        ▼
Claude RGB Status app (Python, background, tray)
   receiver ─► state manager (per-session state machines + aggregate) ─► lighting controller ─► RGB adapter
                                                       └─► tray icon / notifications / diagnostics
```

- **RGB adapter:** `Legion4ZoneHid` — Windows `hid.dll` SetFeature on the `FF89/00CC` 33-byte collection only, PID allowlist from OpenRGB + L5P lists. No firmware / EC writes. Hardware **breath** effect for pulsing (slowest speed, no host animation); static fallback. Writes only on state change. **Requires user confirmation (see Decisions).** Fallback adapter: `None` (tray + notifications only).
- **Stack:** Python 3.14 venv (uv) + `pystray` + `Pillow`; tkinter for Settings/Diagnostics; everything else stdlib/ctypes.
- **Why not OpenRGB:** it doesn't detect `C994`. **Why not http hooks:** error noise when app closed.

### Status mapping (defaults, all configurable)
| Display state | Trigger (evidence) | Keyboard |
|---|---|---|
| Processing | `UserPromptSubmit`; main-agent tool activity newer than last Stop; `quota_auto_resume_fired`; manual `/compact` running | Blue, static |
| Permission | `PermissionRequest` (non-question tool), `PreToolUse(ExitPlanMode)`; `permission_prompt` notification only if no newer resolution (sandbox network prompts) | Amber, slow pulse |
| Question / input | `PreToolUse`/`PermissionRequest` for `AskUserQuestion`; `Elicitation`; `elicitation_*dialog`, `agent_needs_input`, `quota_auto_resume_stale` notifications | Purple, slow pulse |
| Finished | `Stop` (main agent only) — "response ended, check Claude"; `idle_prompt` while still marked busy | Green, until acknowledged / work resumes |
| Error | `StopFailure`; `quota_auto_resume_disabled` | Red |
| Idle | `SessionStart`, after acknowledge, after manual compaction | Dim white |
| Unknown / disconnected | no session yet, Claude process gone without `SessionEnd`, app just started | Dim orange |
| Paused / quit | — | Restore profile (configurable; can't read Vantage state) |

Resolution rules: a pending permission/question clears only on its own outcome (`PostToolUse`/`Failure` of the same call, `PermissionDenied`, `PostToolBatch` containing it, `ElicitationResult`), or on turn-level evidence (`Stop`, `StopFailure`, `UserPromptSubmit`, `idle_prompt`, `SessionEnd`, scope's `SubagentStop`). Unrelated parallel tool events never clear it. Events older than the session's latest turn boundary are ignored; duplicates are idempotent. Subagent stops never finish the parent.

Plain-text questions: Claude asking in ordinary text ends with `Stop` → **green** ("response ended — check Claude"). Optional heuristic flags "last line ends with `?`" in the tray tooltip; it never claims certainty.

---

## 3. Decisions

- [x] D1. Hardware path: **direct HID 4-zone protocol** (user chose 2026-10-03); tray-only remains as fallback adapter
- [x] D2. Restore profile on pause/quit: **static white for now**, user will set their usual look in Settings later
- [x] D3. Hooks installed in user-level `~/.claude/settings.json` (all projects), backed up first
- [x] D4. Default tracking = "follow most recently prompted session"; aggregate mode optional
- [x] D5. WSL bridge not needed (Claude runs natively on Windows) — documented as N/A

---

## 4. Checklist

### Phase A — Hardware adapter (prove real keyboard control first)
- [x] A1. ctypes HID layer: enumerate interfaces, VID/PID, usage page/usage, feature length, instance path (`rgb/hidwin.py`)
- [x] A2. `Legion4ZoneAdapter`: packet builder (static / breath / wave / smooth / off), PID allowlist, open/close (`rgb/legion4zone.py`); reconnect handled in lighting controller (Phase D)
- [x] A3. Packet unit tests vs OpenRGB/L5P layout (`tests/test_legion_packet.py`, 10 passed)
- [x] A4. `hwtest` CLI: cycle every state color on the real keyboard, then restore profile
- [x] A5. **User visually confirmed on the real keyboard (2026-10-03):** all 7 status colors + restore shown; breath speed 1 "slow and calm"; dim white/orange "clearly dim"

### Phase B — Claude Code integration
- [x] B1. `src/claude_rgb_status/hook_relay.py` relay (stdlib only, metadata only, CRC32 input hash, Claude PID via process tree, spawn-time timestamp, question hint flag, always exit 0)
- [x] B2. Relay tests (`tests/test_hook_relay.py`, 32 passed): no prompt/tool/message text forwarded; silent exit 0 when app missing, port closed, or input garbage; ~110 ms per event (async, Claude doesn't wait)
- [x] B3. Hook installer (`hooks_install.py`): relay self-test, backup, idempotent merge, preserves unrelated settings + non-ASCII, atomic write + verify, refuses invalid files
- [x] B4. Uninstaller removes only this app's handlers; manifest restores pre-existing empty lists/objects byte-exactly (`tests/test_hooks_install.py`, 17 passed — temp files only so far)

### Phase C — State management
- [x] C1. Per-session state machine (`state.py`): session ids, spawn-time timestamps, tool-call correlation by tool_use_id or (agent, tool, input hash), agent scopes, turn boundaries
- [x] C2. Selection: follow latest / pinned / aggregate (priority permission > question > error > finished > processing > idle)
- [x] C3. Liveness: Claude PID + creation time (`winproc.py`) → disconnected only when the process is gone; unknown liveness never disconnects
- [x] C4. `tests/test_state.py` (47 passed): every spec scenario + out-of-order async delivery, interrupt without Stop, manual denial, sandbox network prompt, elicitations, quota notices, compaction, revival

### Phase D — App
- [x] D1. UDP receiver (`receiver.py`): 127.0.0.1 only, SO_EXCLUSIVEADDRUSE, token (constant-time), size/rate limits; control messages (quit/pause/resume/reapply); single-instance mutex (`app.py`)
- [x] D2. Lighting controller (`lighting.py` + `core.py`): state→effect, 300 ms request debounce, write dedupe, pause/restore, quit/logoff restore, reopen after write failure / sleep, re-apply when Vantage's service restarts
- [x] D3. Tray (`tray.py`): state badge, tooltip with project/session, menu (status, acknowledge = left-click, clear stuck request, pause/resume, track latest/aggregate/pinned, test colors, re-apply, toggles, settings, diagnostics, quit) — menu build covered by `tests/test_ui_smoke.py`
- [x] D4. Settings window (`ui.py`): device selector, brightness, pulse speed, per-state color/intensity/pulse + Test, restore profile, behavior — smoke-tested
- [x] D5. Diagnostics window: receiver, hooks, keyboard, conflicts, sessions, transitions — smoke-tested
- [~] D6. Notifications **seen by user** in the demo; start-at-login round-trip verified (HKCU Run value written with the venv `pythonw` command, then removed — left **off**); sound implemented but not heard/confirmed by the user
- [x] D7. Conflict detection — on this machine it reports Lenovo Vantage's gaming add-in (warn only, never disabled)
- [x] D8. Metadata-only rotating log + `status.json` (integration test asserts prompt text never appears)

### Phase E — Validation & delivery
- [x] E1. Full automated suite green: 117 tests (packet, relay, installer, state, conflicts, integration, UI smoke)
- [x] E2. Simulated events through the real relay → running app → **real keyboard**: user confirmed the full demo sequence + toasts (2nd run)
- [x] E3. Real Claude Code sessions: this interactive session (pid 7944) tracked live; headless `claude -p` sessions drove SessionStart → processing → (Glob tool) → finished → closed, and a real **PermissionRequest (Bash)** → resolved by PostToolBatch after the non-interactive auto-deny (no amber flash thanks to the debounce; nothing approved)
- [x] E4. Installed into real `~/.claude/settings.json`: backup made, only the `hooks` key added (all 8 original keys verified unchanged), 18/18 events; uninstall verified on temp copies (byte-exact)
- [x] E5. README + `scripts/install.ps1`, `scripts/uninstall.ps1`, `scripts/run.ps1`
- [x] E6. Live question test in this real session: 1st run exposed a bug (Claude Code's `permission_prompt` notice arrives ~6 s after an AskUserQuestion dialog and flipped purple → amber); fixed + regression test; **retest confirmed by user: purple pulse for ~20 s and purple ring tray badge**, then blue after answering

---

## 5. Verification summary

| Area | Status |
|---|---|
| Keyboard colors, pulse, dim states, restore (real hardware) | **Verified by user** (`hwtest`, demo, live question) |
| Real Claude Code → keyboard (this interactive session) | **Verified**: processing, AskUserQuestion purple, back to blue; session PID = `claude.exe` |
| Real headless sessions (`claude -p`) | **Verified via app log**: start → processing → finished → closed; real PermissionRequest matched and cleared |
| Tray icon + toasts | **Verified by user** (purple ring badge; permission/question toasts) |
| Multiple sessions, aggregate/pinned, disconnect, late/duplicate events, subagents, errors | **Simulated** (unit + integration tests); real multi-session tracking observed (2 live sessions) |
| Interactive permission prompt (amber) with a real dialog | **Not yet done live** — covered by simulation, a real headless PermissionRequest, and the README manual test step 3 |
| Sleep/resume and Vantage-restart re-apply | **Implemented + unit-tested with a fake device**; not exercised with a real sleep cycle |
| Sound, Settings/Diagnostics windows | **Implemented + smoke-tested**; not yet opened by the user |
| Install into real settings | **Done** (backup in `%LOCALAPPDATA%\ClaudeRGBStatus\backups\`); uninstall verified on temp copies only |
