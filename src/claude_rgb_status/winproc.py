"""Windows process helpers: liveness of Claude Code processes and the running-process list."""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.OpenProcess.restype = wt.HANDLE
_k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
_k32.CloseHandle.argtypes = [wt.HANDLE]
_k32.GetExitCodeProcess.argtypes = [wt.HANDLE, ctypes.POINTER(wt.DWORD)]
_k32.GetProcessTimes.argtypes = [wt.HANDLE] + [ctypes.POINTER(wt.FILETIME)] * 4
_k32.CreateToolhelp32Snapshot.restype = wt.HANDLE
_k32.CreateToolhelp32Snapshot.argtypes = [wt.DWORD, wt.DWORD]

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259
_ERROR_INVALID_PARAMETER = 87  # OpenProcess on a PID that doesn't exist
_INVALID_HANDLE = wt.HANDLE(-1).value


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wt.DWORD), ("cntUsage", wt.DWORD), ("th32ProcessID", wt.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wt.DWORD), ("cntThreads", wt.DWORD),
        ("th32ParentProcessID", wt.DWORD), ("pcPriClassBase", ctypes.c_long), ("dwFlags", wt.DWORD),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


_k32.Process32FirstW.argtypes = [wt.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
_k32.Process32NextW.argtypes = [wt.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]


def _filetime_to_unix(ft: wt.FILETIME) -> float:
    return ((ft.dwHighDateTime << 32) | ft.dwLowDateTime) / 1e7 - 11644473600.0


def probe(pid: int) -> tuple[bool | None, float | None]:
    """(alive?, creation time). alive is None when Windows won't tell us."""
    handle = _k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        if ctypes.get_last_error() == _ERROR_INVALID_PARAMETER:
            return False, None
        return None, None
    try:
        code = wt.DWORD()
        if not _k32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return None, None
        times = [wt.FILETIME() for _ in range(4)]
        created = None
        if _k32.GetProcessTimes(handle, *[ctypes.byref(t) for t in times]):
            created = _filetime_to_unix(times[0])
        return code.value == _STILL_ACTIVE, created
    finally:
        _k32.CloseHandle(handle)


class ProcessWatch:
    """Remembers each PID's creation time so a reused PID isn't mistaken for Claude."""

    def __init__(self) -> None:
        self._created: dict[int, float] = {}

    def is_alive(self, pid: int) -> bool | None:
        alive, created = probe(pid)
        if alive is not True:
            if alive is False:
                self._created.pop(pid, None)
            return alive
        if created is None:
            return True
        first = self._created.setdefault(pid, created)
        return abs(first - created) < 0.01


def list_processes() -> list[tuple[int, int, str]]:
    """(pid, parent pid, exe name) for every running process."""
    snapshot = _k32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    if snapshot in (None, _INVALID_HANDLE):
        return []
    result = []
    try:
        entry = _PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        ok = _k32.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            result.append((entry.th32ProcessID, entry.th32ParentProcessID, entry.szExeFile))
            ok = _k32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        _k32.CloseHandle(snapshot)
    return result
