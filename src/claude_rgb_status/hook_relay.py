"""Claude Code hook relay for Claude RGB Status.

Claude Code runs this file for each hook event (exec form, async):

    python.exe -I -S hook_relay.py

It reads the event JSON from stdin, keeps only status metadata, and sends one UDP
datagram to the local app. It never prints, never returns a decision and always
exits 0, so it cannot change what Claude Code does, even when the app isn't running.

Privacy: prompts, tool inputs/outputs, assistant messages and notification text are
never forwarded. Tool inputs are reduced to a CRC32 so a permission request can be
matched with its tool call; the final message is reduced to one "ends with a
question?" boolean.

Standard library only: this runs on the bare interpreter, outside the app's venv.
"""

import sys
import time

T_START = time.time()

import json  # noqa: E402
import os  # noqa: E402
import socket  # noqa: E402
import zlib  # noqa: E402

PROTOCOL = b"CRS1"
MAX_DATAGRAM = 60000
APP_DIR_NAME = "ClaudeRGBStatus"
HOME_OVERRIDE_ENV = "CLAUDE_RGB_STATUS_HOME"
CLAUDE_HOSTS = {"claude.exe", "node.exe", "bun.exe"}
# Processes that can sit between Claude Code and this script.
LAUNCHERS = {
    "python.exe", "pythonw.exe", "py.exe", "cmd.exe", "bash.exe", "sh.exe", "dash.exe", "env.exe",
    "powershell.exe", "pwsh.exe", "conhost.exe", "uv.exe", "uvx.exe",
}
TOOL_EVENTS = {"PreToolUse", "PermissionRequest", "PermissionDenied", "PostToolUse", "PostToolUseFailure"}


def default_endpoint_path():
    home = os.environ.get(HOME_OVERRIDE_ENV)
    if home:
        return os.path.join(home, "endpoint.json")
    base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")
    return os.path.join(base, APP_DIR_NAME, "endpoint.json")


def input_hash(tool_input):
    if tool_input is None:
        return None
    try:
        text = json.dumps(tool_input, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError):
        return None
    return "%08x" % zlib.crc32(text.encode("utf-8", "surrogatepass"))


def looks_like_question(text):
    """True if one of the last three non-empty lines ends with '?'. A hint, never certain."""
    if not isinstance(text, str):
        return False
    lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
    for line in lines[-3:]:
        if line.rstrip("*_`)\"' ").endswith("?"):
            return True
    return False


def extract(data):
    """Reduce a hook payload to the metadata the app needs."""
    ev = data.get("hook_event_name")
    msg = {"v": 1, "ev": ev, "sid": data.get("session_id"), "cwd": data.get("cwd")}
    for src, dst in (("agent_id", "aid"), ("agent_type", "at"), ("permission_mode", "pm"),
                     ("session_title", "title")):
        value = data.get(src)
        if value not in (None, ""):
            msg[dst] = value

    if ev in TOOL_EVENTS:
        msg["tool"] = data.get("tool_name")
        if data.get("tool_use_id"):
            msg["tuid"] = data.get("tool_use_id")
        msg["ih"] = input_hash(data.get("tool_input"))
        server = data.get("mcp_server")
        if isinstance(server, dict) and server.get("name"):
            msg["mcp"] = server.get("name")
        if ev == "PostToolUseFailure":
            msg["intr"] = bool(data.get("is_interrupt"))
    elif ev == "PostToolBatch":
        calls = data.get("tool_calls") or []
        msg["batch"] = [
            [c.get("tool_use_id"), c.get("tool_name"), input_hash(c.get("tool_input"))]
            for c in calls[:200] if isinstance(c, dict)
        ]
    elif ev == "Notification":
        msg["nt"] = data.get("notification_type")
    elif ev == "SessionStart":
        msg["src"] = data.get("source")
    elif ev == "SessionEnd":
        msg["rsn"] = data.get("reason")
    elif ev in ("Stop", "SubagentStop"):
        msg["q"] = looks_like_question(data.get("last_assistant_message"))
        tasks = data.get("background_tasks")
        msg["bg"] = len(tasks) if isinstance(tasks, list) else 0
        msg["sha"] = bool(data.get("stop_hook_active"))
    elif ev == "StopFailure":
        msg["err"] = data.get("error") or data.get("error_type")
    elif ev in ("PreCompact", "PostCompact"):
        msg["trig"] = data.get("trigger") or data.get("compact_trigger")
    elif ev in ("Elicitation", "ElicitationResult"):
        msg["mcp"] = data.get("mcp_server_name")
        if data.get("elicitation_id"):
            msg["eid"] = data.get("elicitation_id")
        if data.get("mode"):
            msg["mode"] = data.get("mode")
        if ev == "ElicitationResult":
            msg["act"] = data.get("action")
    return msg


def process_info():
    """(this process's creation time as Unix seconds, Claude Code's PID).

    The creation time is when Claude Code spawned the hook, so it orders events more
    precisely than the time this script starts running. The PID lets the app notice a
    Claude session that exits without SessionEnd.
    """
    if os.name != "nt":
        return None, os.getppid()
    import ctypes
    from ctypes import wintypes as wt

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.GetCurrentProcess.restype = wt.HANDLE
    k32.GetProcessTimes.argtypes = [wt.HANDLE] + [ctypes.POINTER(wt.FILETIME)] * 4
    k32.CreateToolhelp32Snapshot.restype = wt.HANDLE
    k32.CreateToolhelp32Snapshot.argtypes = [wt.DWORD, wt.DWORD]
    k32.CloseHandle.argtypes = [wt.HANDLE]

    created = None
    times = [wt.FILETIME() for _ in range(4)]
    if k32.GetProcessTimes(k32.GetCurrentProcess(), *[ctypes.byref(t) for t in times]):
        ticks = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
        created = ticks / 1e7 - 11644473600.0

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wt.DWORD), ("cntUsage", wt.DWORD), ("th32ProcessID", wt.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wt.DWORD), ("cntThreads", wt.DWORD),
            ("th32ParentProcessID", wt.DWORD), ("pcPriClassBase", ctypes.c_long), ("dwFlags", wt.DWORD),
            ("szExeFile", ctypes.c_wchar * 260),
        ]

    k32.Process32FirstW.argtypes = [wt.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    k32.Process32NextW.argtypes = [wt.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    snapshot = k32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    if snapshot in (None, wt.HANDLE(-1).value):
        return created, os.getppid()
    table = {}
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        ok = k32.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            table[entry.th32ProcessID] = (entry.th32ParentProcessID, entry.szExeFile.lower())
            ok = k32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        k32.CloseHandle(snapshot)

    pid = table.get(os.getpid(), (os.getppid(), ""))[0]
    first_real = None
    for _ in range(8):
        if pid not in table:
            break
        parent, exe = table[pid]
        if exe in CLAUDE_HOSTS:
            return created, pid
        if first_real is None and exe not in LAUNCHERS:
            first_real = pid
        pid = parent
    return created, first_real or os.getppid()


def read_endpoint(path):
    with open(path, encoding="utf-8") as fh:
        endpoint = json.load(fh)
    return int(endpoint["port"]), str(endpoint["token"])


def send(msg, endpoint_path):
    try:
        port, token = read_endpoint(endpoint_path)
    except (OSError, ValueError, KeyError, TypeError):
        return False  # app never ran or was uninstalled: nothing to do
    body = json.dumps(msg, separators=(",", ":"), ensure_ascii=False).encode("utf-8", "surrogatepass")
    if len(body) > MAX_DATAGRAM and "batch" in msg:
        msg = dict(msg, batch=[], batch_truncated=True)
        body = json.dumps(msg, separators=(",", ":"), ensure_ascii=False).encode("utf-8", "surrogatepass")
    payload = PROTOCOL + b" " + token.encode("ascii") + b"\n" + body
    if len(payload) > MAX_DATAGRAM:
        return False
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.sendto(payload, ("127.0.0.1", port))
    finally:
        sock.close()
    return True


def main(argv):
    endpoint_path = default_endpoint_path()
    if len(argv) >= 2 and argv[0] == "--endpoint":
        endpoint_path = argv[1]
    if sys.stdin is None or sys.stdin.isatty():
        return
    data = json.loads(sys.stdin.buffer.read() or b"null")
    if not isinstance(data, dict) or not data.get("hook_event_name"):
        return
    msg = extract(data)
    created, claude_pid = process_info()
    msg["t"] = created if created and created <= T_START + 1 else T_START
    msg["t0"] = T_START
    msg["pid"] = claude_pid
    send(msg, endpoint_path)


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except BaseException:  # never let anything reach Claude Code as a hook error
        pass
    os._exit(0)
