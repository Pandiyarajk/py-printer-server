"""ctypes bindings to the Windows print spooler and shell print verb.

Author: Pandiyaraj Karuppasamy
Date: Sep-14-2026

This is the only module in the package that touches ctypes. Every other
module talks to plain Python types (dataclasses, dicts, bytes) so the rest of
the package stays importable and testable on any platform, and so a bug in a
struct layout has exactly one place to be found.

All Win32 calls use the wide (``W``) entry points, never the narrow (``A``)
ones, so non-ASCII printer and file names round-trip correctly.
"""

from __future__ import annotations

import platform
import winreg
from ctypes import (
    POINTER,
    Structure,
    WinError,
    byref,
    c_int,
    c_long,
    c_short,
    c_ulong,
    c_void_p,
    c_wchar,
    c_wchar_p,
    cast,
    create_string_buffer,
    get_last_error,
    sizeof,
)
from pathlib import Path

if platform.system() != "Windows":
    raise RuntimeError("py-printer-server requires Windows (uses ctypes bindings to winspool.drv)")

import ctypes  # noqa: E402  (after the platform guard, deliberately)

winspool = ctypes.WinDLL("winspool.drv", use_last_error=True)
shell32 = ctypes.WinDLL("shell32.dll", use_last_error=True)

ERROR_INSUFFICIENT_BUFFER = 122
ERROR_SUCCESS = 0

PRINTER_ENUM_LOCAL = 0x00000002
PRINTER_ENUM_CONNECTIONS = 0x00000004

# Verified against pywin32's win32print constants (which wrap the same
# WinGDI/WinSpool headers) -- an earlier version of this file had
# PRINTER_ACCESS_USE and PRINTER_ALL_ACCESS wrong, which surfaced as
# ERROR_ACCESS_DENIED (5) from SetPrinterW/OpenPrinterW rather than as an
# obviously bad constant.
PRINTER_ACCESS_ADMINISTER = 0x00000004
PRINTER_ACCESS_USE = 0x00000008
STANDARD_RIGHTS_READ = 0x00020000
PRINTER_ALL_ACCESS = 0x000F000C  # STANDARD_RIGHTS_REQUIRED | ADMINISTER | USE

DC_COLORDEVICE = 6
DC_DUPLEX = 7

# DocumentPropertiesW flags (wingdi.h). DM_OUT_BUFFER asks the driver to fill
# our buffer with its current settings; DM_IN_BUFFER asks it to validate and
# merge settings we supply.
DM_UPDATE = 1
DM_COPY = 2
DM_PROMPT = 4
DM_MODIFY = 8
DM_OUT_BUFFER = DM_COPY
DM_IN_BUFFER = DM_MODIFY

DM_COLOR = 0x00000800
DM_DUPLEX = 0x00001000
DM_PAPERSIZE = 0x00000002
DM_COPIES = 0x00000100
DM_ORIENTATION = 0x00000001

DMCOLOR_MONOCHROME = 1
DMCOLOR_COLOR = 2

DMDUP_SIMPLEX = 1
DMDUP_VERTICAL = 2

DMPAPER_A4 = 9

DMORIENT_PORTRAIT = 1

SW_HIDE = 0
SE_ERR_NOASSOC = 31

CCHDEVICENAME = 32
CCHFORMNAME = 32


class DEVMODEW(Structure):
    """The Win32 DEVMODEW structure.

    This is the fiddly one: a fixed-size device name, a union of print/display
    fields we only use the print half of, and a `dmFields` bitmask that
    controls which of the other fields the driver actually honours. A field
    written without its bit set in `dmFields` is silently ignored by the
    driver -- it is not a ctypes bug, it is how the API works, but it looks
    exactly like a setting that "does nothing".

    Layout matches the public Win32 SDK header (wingdi.h) field-for-field, in
    order, so `sizeof(DEVMODEW)` can be checked against the measured size
    (verified empirically: 224 bytes on 64-bit Windows, with the compiler
    padding `dmFields` and the block after `dmDuplex` to 4/8-byte alignment)
    as a build-time guard against a mistyped field.
    """

    _fields_ = [
        ("dmDeviceName", c_wchar * CCHDEVICENAME),
        ("dmSpecVersion", c_short),
        ("dmDriverVersion", c_short),
        ("dmSize", c_short),
        ("dmDriverExtra", c_short),
        ("dmFields", c_long),
        ("dmOrientation", c_short),
        ("dmPaperSize", c_short),
        ("dmPaperLength", c_short),
        ("dmPaperWidth", c_short),
        ("dmScale", c_short),
        ("dmCopies", c_short),
        ("dmDefaultSource", c_short),
        ("dmPrintQuality", c_short),
        ("dmColor", c_short),
        ("dmDuplex", c_short),
        ("dmYResolution", c_short),
        ("dmTTOption", c_short),
        ("dmCollate", c_short),
        ("dmFormName", c_wchar * CCHFORMNAME),
        ("dmLogPixels", c_ulong),
        ("dmBitsPerPel", c_ulong),
        ("dmPelsWidth", c_ulong),
        ("dmPelsHeight", c_ulong),
        ("dmDisplayFlags", c_ulong),
        ("dmDisplayFrequency", c_ulong),
        ("dmICMMethod", c_ulong),
        ("dmICMIntent", c_ulong),
        ("dmMediaType", c_ulong),
        ("dmDitherType", c_ulong),
        ("dmReserved1", c_ulong),
        ("dmReserved2", c_ulong),
        ("dmPanningWidth", c_ulong),
        ("dmPanningHeight", c_ulong),
    ]


class PRINTER_INFO_2W(Structure):
    """Only the fields we read; the struct must still match layout exactly
    because ctypes maps memory positionally, not by name."""

    _fields_ = [
        ("pServerName", c_wchar_p),
        ("pPrinterName", c_wchar_p),
        ("pShareName", c_wchar_p),
        ("pPortName", c_wchar_p),
        ("pDriverName", c_wchar_p),
        ("pComment", c_wchar_p),
        ("pLocation", c_wchar_p),
        ("pDevMode", POINTER(DEVMODEW)),
        ("pSepFile", c_wchar_p),
        ("pPrintProcessor", c_wchar_p),
        ("pDatatype", c_wchar_p),
        ("pParameters", c_wchar_p),
        ("pSecurityDescriptor", c_void_p),
        ("Attributes", c_ulong),
        ("Priority", c_ulong),
        ("DefaultPriority", c_ulong),
        ("StartTime", c_ulong),
        ("UntilTime", c_ulong),
        ("Status", c_ulong),
        ("cJobs", c_ulong),
        ("AveragePPM", c_ulong),
    ]


class DOC_INFO_1W(Structure):
    _fields_ = [
        ("pDocName", c_wchar_p),
        ("pOutputFile", c_wchar_p),
        ("pDatatype", c_wchar_p),
    ]


class PRINTER_DEFAULTS(Structure):
    """Passed to OpenPrinterW to request a specific access level.

    Passing NULL for this parameter (as an earlier version of this module
    did) silently opens the handle with a default, lower access level --
    a handle that lets you read a printer's devmode but returns
    ERROR_ACCESS_DENIED (5) the moment SetPrinterW tries to write it back,
    which looks like a permissions problem rather than a missed struct.
    """

    _fields_ = [
        ("pDatatype", c_wchar_p),
        ("pDevMode", c_void_p),
        ("DesiredAccess", c_ulong),
    ]


class JOB_INFO_1W(Structure):
    _fields_ = [
        ("JobId", c_ulong),
        ("pPrinterName", c_wchar_p),
        ("pMachineName", c_wchar_p),
        ("pUserName", c_wchar_p),
        ("pDocument", c_wchar_p),
        ("pDatatype", c_wchar_p),
        ("pStatus", c_wchar_p),
        ("Status", c_ulong),
        ("Priority", c_ulong),
        ("Position", c_ulong),
        ("TotalPages", c_ulong),
        ("PagesPrinted", c_ulong),
        # SYSTEMTIME Submitted (8 x WORD) -- not decoded, only need submit
        # ordering which EnumJobsW already gives us for free (queue order).
        ("Submitted_wYear", c_short),
        ("Submitted_wMonth", c_short),
        ("Submitted_wDayOfWeek", c_short),
        ("Submitted_wDay", c_short),
        ("Submitted_wHour", c_short),
        ("Submitted_wMinute", c_short),
        ("Submitted_wSecond", c_short),
        ("Submitted_wMilliseconds", c_short),
        ("Time", c_ulong),
        ("TotalPages2", c_ulong),
    ]


# ---------------------------------------------------------------------------
# Function prototypes. Every one gets explicit argtypes/restype: without
# them ctypes assumes `int`, which truncates 64-bit handles and pointers on
# a 64-bit process -- the failure mode is a handle that appears to work once
# and then corrupts memory or raises an access violation.
# ---------------------------------------------------------------------------

winspool.EnumPrintersW.argtypes = [c_ulong, c_wchar_p, c_ulong, c_void_p, c_ulong, POINTER(c_ulong), POINTER(c_ulong)]
winspool.EnumPrintersW.restype = c_int

winspool.OpenPrinterW.argtypes = [c_wchar_p, POINTER(c_void_p), POINTER(PRINTER_DEFAULTS)]
winspool.OpenPrinterW.restype = c_int

winspool.ClosePrinter.argtypes = [c_void_p]
winspool.ClosePrinter.restype = c_int

winspool.GetPrinterW.argtypes = [c_void_p, c_ulong, c_void_p, c_ulong, POINTER(c_ulong)]
winspool.GetPrinterW.restype = c_int

winspool.SetPrinterW.argtypes = [c_void_p, c_ulong, c_void_p, c_ulong]
winspool.SetPrinterW.restype = c_int

winspool.GetDefaultPrinterW.argtypes = [c_wchar_p, POINTER(c_ulong)]
winspool.GetDefaultPrinterW.restype = c_int

winspool.DeviceCapabilitiesW.argtypes = [c_wchar_p, c_wchar_p, c_ulong, c_wchar_p, POINTER(DEVMODEW)]
winspool.DeviceCapabilitiesW.restype = c_int

winspool.DocumentPropertiesW.argtypes = [
    c_void_p, c_void_p, c_wchar_p, POINTER(DEVMODEW), POINTER(DEVMODEW), c_long
]
winspool.DocumentPropertiesW.restype = c_long

winspool.StartDocPrinterW.argtypes = [c_void_p, c_ulong, POINTER(DOC_INFO_1W)]
winspool.StartDocPrinterW.restype = c_int

winspool.StartPagePrinter.argtypes = [c_void_p]
winspool.StartPagePrinter.restype = c_int

winspool.WritePrinter.argtypes = [c_void_p, c_void_p, c_ulong, POINTER(c_ulong)]
winspool.WritePrinter.restype = c_int

winspool.EndPagePrinter.argtypes = [c_void_p]
winspool.EndPagePrinter.restype = c_int

winspool.EndDocPrinter.argtypes = [c_void_p]
winspool.EndDocPrinter.restype = c_int

winspool.EnumJobsW.argtypes = [c_void_p, c_ulong, c_ulong, c_ulong, c_void_p, c_ulong, POINTER(c_ulong), POINTER(c_ulong)]
winspool.EnumJobsW.restype = c_int

shell32.ShellExecuteW.argtypes = [c_void_p, c_wchar_p, c_wchar_p, c_wchar_p, c_wchar_p, c_int]
shell32.ShellExecuteW.restype = c_void_p

SEE_MASK_CLASSNAME = 0x00000001
SEE_MASK_FLAG_NO_UI = 0x00000400


class SHELLEXECUTEINFOW(Structure):
    """Only used to force a specific alternate handler by ProgID (lpClass)
    when the extension's current default handler has no ``printto`` verb --
    see ``_find_alternate_printto_progid``. The plain ShellExecuteW path
    above still handles the common case."""

    _fields_ = [
        ("cbSize", c_ulong),
        ("fMask", c_ulong),
        ("hwnd", c_void_p),
        ("lpVerb", c_wchar_p),
        ("lpFile", c_wchar_p),
        ("lpParameters", c_wchar_p),
        ("lpDirectory", c_wchar_p),
        ("nShow", c_int),
        ("hInstApp", c_void_p),
        ("lpIDList", c_void_p),
        ("lpClass", c_wchar_p),
        ("hkeyClass", c_void_p),
        ("dwHotKey", c_ulong),
        ("hIconOrMonitor", c_void_p),
        ("hProcess", c_void_p),
    ]


shell32.ShellExecuteExW.argtypes = [POINTER(SHELLEXECUTEINFOW)]
shell32.ShellExecuteExW.restype = c_int


class WinspoolError(RuntimeError):
    """Raised when a winspool.drv/shell32.dll call fails."""


def _check(ok: int, what: str) -> None:
    if not ok:
        err = get_last_error()
        raise WinspoolError(f"{what} failed (GetLastError={err}): {WinError(err)}")


def enum_printers_raw(flags: int):
    """Return (buffer, count) from EnumPrintersW(flags, level=2).

    Returns the live ctypes buffer object, not a `bytes` copy of it (via
    `.raw`). PRINTER_INFO_2W's string fields are pointers Windows wrote
    *into this same buffer* -- casting a `bytes` copy of the data produces a
    struct array whose pointers still reference the original buffer's
    address, but that buffer no longer has a live Python reference keeping
    it allocated, so every string field silently reads as garbage or empty.
    The caller must keep the returned buffer alive as long as the parsed
    structs are in use (parse_printer_info_2w does this by copying strings
    out immediately).
    """
    needed = c_ulong(0)
    returned = c_ulong(0)
    ok = winspool.EnumPrintersW(flags, None, 2, None, 0, byref(needed), byref(returned))
    if not ok and get_last_error() not in (ERROR_INSUFFICIENT_BUFFER, ERROR_SUCCESS):
        raise WinspoolError(f"EnumPrintersW sizing failed: {WinError(get_last_error())}")
    if needed.value == 0:
        return create_string_buffer(0), 0
    buf = create_string_buffer(needed.value)
    ok = winspool.EnumPrintersW(flags, None, 2, buf, needed.value, byref(needed), byref(returned))
    _check(ok, "EnumPrintersW")
    return buf, returned.value


def parse_printer_info_2w(buf, count: int) -> list[PRINTER_INFO_2W]:
    """Cast a live EnumPrintersW(level=2) buffer into an array of structs.

    `buf` must be the buffer object returned by enum_printers_raw, still
    alive -- see that function's docstring for why a `bytes` copy breaks the
    embedded string pointers.
    """
    if count == 0:
        return []
    array_type = PRINTER_INFO_2W * count
    array = cast(buf, POINTER(array_type)).contents
    return [array[i] for i in range(count)]


def get_default_printer() -> str | None:
    """Return the system default printer name, or None if none is set."""
    size = c_ulong(0)
    ok = winspool.GetDefaultPrinterW(None, byref(size))
    if not ok and get_last_error() != ERROR_INSUFFICIENT_BUFFER:
        return None
    if size.value == 0:
        return None
    buf = (c_wchar * size.value)()
    ok = winspool.GetDefaultPrinterW(buf, byref(size))
    if not ok:
        return None
    return buf.value or None


def open_printer(name: str, access: int = PRINTER_ACCESS_USE) -> c_void_p:
    """Open a printer handle with the requested access level.

    `access` must actually be threaded through a PRINTER_DEFAULTS struct --
    passing NULL for the third argument silently grants a lower default
    access level regardless of what the caller asked for, which surfaces
    later as ERROR_ACCESS_DENIED (5) from SetPrinterW rather than here.
    """
    handle = c_void_p()
    defaults = PRINTER_DEFAULTS(pDatatype=None, pDevMode=None, DesiredAccess=access)
    ok = winspool.OpenPrinterW(name, byref(handle), byref(defaults))
    _check(ok, f"OpenPrinterW({name!r}, access={access:#x})")
    return handle


def close_printer(handle: c_void_p) -> None:
    if handle:
        winspool.ClosePrinter(handle)


def build_job_devmode(handle: c_void_p, printer_name: str, mutate) -> tuple:
    """Build a per-job DEVMODE with `mutate` applied, changing nothing globally.

    Returns ``(pointer_to_DEVMODEW, backing_buffer)``. The caller must keep the
    backing buffer referenced for as long as the pointer is used.

    This is the right way to set per-job print options. The obvious-looking
    alternative -- GetPrinterW/SetPrinterW(level=2) -- changes the printer's
    *global* default settings, which requires the "Manage this printer"
    permission (it fails with ERROR_ACCESS_DENIED for an ordinary user) and
    would leak one job's choices into every other application's printing.
    DocumentPropertiesW needs no special rights and is scoped to the document
    we are about to start.

    The buffer is sized from the driver, never from ``sizeof(DEVMODEW)``: a
    real driver appends private data after the public struct (measured at
    15356 bytes for an HP inkjet against a 224-byte public struct), and
    copying only the public part truncates settings the driver depends on.
    """
    size = winspool.DocumentPropertiesW(None, handle, printer_name, None, None, 0)
    if size < 0:
        raise WinspoolError(
            f"DocumentPropertiesW size query failed for {printer_name!r}: {get_last_error()}"
        )
    buf = create_string_buffer(max(size, sizeof(DEVMODEW)))
    dm = cast(buf, POINTER(DEVMODEW))

    rc = winspool.DocumentPropertiesW(None, handle, printer_name, dm, None, DM_OUT_BUFFER)
    if rc < 0:
        raise WinspoolError(
            f"DocumentPropertiesW could not read defaults for {printer_name!r}: {get_last_error()}"
        )

    mutate(dm.contents)

    # Hand it back to the driver to validate and merge: a driver may clamp or
    # refuse a setting its hardware does not support (e.g. duplex on a
    # simplex-only device), and this is where that is resolved rather than at
    # print time.
    rc = winspool.DocumentPropertiesW(
        None, handle, printer_name, dm, dm, DM_IN_BUFFER | DM_OUT_BUFFER
    )
    if rc < 0:
        raise WinspoolError(
            f"DocumentPropertiesW rejected the requested settings for {printer_name!r}: "
            f"{get_last_error()}"
        )
    return dm, buf


def device_capabilities(device: str, port: str, capability: int) -> int:
    """Return DeviceCapabilitiesW(device, port, capability, None, None).

    Returns -1 on failure (the documented sentinel for "not supported" or a
    driver error), rather than raising, since callers treat both the same way
    (fall back to a conservative default).
    """
    result = winspool.DeviceCapabilitiesW(device, port, capability, None, None)
    return result


def _progid_has_printto(progid: str) -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, f"{progid}\\shell\\printto\\command"):
            return True
    except OSError:
        return False


def _find_alternate_printto_progid(file_path: str) -> str | None:
    """Find an installed handler for this file's extension that registers a
    ``printto`` verb.

    Needed because the extension's *current default* handler is not
    guaranteed to support silent printing at all: Windows' built-in PDF
    viewer (MSEdgePDF) registers only an ``open`` verb, so
    ShellExecuteW("printto", ...) fails with SE_ERR_NOASSOC even when a
    print-capable reader (e.g. Adobe Acrobat Reader) is installed and listed
    as an alternate "Open with" choice for the same extension. Both the
    per-user and machine-wide "Open with" lists are checked, since which one
    holds a given ProgID depends on how it was installed.
    """
    ext = Path(file_path).suffix.lower()
    if not ext:
        return None

    candidates: list[str] = []
    for root, subkey in (
        (winreg.HKEY_CURRENT_USER,
         f"Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\FileExts\\{ext}\\OpenWithProgids"),
        (winreg.HKEY_CLASSES_ROOT, f"{ext}\\OpenWithProgids"),
    ):
        try:
            with winreg.OpenKey(root, subkey) as key:
                i = 0
                while True:
                    try:
                        name, _, _ = winreg.EnumValue(key, i)
                    except OSError:
                        break
                    if name and name not in candidates:
                        candidates.append(name)
                    i += 1
        except OSError:
            pass

    for progid in candidates:
        if _progid_has_printto(progid):
            return progid
    return None


def shell_print_to(printer_name: str, file_path: str) -> int:
    """Invoke the shell's ``printto`` verb, returning a ShellExecuteW-style
    result code (<= 32 is an error, see SE_ERR_NOASSOC and friends; anything
    above 32 means a handoff to some application succeeded).

    ``printto`` (not ``print``) is required: the plain ``print`` verb always
    targets the system default printer and ignores the third argument, which
    would make the printer dropdown in the UI silently do nothing whenever it
    was not already the default.

    If the extension's current default handler has no ``printto`` verb at
    all (see ``_find_alternate_printto_progid``), this retries against
    whichever other installed handler for the same extension does have one,
    by forcing that ProgID through ShellExecuteExW's lpClass -- rather than
    failing outright just because the *default* choice happens to be a
    viewer with no print automation.
    """
    result = shell32.ShellExecuteW(None, "printto", file_path, f'"{printer_name}"', None, SW_HIDE)
    code = int(result) if result else 0
    if code > 32:
        return code

    progid = _find_alternate_printto_progid(file_path)
    if progid is None:
        return code

    info = SHELLEXECUTEINFOW()
    info.cbSize = sizeof(SHELLEXECUTEINFOW)
    info.fMask = SEE_MASK_CLASSNAME | SEE_MASK_FLAG_NO_UI
    info.lpVerb = "printto"
    info.lpFile = file_path
    info.lpParameters = f'"{printer_name}"'
    info.lpClass = progid
    info.nShow = SW_HIDE
    ok = shell32.ShellExecuteExW(byref(info))
    if not ok:
        err = get_last_error()
        return err if err else code
    return 33
