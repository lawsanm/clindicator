"""Minimal Windows HID access through hid.dll / setupapi.dll (ctypes, no extra packages).

Only feature reports are supported because that is all the 4-zone keyboard needs.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import sys
from dataclasses import dataclass

if sys.platform != "win32":  # pragma: no cover - the app is Windows-only
    raise ImportError("hidwin requires Windows")

_setupapi = ctypes.WinDLL("setupapi", use_last_error=True)
_hid = ctypes.WinDLL("hid", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", wt.DWORD), ("Data2", wt.WORD), ("Data3", wt.WORD), ("Data4", ctypes.c_ubyte * 8)]


class _SP_DEVICE_INTERFACE_DATA(ctypes.Structure):
    _fields_ = [
        ("cbSize", wt.DWORD),
        ("InterfaceClassGuid", _GUID),
        ("Flags", wt.DWORD),
        ("Reserved", ctypes.c_size_t),
    ]


class _HIDD_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Size", wt.ULONG), ("VendorID", wt.USHORT), ("ProductID", wt.USHORT), ("VersionNumber", wt.USHORT)]


class _HIDP_CAPS(ctypes.Structure):
    _fields_ = [
        ("Usage", wt.USHORT),
        ("UsagePage", wt.USHORT),
        ("InputReportByteLength", wt.USHORT),
        ("OutputReportByteLength", wt.USHORT),
        ("FeatureReportByteLength", wt.USHORT),
        ("Reserved", wt.USHORT * 17),
        ("NumberLinkCollectionNodes", wt.USHORT),
        ("NumberInputButtonCaps", wt.USHORT),
        ("NumberInputValueCaps", wt.USHORT),
        ("NumberInputDataIndices", wt.USHORT),
        ("NumberOutputButtonCaps", wt.USHORT),
        ("NumberOutputValueCaps", wt.USHORT),
        ("NumberOutputDataIndices", wt.USHORT),
        ("NumberFeatureButtonCaps", wt.USHORT),
        ("NumberFeatureValueCaps", wt.USHORT),
        ("NumberFeatureDataIndices", wt.USHORT),
    ]


_DIGCF_PRESENT = 0x02
_DIGCF_DEVICEINTERFACE = 0x10
_GENERIC_READ = 0x80000000
_GENERIC_WRITE = 0x40000000
_FILE_SHARE_READ = 0x01
_FILE_SHARE_WRITE = 0x02
_OPEN_EXISTING = 3
_INVALID_HANDLE = ctypes.c_void_p(-1).value
# SP_DEVICE_INTERFACE_DETAIL_DATA_W: DWORD cbSize + WCHAR[1], padded to 8 on x64, 6 on x86.
_DETAIL_CBSIZE = 8 if ctypes.sizeof(ctypes.c_void_p) == 8 else 6

_setupapi.SetupDiGetClassDevsW.restype = ctypes.c_void_p
_setupapi.SetupDiGetClassDevsW.argtypes = [ctypes.POINTER(_GUID), wt.LPCWSTR, wt.HWND, wt.DWORD]
_setupapi.SetupDiEnumDeviceInterfaces.restype = wt.BOOL
_setupapi.SetupDiEnumDeviceInterfaces.argtypes = [
    ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(_GUID), wt.DWORD, ctypes.POINTER(_SP_DEVICE_INTERFACE_DATA),
]
_setupapi.SetupDiGetDeviceInterfaceDetailW.restype = wt.BOOL
_setupapi.SetupDiGetDeviceInterfaceDetailW.argtypes = [
    ctypes.c_void_p, ctypes.POINTER(_SP_DEVICE_INTERFACE_DATA), ctypes.c_void_p, wt.DWORD,
    ctypes.POINTER(wt.DWORD), ctypes.c_void_p,
]
_setupapi.SetupDiDestroyDeviceInfoList.argtypes = [ctypes.c_void_p]
_kernel32.CreateFileW.restype = ctypes.c_void_p
_kernel32.CreateFileW.argtypes = [
    wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p, wt.DWORD, wt.DWORD, ctypes.c_void_p,
]
_kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
_kernel32.CloseHandle.restype = wt.BOOL
_hid.HidD_GetHidGuid.argtypes = [ctypes.POINTER(_GUID)]
_hid.HidD_GetAttributes.argtypes = [ctypes.c_void_p, ctypes.POINTER(_HIDD_ATTRIBUTES)]
_hid.HidD_GetAttributes.restype = wt.BOOLEAN
_hid.HidD_GetPreparsedData.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
_hid.HidD_GetPreparsedData.restype = wt.BOOLEAN
_hid.HidD_FreePreparsedData.argtypes = [ctypes.c_void_p]
_hid.HidP_GetCaps.argtypes = [ctypes.c_void_p, ctypes.POINTER(_HIDP_CAPS)]
_hid.HidP_GetCaps.restype = ctypes.c_long
_hid.HidD_GetProductString.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wt.ULONG]
_hid.HidD_GetProductString.restype = wt.BOOLEAN
_hid.HidD_SetFeature.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wt.ULONG]
_hid.HidD_SetFeature.restype = wt.BOOLEAN


@dataclass(frozen=True)
class HidInterface:
    path: str
    instance_id: str
    vendor_id: int
    product_id: int
    version: int
    usage_page: int
    usage: int
    feature_report_len: int
    product: str


def instance_id_from_path(path: str) -> str:
    r"""\\?\hid#vid_048d&pid_c994&mi_00&col03#9&601fa85&0&0002#{guid} -> HID\VID_048D&...\9&601FA85&0&0002"""
    body = path[4:] if path.startswith("\\\\?\\") else path
    parts = body.split("#")
    if len(parts) >= 3:
        return "\\".join(parts[:3]).upper()
    return body.upper()


def _open(path: str, access: int) -> int | None:
    handle = _kernel32.CreateFileW(
        path, access, _FILE_SHARE_READ | _FILE_SHARE_WRITE, None, _OPEN_EXISTING, 0, None
    )
    if handle in (None, _INVALID_HANDLE):
        return None
    return handle


def _interface_paths() -> list[str]:
    guid = _GUID()
    _hid.HidD_GetHidGuid(ctypes.byref(guid))
    info_set = _setupapi.SetupDiGetClassDevsW(ctypes.byref(guid), None, None, _DIGCF_PRESENT | _DIGCF_DEVICEINTERFACE)
    if info_set in (None, _INVALID_HANDLE):
        raise ctypes.WinError(ctypes.get_last_error())
    paths: list[str] = []
    try:
        index = 0
        while True:
            iface = _SP_DEVICE_INTERFACE_DATA()
            iface.cbSize = ctypes.sizeof(iface)
            if not _setupapi.SetupDiEnumDeviceInterfaces(info_set, None, ctypes.byref(guid), index, ctypes.byref(iface)):
                break
            index += 1
            required = wt.DWORD()
            _setupapi.SetupDiGetDeviceInterfaceDetailW(info_set, ctypes.byref(iface), None, 0, ctypes.byref(required), None)
            if not required.value:
                continue
            buf = ctypes.create_string_buffer(required.value)
            ctypes.cast(buf, ctypes.POINTER(wt.DWORD))[0] = _DETAIL_CBSIZE
            if _setupapi.SetupDiGetDeviceInterfaceDetailW(info_set, ctypes.byref(iface), buf, required, None, None):
                paths.append(ctypes.wstring_at(ctypes.addressof(buf) + 4))
    finally:
        _setupapi.SetupDiDestroyDeviceInfoList(info_set)
    return paths


def _describe(path: str) -> HidInterface | None:
    # Access 0 is enough for attributes/caps and works even for collections
    # the OS holds exclusively (keyboards, mice).
    handle = _open(path, 0)
    if handle is None:
        return None
    try:
        attrs = _HIDD_ATTRIBUTES()
        attrs.Size = ctypes.sizeof(attrs)
        if not _hid.HidD_GetAttributes(handle, ctypes.byref(attrs)):
            return None
        caps = _HIDP_CAPS()
        preparsed = ctypes.c_void_p()
        if _hid.HidD_GetPreparsedData(handle, ctypes.byref(preparsed)):
            try:
                _hid.HidP_GetCaps(preparsed, ctypes.byref(caps))
            finally:
                _hid.HidD_FreePreparsedData(preparsed)
        product = ctypes.create_unicode_buffer(127)
        if not _hid.HidD_GetProductString(handle, product, ctypes.sizeof(product)):
            product.value = ""
        return HidInterface(
            path=path,
            instance_id=instance_id_from_path(path),
            vendor_id=attrs.VendorID,
            product_id=attrs.ProductID,
            version=attrs.VersionNumber,
            usage_page=caps.UsagePage,
            usage=caps.Usage,
            feature_report_len=caps.FeatureReportByteLength,
            product=product.value,
        )
    finally:
        _kernel32.CloseHandle(handle)


def list_interfaces(vendor_id: int | None = None) -> list[HidInterface]:
    """All present HID top-level collections, optionally filtered by vendor."""
    result = []
    for path in _interface_paths():
        if vendor_id is not None and f"vid_{vendor_id:04x}" not in path.lower():
            continue
        info = _describe(path)
        if info is not None and (vendor_id is None or info.vendor_id == vendor_id):
            result.append(info)
    return result


class HidDevice:
    """An open HID collection used for feature reports."""

    def __init__(self, path: str):
        self.path = path
        self._handle: int | None = None

    def open(self) -> None:
        if self._handle is not None:
            return
        handle = _open(self.path, _GENERIC_READ | _GENERIC_WRITE)
        if handle is None:
            raise ctypes.WinError(ctypes.get_last_error())
        self._handle = handle

    @property
    def is_open(self) -> bool:
        return self._handle is not None

    def set_feature(self, report: bytes) -> None:
        if self._handle is None:
            raise OSError("device not open")
        buf = (ctypes.c_ubyte * len(report)).from_buffer_copy(report)
        if not _hid.HidD_SetFeature(self._handle, buf, len(report)):
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self) -> None:
        if self._handle is not None:
            _kernel32.CloseHandle(self._handle)
            self._handle = None

    def __enter__(self) -> HidDevice:
        self.open()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
