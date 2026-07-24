"""Delayed Windows platform primitives for the public SMT controller.

This module intentionally performs no Win32 loading or output at import time so
repository checks can import and compile it on non-Windows hosts.
"""

from __future__ import annotations

import codecs
import ctypes
import errno
import json
import locale
import ntpath
import os
import signal
import stat
import subprocess
import threading
import time
import unicodedata
from collections import deque
from ctypes import wintypes
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import IO, Any, Callable, Literal, Mapping, Sequence


_IS_WINDOWS = os.name == "nt"
_USE_WINDOWS_RENAME = os.name == "nt"
_OUTPUT_READ_CHUNK_SIZE = 4096

_ERROR_LOCK_VIOLATION = 33
_ERROR_IO_PENDING = 997
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

_GENERIC_READ = 0x80000000
_GENERIC_WRITE = 0x40000000
_FILE_SHARE_READ = 0x00000001
_FILE_SHARE_WRITE = 0x00000002
_FILE_SHARE_DELETE = 0x00000004
_OPEN_EXISTING = 3
_OPEN_ALWAYS = 4
_CREATE_NEW = 1
_FILE_ATTRIBUTE_NORMAL = 0x00000080
_FILE_ATTRIBUTE_DIRECTORY = 0x00000010
_FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_LOCKFILE_FAIL_IMMEDIATELY = 0x00000001
_LOCKFILE_EXCLUSIVE_LOCK = 0x00000002

_AT_FDCWD = -100
_RENAME_NOREPLACE = 1

_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_CREATE_SUSPENDED = 0x00000004
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_TH32CS_SNAPTHREAD = 0x00000004
_THREAD_SUSPEND_RESUME = 0x0002
_STILL_ACTIVE = 259

_FOLDERID_DOCUMENTS = "FDD39AD0-238F-46AF-ADB4-6C85480369C7"
_FOLDERID_LOCAL_APP_DATA = "F1B32785-6FBA-4FCF-9D55-7B8E7F157091"


class ManagedProcessEnvironmentError(RuntimeError):
    """The platform cannot provide a required reliable Windows primitive."""


class ManagedProcessTimeoutError(TimeoutError):
    """A managed process exceeded its deadline and its Job was terminated."""


class SmtLockTimeoutError(TimeoutError):
    """A process file lock remained contended until its deadline."""


# Frozen planning documents use these concise names.  Keep the descriptive
# aliases above for callers that want to distinguish process setup failures.
WindowsEnvironmentUnavailable = ManagedProcessEnvironmentError
SmtLockTimeout = SmtLockTimeoutError


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_uint32),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", ctypes.c_ubyte * 8),
    ]

    @classmethod
    def from_string(cls, value: str) -> _GUID:
        import uuid

        raw = uuid.UUID(value).bytes_le
        return cls.from_buffer_copy(raw)


class _OVERLAPPED(ctypes.Structure):
    _fields_ = [
        ("Internal", ctypes.c_size_t),
        ("InternalHigh", ctypes.c_size_t),
        ("Offset", wintypes.DWORD),
        ("OffsetHigh", wintypes.DWORD),
        ("hEvent", wintypes.HANDLE),
    ]


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _THREADENTRY32(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ThreadID", wintypes.DWORD),
        ("th32OwnerProcessID", wintypes.DWORD),
        ("tpBasePri", wintypes.LONG),
        ("tpDeltaPri", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
    ]


class _BY_HANDLE_FILE_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("dwFileAttributes", wintypes.DWORD),
        ("ftCreationTime", wintypes.FILETIME),
        ("ftLastAccessTime", wintypes.FILETIME),
        ("ftLastWriteTime", wintypes.FILETIME),
        ("dwVolumeSerialNumber", wintypes.DWORD),
        ("nFileSizeHigh", wintypes.DWORD),
        ("nFileSizeLow", wintypes.DWORD),
        ("nNumberOfLinks", wintypes.DWORD),
        ("nFileIndexHigh", wintypes.DWORD),
        ("nFileIndexLow", wintypes.DWORD),
    ]


@dataclass(frozen=True)
class _Win32Bindings:
    kernel32: Any
    shell32: Any
    ole32: Any


def _require_windows() -> None:
    if not _IS_WINDOWS:
        raise ManagedProcessEnvironmentError(
            "SMT workflow execution currently requires Windows"
        )


def _prototype(function: Any, argtypes: list[Any], restype: Any) -> Any:
    function.argtypes = argtypes
    function.restype = restype
    return function


@lru_cache(maxsize=1)
def _win32_bindings() -> _Win32Bindings:
    """Load and type all Win32 functions only when first needed."""

    _require_windows()
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        ole32 = ctypes.WinDLL("ole32", use_last_error=True)
    except OSError as exc:
        raise ManagedProcessEnvironmentError(
            f"required Windows API libraries are unavailable: {exc}"
        ) from exc

    _prototype(
        shell32.SHGetKnownFolderPath,
        [ctypes.POINTER(_GUID), wintypes.DWORD, wintypes.HANDLE, ctypes.POINTER(ctypes.c_wchar_p)],
        ctypes.c_long,
    )
    _prototype(ole32.CoTaskMemFree, [ctypes.c_void_p], None)
    _prototype(
        kernel32.CreateFileW,
        [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ],
        wintypes.HANDLE,
    )
    _prototype(
        kernel32.LockFileEx,
        [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(_OVERLAPPED)],
        wintypes.BOOL,
    )
    _prototype(
        kernel32.UnlockFileEx,
        [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(_OVERLAPPED)],
        wintypes.BOOL,
    )
    _prototype(kernel32.CloseHandle, [wintypes.HANDLE], wintypes.BOOL)
    _prototype(
        kernel32.GetFileInformationByHandle,
        [wintypes.HANDLE, ctypes.POINTER(_BY_HANDLE_FILE_INFORMATION)],
        wintypes.BOOL,
    )
    _prototype(
        kernel32.GetFinalPathNameByHandleW,
        [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD],
        wintypes.DWORD,
    )
    _prototype(
        kernel32.SetFilePointerEx,
        [wintypes.HANDLE, ctypes.c_longlong, ctypes.POINTER(ctypes.c_longlong), wintypes.DWORD],
        wintypes.BOOL,
    )
    _prototype(kernel32.SetEndOfFile, [wintypes.HANDLE], wintypes.BOOL)
    _prototype(
        kernel32.WriteFile,
        [wintypes.HANDLE, wintypes.LPCVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID],
        wintypes.BOOL,
    )
    _prototype(kernel32.FlushFileBuffers, [wintypes.HANDLE], wintypes.BOOL)
    _prototype(kernel32.CreateJobObjectW, [wintypes.LPVOID, wintypes.LPCWSTR], wintypes.HANDLE)
    _prototype(
        kernel32.SetInformationJobObject,
        [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD],
        wintypes.BOOL,
    )
    _prototype(
        kernel32.AssignProcessToJobObject,
        [wintypes.HANDLE, wintypes.HANDLE],
        wintypes.BOOL,
    )
    _prototype(kernel32.TerminateJobObject, [wintypes.HANDLE, wintypes.UINT], wintypes.BOOL)
    _prototype(kernel32.TerminateProcess, [wintypes.HANDLE, wintypes.UINT], wintypes.BOOL)
    _prototype(kernel32.CreateToolhelp32Snapshot, [wintypes.DWORD, wintypes.DWORD], wintypes.HANDLE)
    _prototype(kernel32.Thread32First, [wintypes.HANDLE, ctypes.POINTER(_THREADENTRY32)], wintypes.BOOL)
    _prototype(kernel32.Thread32Next, [wintypes.HANDLE, ctypes.POINTER(_THREADENTRY32)], wintypes.BOOL)
    _prototype(kernel32.OpenThread, [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE)
    _prototype(kernel32.ResumeThread, [wintypes.HANDLE], wintypes.DWORD)
    _prototype(kernel32.OpenProcess, [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE)
    _prototype(kernel32.GetExitCodeProcess, [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL)
    _prototype(kernel32.GenerateConsoleCtrlEvent, [wintypes.DWORD, wintypes.DWORD], wintypes.BOOL)
    return _Win32Bindings(kernel32=kernel32, shell32=shell32, ole32=ole32)


def _last_winerror(prefix: str) -> OSError:
    return ctypes.WinError(ctypes.get_last_error(), prefix)


def _known_folder_path(folder_id: str) -> Path:
    bindings = _win32_bindings()
    guid = _GUID.from_string(folder_id)
    raw_path = ctypes.c_wchar_p()
    try:
        result = bindings.shell32.SHGetKnownFolderPath(
            ctypes.byref(guid), 0, None, ctypes.byref(raw_path)
        )
        if result < 0:
            raise ManagedProcessEnvironmentError(
                "Windows Known Folder lookup failed with HRESULT "
                f"0x{result & 0xFFFFFFFF:08X}"
            )
        value = raw_path.value
        if not value:
            raise ManagedProcessEnvironmentError("Windows Known Folder returned an empty path")
        return Path(value)
    finally:
        bindings.ole32.CoTaskMemFree(ctypes.cast(raw_path, ctypes.c_void_p))


def get_documents_path() -> Path:
    """Return the redirected Documents directory via the Known Folder API."""

    return _known_folder_path(_FOLDERID_DOCUMENTS)


def get_local_app_data_path() -> Path:
    """Return Local AppData via the Known Folder API."""

    return _known_folder_path(_FOLDERID_LOCAL_APP_DATA)


def documents_directory() -> Path:
    """Frozen public helper name for the redirected Documents directory."""

    return get_documents_path()


def local_app_data_directory() -> Path:
    """Frozen public helper name for Local AppData."""

    return get_local_app_data_path()


def canonical_windows_path(path: Path | str) -> Path:
    """Map safe Win32 aliases to one ordinary absolute DOS/UNC path."""

    value = os.fspath(path).replace("/", "\\")
    folded = value.casefold()
    namespace_prefix = next(
        (
            prefix
            for prefix in ("\\\\?\\", "\\\\.\\")
            if folded.startswith(prefix.casefold())
        ),
        None,
    )
    if namespace_prefix is not None:
        remainder = value[len(namespace_prefix) :]
        if remainder.casefold().startswith("unc\\"):
            unc_tail = remainder[4:]
            unc_parts = unc_tail.split("\\")
            if (
                len(unc_parts) < 2
                or not unc_parts[0]
                or not unc_parts[1]
                or unc_parts[0] in {".", ".."}
                or unc_parts[1] in {".", ".."}
            ):
                raise ManagedProcessEnvironmentError(
                    f"Windows device namespace has no safe UNC mapping: {path}"
                )
            value = "\\\\" + unc_tail
        elif (
            len(remainder) >= 3
            and remainder[0].isalpha()
            and remainder[1:3] == ":\\"
        ):
            value = remainder
        else:
            raise ManagedProcessEnvironmentError(
                f"Windows device namespace has no safe DOS path mapping: {path}"
            )
    elif folded.startswith(("\\??\\", "\\\\??\\")):
        raise ManagedProcessEnvironmentError(
            f"Windows device namespace is not accepted: {path}"
        )
    return Path(ntpath.abspath(ntpath.normpath(value)))


def windows_path_key(path: Path | str) -> str:
    ordinary = canonical_windows_path(path)
    return unicodedata.normalize(
        "NFC",
        ntpath.normcase(ntpath.normpath(str(ordinary))),
    )


@lru_cache(maxsize=1)
def _renameat2_function() -> Any | None:
    try:
        renameat2 = ctypes.CDLL(None, use_errno=True).renameat2
    except (AttributeError, OSError):
        return None
    renameat2.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    renameat2.restype = ctypes.c_int
    return renameat2


def publish_path_no_replace(source: Path | str, target: Path | str) -> None:
    """Atomically publish one path while refusing an existing destination."""

    if _USE_WINDOWS_RENAME:
        os.rename(source, target)
        return
    renameat2 = _renameat2_function()
    if renameat2 is None:
        raise ManagedProcessEnvironmentError(
            "atomic no-replace rename is unavailable on this platform"
        )
    ctypes.set_errno(0)
    result = renameat2(
        _AT_FDCWD,
        os.fsencode(source),
        _AT_FDCWD,
        os.fsencode(target),
        _RENAME_NOREPLACE,
    )
    if result == 0:
        return
    error = ctypes.get_errno()
    if error == errno.EEXIST:
        raise FileExistsError(error, os.strerror(error), os.fspath(target))
    unsupported = {
        errno.ENOSYS,
        errno.EINVAL,
        getattr(errno, "ENOTSUP", errno.EINVAL),
        getattr(errno, "EOPNOTSUPP", errno.EINVAL),
    }
    if error in unsupported:
        raise ManagedProcessEnvironmentError(
            "atomic no-replace rename is unsupported by this filesystem"
        )
    raise OSError(error, os.strerror(error), os.fspath(source), os.fspath(target))


def _ordinary_windows_path(value: str) -> Path:
    return canonical_windows_path(value)


def _final_path_from_handle(handle: int) -> Path:
    bindings = _win32_bindings()
    size = 512
    while True:
        buffer = ctypes.create_unicode_buffer(size)
        length = bindings.kernel32.GetFinalPathNameByHandleW(
            handle,
            buffer,
            size,
            0,
        )
        if length == 0:
            raise ManagedProcessEnvironmentError(
                str(_last_winerror("GetFinalPathNameByHandleW failed"))
            )
        if length < size:
            return _ordinary_windows_path(buffer.value)
        size = int(length) + 1


def _stat_is_reparse(entry_stat: os.stat_result) -> bool:
    attributes = int(getattr(entry_stat, "st_file_attributes", 0))
    return stat.S_ISLNK(entry_stat.st_mode) or bool(
        attributes & _FILE_ATTRIBUTE_REPARSE_POINT
    )


def _portable_directory_snapshots(
    path: Path | str,
    allowed_root: Path | str,
) -> tuple[Path, tuple[tuple[Path, tuple[int, int]], ...]]:
    target = Path(os.path.abspath(path))
    root = Path(os.path.abspath(allowed_root))
    try:
        relative = target.relative_to(root)
    except ValueError as exc:
        raise ManagedProcessEnvironmentError(
            "portable SMT path is outside its allowed root"
        ) from exc
    candidates = [root]
    current = root
    for part in relative.parts:
        current /= part
        candidates.append(current)
    snapshots: list[tuple[Path, tuple[int, int]]] = []
    for candidate in candidates:
        candidate_stat = candidate.lstat()
        if _stat_is_reparse(candidate_stat):
            raise ManagedProcessEnvironmentError(
                f"portable SMT path contains a link or reparse point: {candidate}"
            )
        if not stat.S_ISDIR(candidate_stat.st_mode):
            raise ManagedProcessEnvironmentError(
                f"portable SMT path contains a non-directory: {candidate}"
            )
        snapshots.append(
            (candidate, (int(candidate_stat.st_dev), int(candidate_stat.st_ino)))
        )
    return target, tuple(snapshots)


def _verify_portable_directory_snapshots(
    snapshots: Sequence[tuple[Path, tuple[int, int]]],
) -> None:
    for candidate, expected in snapshots:
        candidate_stat = candidate.lstat()
        actual = (int(candidate_stat.st_dev), int(candidate_stat.st_ino))
        if (
            _stat_is_reparse(candidate_stat)
            or not stat.S_ISDIR(candidate_stat.st_mode)
            or actual != expected
        ):
            raise ManagedProcessEnvironmentError(
                f"portable SMT directory identity changed: {candidate}"
            )


def _validate_regular_single_link_handle(
    handle: int,
    expected_path: Path,
    expected_parent: Path,
    *,
    label: str,
) -> None:
    bindings = _win32_bindings()
    information = _BY_HANDLE_FILE_INFORMATION()
    if not bindings.kernel32.GetFileInformationByHandle(
        handle,
        ctypes.byref(information),
    ):
        raise ManagedProcessEnvironmentError(
            str(_last_winerror(f"GetFileInformationByHandle failed for {label}"))
        )
    attributes = int(information.dwFileAttributes)
    if attributes & _FILE_ATTRIBUTE_DIRECTORY:
        raise ManagedProcessEnvironmentError(f"{label} is a directory")
    if attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
        raise ManagedProcessEnvironmentError(
            f"{label} is a symlink, junction, or reparse point"
        )
    if int(information.nNumberOfLinks) != 1:
        raise ManagedProcessEnvironmentError(
            f"{label} must have exactly one hardlink"
        )
    final_path = _final_path_from_handle(handle)
    expected_final_path = expected_parent / expected_path.name
    if windows_path_key(final_path) != windows_path_key(expected_final_path):
        raise ManagedProcessEnvironmentError(
            f"{label} handle resolves to a different physical path"
        )
    if windows_path_key(final_path.parent) != windows_path_key(expected_parent):
        raise ManagedProcessEnvironmentError(
            f"{label} handle escaped its pinned parent directory"
        )


def copy_file_exclusive(
    source: Path,
    target: Path,
    allowed_root: Path,
    copier: Callable[[Path, IO[bytes]], None],
) -> None:
    """Create one new regular file and expose only its already-open stream."""

    if not _IS_WINDOWS:
        target = Path(os.path.abspath(target))
        parent_pin = PinnedDirectoryHandle(target.parent, allowed_root)
        parent_pin.acquire()
        descriptor: int | None = None
        try:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            flags |= int(getattr(os, "O_BINARY", 0))
            flags |= int(getattr(os, "O_NOFOLLOW", 0))
            descriptor = os.open(target, flags, 0o600)
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
                raise ManagedProcessEnvironmentError(
                    "portable SMT import target is not a single-link regular file"
                )
            with os.fdopen(descriptor, "wb", closefd=True) as output_stream:
                descriptor = None
                copier(source, output_stream)
                output_stream.flush()
                os.fsync(output_stream.fileno())
                completed = os.fstat(output_stream.fileno())
                if not stat.S_ISREG(completed.st_mode) or completed.st_nlink != 1:
                    raise ManagedProcessEnvironmentError(
                        "portable SMT import target identity changed while open"
                    )
            final_stat = target.lstat()
            if (
                _stat_is_reparse(final_stat)
                or not stat.S_ISREG(final_stat.st_mode)
                or final_stat.st_nlink != 1
                or (int(final_stat.st_dev), int(final_stat.st_ino))
                != (int(completed.st_dev), int(completed.st_ino))
            ):
                raise ManagedProcessEnvironmentError(
                    "portable SMT import target changed after copy"
                )
        finally:
            if descriptor is not None:
                os.close(descriptor)
            parent_pin.release()
        return

    import msvcrt

    target = Path(os.path.abspath(target))
    parent_pin = PinnedDirectoryHandle(target.parent, allowed_root)
    parent_pin.acquire()
    handle: int | None = None
    try:
        bindings = _win32_bindings()
        raw_handle = bindings.kernel32.CreateFileW(
            str(target),
            _GENERIC_WRITE,
            0,
            None,
            _CREATE_NEW,
            _FILE_ATTRIBUTE_NORMAL | _FILE_FLAG_OPEN_REPARSE_POINT,
            None,
        )
        if raw_handle == _INVALID_HANDLE_VALUE:
            raise FileExistsError(str(_last_winerror("secure SMT target creation failed")))
        handle = int(raw_handle)
        if parent_pin.final_path is None:
            raise ManagedProcessEnvironmentError(
                "secure SMT target parent directory was not pinned"
            )
        _validate_regular_single_link_handle(
            handle,
            target,
            parent_pin.final_path,
            label="secure SMT import target",
        )
        descriptor = msvcrt.open_osfhandle(handle, os.O_WRONLY | os.O_BINARY)
        handle = None
        with os.fdopen(descriptor, "wb", closefd=True) as output_stream:
            copier(source, output_stream)
            output_stream.flush()
            os.fsync(output_stream.fileno())
            information = os.fstat(output_stream.fileno())
            if information.st_nlink != 1:
                raise ManagedProcessEnvironmentError(
                    "secure SMT import target gained another hardlink while open"
                )
    finally:
        if handle is not None:
            _win32_bindings().kernel32.CloseHandle(handle)
        parent_pin.release()


class PinnedDirectoryHandle:
    """Hold a verified directory object open without delete sharing."""

    def __init__(self, path: Path | str, workspace: Path | str) -> None:
        self.path = Path(path)
        self.workspace = Path(workspace)
        self._handle: int | None = None
        self._handles: list[int] = []
        self.final_path: Path | None = None
        self.cleanup_errors: list[str] = []
        self._portable_snapshots: tuple[tuple[Path, tuple[int, int]], ...] = ()

    def acquire(self) -> PinnedDirectoryHandle:
        if self._handles:
            raise RuntimeError("directory handle is already acquired")
        if self._portable_snapshots:
            raise RuntimeError("directory handle is already acquired")
        if not _IS_WINDOWS:
            target, snapshots = _portable_directory_snapshots(
                self.path,
                self.workspace,
            )
            self._portable_snapshots = snapshots
            self.final_path = target
            return self
        bindings = _win32_bindings()
        workspace = Path(os.path.abspath(self.workspace))
        target = Path(os.path.abspath(self.path))
        try:
            common = os.path.commonpath(
                (os.path.normcase(str(target)), os.path.normcase(str(workspace)))
            )
            if os.path.normcase(common) != os.path.normcase(str(workspace)):
                raise ValueError("output open target is outside its SMT workspace")
            relative = target.relative_to(workspace)
            candidates = [workspace]
            current = workspace
            for part in relative.parts:
                current /= part
                candidates.append(current)

            physical_workspace: Path | None = None
            for candidate in candidates:
                handle = bindings.kernel32.CreateFileW(
                    str(candidate),
                    _GENERIC_READ,
                    _FILE_SHARE_READ | _FILE_SHARE_WRITE,
                    None,
                    _OPEN_EXISTING,
                    _FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT,
                    None,
                )
                if handle == _INVALID_HANDLE_VALUE:
                    raise OSError(
                        str(_last_winerror("could not pin output directory"))
                    )
                self._handles.append(int(handle))
                information = _BY_HANDLE_FILE_INFORMATION()
                if not bindings.kernel32.GetFileInformationByHandle(
                    handle,
                    ctypes.byref(information),
                ):
                    raise OSError(
                        str(_last_winerror("GetFileInformationByHandle failed"))
                    )
                attributes = int(information.dwFileAttributes)
                if not attributes & _FILE_ATTRIBUTE_DIRECTORY:
                    raise ValueError("output open target contains a non-directory")
                if attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
                    raise ValueError("output open target contains a reparse point")
                final_path = _final_path_from_handle(int(handle))
                if physical_workspace is None:
                    physical_workspace = final_path
                try:
                    physical_common = os.path.commonpath(
                        (
                            os.path.normcase(str(final_path)),
                            os.path.normcase(str(physical_workspace)),
                        )
                    )
                except ValueError as exc:
                    raise ValueError(
                        "output open target is outside its SMT workspace"
                    ) from exc
                if os.path.normcase(physical_common) != os.path.normcase(
                    str(physical_workspace)
                ):
                    raise ValueError(
                        "output open target is outside its SMT workspace"
                    )
            self._handle = self._handles[-1]
            self.final_path = _final_path_from_handle(self._handle)
            return self
        except BaseException as exc:
            cleanup_errors = self._close_owned_handles()
            if cleanup_errors and hasattr(exc, "add_note"):
                exc.add_note("; ".join(cleanup_errors))
            raise

    def _close_owned_handles(self) -> list[str]:
        handles = self._handles
        if not handles:
            return []
        self._handles = []
        self._handle = None
        self.final_path = None
        bindings = _win32_bindings()
        failures: list[str] = []
        for handle in reversed(handles):
            if not bindings.kernel32.CloseHandle(handle):
                failures.append(
                    str(_last_winerror(f"CloseHandle failed for directory handle {handle}"))
                )
        self.cleanup_errors.extend(failures)
        return failures

    def release(self) -> None:
        if self._portable_snapshots:
            snapshots = self._portable_snapshots
            self._portable_snapshots = ()
            self.final_path = None
            _verify_portable_directory_snapshots(snapshots)
            return
        failures = self._close_owned_handles()
        if failures:
            raise ManagedProcessEnvironmentError(
                "pinned output directory cleanup failed: " + "; ".join(failures)
            )

    def __enter__(self) -> PinnedDirectoryHandle:
        return self.acquire()

    def __exit__(self, _exc_type: Any, exc: Any, _traceback: Any) -> None:
        try:
            self.release()
        except (KeyboardInterrupt, SystemExit, GeneratorExit):
            raise
        except BaseException as cleanup_error:
            if exc is None:
                raise
            diagnostic = (
                "pinned output directory cleanup also failed: "
                f"{type(cleanup_error).__name__}: {cleanup_error}"
            )
            if not self.cleanup_errors:
                self.cleanup_errors.append(diagnostic)
            if hasattr(exc, "add_note"):
                exc.add_note(diagnostic)


def validate_regular_single_link_file(
    path: Path | str,
    allowed_root: Path | str,
    *,
    label: str = "SMT file",
) -> Path:
    """Validate one regular file through an already-open OS handle.

    The parent directory remains pinned without delete sharing while the file
    handle is checked.  This is the common boundary for security-sensitive
    managed-store metadata and payload validation; callers must not replace it
    with a path-only ``resolve()`` check.
    """

    target = Path(os.path.abspath(path))
    root = Path(os.path.abspath(allowed_root))
    parent_pin = PinnedDirectoryHandle(target.parent, root)
    parent_pin.acquire()
    descriptor: int | None = None
    handle: int | None = None
    try:
        if not _IS_WINDOWS:
            flags = os.O_RDONLY | int(getattr(os, "O_BINARY", 0))
            flags |= int(getattr(os, "O_NOFOLLOW", 0))
            descriptor = os.open(target, flags)
            information = os.fstat(descriptor)
            if not stat.S_ISREG(information.st_mode):
                raise ManagedProcessEnvironmentError(f"{label} is not a regular file")
            if int(information.st_nlink) != 1:
                raise ManagedProcessEnvironmentError(
                    f"{label} must have exactly one hardlink"
                )
            current = target.lstat()
            if (
                _stat_is_reparse(current)
                or not stat.S_ISREG(current.st_mode)
                or int(current.st_nlink) != 1
                or (int(current.st_dev), int(current.st_ino))
                != (int(information.st_dev), int(information.st_ino))
            ):
                raise ManagedProcessEnvironmentError(
                    f"{label} path identity changed while it was open"
                )
            return target

        bindings = _win32_bindings()
        raw_handle = bindings.kernel32.CreateFileW(
            str(target),
            _GENERIC_READ,
            _FILE_SHARE_READ | _FILE_SHARE_WRITE,
            None,
            _OPEN_EXISTING,
            _FILE_FLAG_OPEN_REPARSE_POINT,
            None,
        )
        if raw_handle == _INVALID_HANDLE_VALUE:
            raise ManagedProcessEnvironmentError(
                str(_last_winerror(f"could not open {label}"))
            )
        handle = int(raw_handle)
        if parent_pin.final_path is None:
            raise ManagedProcessEnvironmentError(
                f"{label} parent directory was not pinned"
            )
        _validate_regular_single_link_handle(
            handle,
            target,
            parent_pin.final_path,
            label=label,
        )
        return _final_path_from_handle(handle)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if handle is not None:
            _win32_bindings().kernel32.CloseHandle(handle)
        parent_pin.release()


def read_regular_single_link_bytes(
    path: Path | str,
    allowed_root: Path | str,
    *,
    label: str = "SMT file",
) -> bytes:
    """Read one regular single-link file through the validated open handle."""

    target = Path(os.path.abspath(path))
    root = Path(os.path.abspath(allowed_root))
    parent_pin = PinnedDirectoryHandle(target.parent, root)
    parent_pin.acquire()
    descriptor: int | None = None
    handle: int | None = None
    try:
        if not _IS_WINDOWS:
            flags = os.O_RDONLY | int(getattr(os, "O_BINARY", 0))
            flags |= int(getattr(os, "O_NOFOLLOW", 0))
            descriptor = os.open(target, flags)
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode) or int(opened.st_nlink) != 1:
                raise ManagedProcessEnvironmentError(
                    f"{label} is not a single-link regular file"
                )
            with os.fdopen(descriptor, "rb", closefd=True) as input_stream:
                descriptor = None
                payload = input_stream.read()
                completed = os.fstat(input_stream.fileno())
            current = target.lstat()
            if (
                _stat_is_reparse(current)
                or not stat.S_ISREG(current.st_mode)
                or int(current.st_nlink) != 1
                or (int(current.st_dev), int(current.st_ino))
                != (int(completed.st_dev), int(completed.st_ino))
            ):
                raise ManagedProcessEnvironmentError(
                    f"{label} path identity changed while it was read"
                )
            return payload

        import msvcrt

        bindings = _win32_bindings()
        raw_handle = bindings.kernel32.CreateFileW(
            str(target),
            _GENERIC_READ,
            _FILE_SHARE_READ,
            None,
            _OPEN_EXISTING,
            _FILE_FLAG_OPEN_REPARSE_POINT,
            None,
        )
        if raw_handle == _INVALID_HANDLE_VALUE:
            raise ManagedProcessEnvironmentError(
                str(_last_winerror(f"could not open {label} for reading"))
            )
        handle = int(raw_handle)
        if parent_pin.final_path is None:
            raise ManagedProcessEnvironmentError(
                f"{label} parent directory was not pinned"
            )
        _validate_regular_single_link_handle(
            handle,
            target,
            parent_pin.final_path,
            label=label,
        )
        descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
        handle = None
        with os.fdopen(descriptor, "rb", closefd=True) as input_stream:
            descriptor = None
            return input_stream.read()
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if handle is not None:
            _win32_bindings().kernel32.CloseHandle(handle)
        parent_pin.release()


def remove_regular_tree(
    path: Path | str,
    allowed_root: Path | str,
    *,
    label: str = "SMT deletion tree",
) -> None:
    """Remove one validated tree without following reparse points or hardlinks."""

    target = Path(os.path.abspath(path))
    root = Path(os.path.abspath(allowed_root))
    if windows_path_key(target) == windows_path_key(root):
        raise ManagedProcessEnvironmentError(
            f"{label} cannot be the allowed root itself"
        )
    directories: list[Path] = []
    files: list[Path] = []
    with PinnedDirectoryHandle(target, root):
        for current, directory_names, file_names in os.walk(
            target,
            topdown=True,
            followlinks=False,
        ):
            current_path = Path(current)
            for name in directory_names:
                directory = current_path / name
                entry_stat = directory.lstat()
                if (
                    directory.is_symlink()
                    or _stat_is_reparse(entry_stat)
                    or not stat.S_ISDIR(entry_stat.st_mode)
                ):
                    raise ManagedProcessEnvironmentError(
                        f"{label} contains an unsafe directory: {directory}"
                    )
                directories.append(directory)
            for name in file_names:
                file_path = current_path / name
                validate_regular_single_link_file(
                    file_path,
                    target,
                    label=f"{label} file",
                )
                files.append(file_path)
    for file_path in files:
        validate_regular_single_link_file(
            file_path,
            target,
            label=f"{label} file",
        ).unlink()
    for directory in sorted(
        directories,
        key=lambda item: len(item.parts),
        reverse=True,
    ):
        entry_stat = directory.lstat()
        if (
            directory.is_symlink()
            or _stat_is_reparse(entry_stat)
            or not stat.S_ISDIR(entry_stat.st_mode)
        ):
            raise ManagedProcessEnvironmentError(
                f"{label} directory changed before deletion: {directory}"
            )
        directory.rmdir()
    with PinnedDirectoryHandle(target, root):
        pass
    target.rmdir()


@dataclass(frozen=True)
class _BoundImportEntry:
    relative_path: str
    entry_type: Literal["file", "directory"]
    identity: tuple[int, int]
    handle: int | None = None


def _win32_handle_identity(handle: int) -> tuple[int, int]:
    information = _BY_HANDLE_FILE_INFORMATION()
    if not _win32_bindings().kernel32.GetFileInformationByHandle(
        handle,
        ctypes.byref(information),
    ):
        raise ManagedProcessEnvironmentError(
            str(_last_winerror("GetFileInformationByHandle failed for import binding"))
        )
    file_index = (int(information.nFileIndexHigh) << 32) | int(
        information.nFileIndexLow
    )
    return int(information.dwVolumeSerialNumber), file_index


def _open_win32_import_entry(
    path: Path,
    entry_type: Literal["file", "directory"],
    *,
    allow_rename: bool,
) -> tuple[int, tuple[int, int]]:
    share_mode = _FILE_SHARE_READ
    if allow_rename:
        share_mode |= _FILE_SHARE_DELETE
        if entry_type == "directory":
            share_mode |= _FILE_SHARE_WRITE
    flags = _FILE_FLAG_OPEN_REPARSE_POINT
    if entry_type == "directory":
        flags |= _FILE_FLAG_BACKUP_SEMANTICS
    bindings = _win32_bindings()
    raw_handle = bindings.kernel32.CreateFileW(
        str(path),
        _GENERIC_READ,
        share_mode,
        None,
        _OPEN_EXISTING,
        flags,
        None,
    )
    if raw_handle == _INVALID_HANDLE_VALUE:
        raise ManagedProcessEnvironmentError(
            str(_last_winerror(f"could not bind SMT import entry: {path}"))
        )
    handle = int(raw_handle)
    try:
        information = _BY_HANDLE_FILE_INFORMATION()
        if not bindings.kernel32.GetFileInformationByHandle(
            handle,
            ctypes.byref(information),
        ):
            raise ManagedProcessEnvironmentError(
                str(_last_winerror("GetFileInformationByHandle failed for import entry"))
            )
        attributes = int(information.dwFileAttributes)
        if attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
            raise ManagedProcessEnvironmentError(
                f"SMT import entry is a link or reparse point: {path}"
            )
        is_directory = bool(attributes & _FILE_ATTRIBUTE_DIRECTORY)
        if is_directory != (entry_type == "directory"):
            raise ManagedProcessEnvironmentError(
                f"SMT import entry type changed: {path}"
            )
        if entry_type == "file" and int(information.nNumberOfLinks) != 1:
            raise ManagedProcessEnvironmentError(
                f"SMT import file must have exactly one hardlink: {path}"
            )
        return handle, _win32_handle_identity(handle)
    except BaseException:
        bindings.kernel32.CloseHandle(handle)
        raise


class PinnedImportTree:
    """Bind every staged object across verification, publication, and session write."""

    def __init__(
        self,
        root: Path | str,
        allowed_root: Path | str,
        entries: Sequence[tuple[str, Literal["file", "directory"]]],
        *,
        root_type: Literal["file", "directory"],
        allow_rename: bool,
    ) -> None:
        self.root = Path(os.path.abspath(root))
        self.allowed_root = Path(os.path.abspath(allowed_root))
        self.entries = tuple(entries)
        self.root_type = root_type
        self.allow_rename = allow_rename
        self._bound: tuple[_BoundImportEntry, ...] = ()

    def _paths(
        self, root: Path
    ) -> tuple[tuple[str, Literal["file", "directory"], Path], ...]:
        rows: list[tuple[str, Literal["file", "directory"], Path]] = [
            ("", self.root_type, root)
        ]
        rows.extend(
            (
                relative,
                entry_type,
                root.joinpath(*relative.split("/")),
            )
            for relative, entry_type in self.entries
        )
        return tuple(rows)

    def acquire(self) -> PinnedImportTree:
        if self._bound:
            raise RuntimeError("SMT import tree is already bound")
        try:
            self.root.relative_to(self.allowed_root)
        except ValueError as exc:
            raise ManagedProcessEnvironmentError(
                "SMT import tree is outside its allowed root"
            ) from exc
        bound: list[_BoundImportEntry] = []
        try:
            for relative, entry_type, path in self._paths(self.root):
                if _IS_WINDOWS:
                    handle, identity = _open_win32_import_entry(
                        path,
                        entry_type,
                        allow_rename=self.allow_rename,
                    )
                else:
                    path_stat = path.lstat()
                    if _stat_is_reparse(path_stat):
                        raise ManagedProcessEnvironmentError(
                            f"portable SMT import entry is a link: {path}"
                        )
                    is_directory = stat.S_ISDIR(path_stat.st_mode)
                    if is_directory != (entry_type == "directory"):
                        raise ManagedProcessEnvironmentError(
                            f"portable SMT import entry type changed: {path}"
                        )
                    if entry_type == "file" and (
                        not stat.S_ISREG(path_stat.st_mode) or path_stat.st_nlink != 1
                    ):
                        raise ManagedProcessEnvironmentError(
                            f"portable SMT import file is not single-link: {path}"
                        )
                    handle = None
                    identity = (int(path_stat.st_dev), int(path_stat.st_ino))
                bound.append(
                    _BoundImportEntry(relative, entry_type, identity, handle)
                )
        except BaseException:
            for entry in reversed(bound):
                if entry.handle is not None:
                    _win32_bindings().kernel32.CloseHandle(entry.handle)
            raise
        self._bound = tuple(bound)
        return self

    def identity_map(self) -> dict[str, tuple[int, int]]:
        return {entry.relative_path: entry.identity for entry in self._bound}

    def verify(self, root: Path | str) -> None:
        if not self._bound:
            raise RuntimeError("SMT import tree is not bound")
        selected_root = Path(os.path.abspath(root))
        expected_rows = {
            relative: (entry_type, path)
            for relative, entry_type, path in self._paths(selected_root)
        }
        for entry in self._bound:
            entry_type, path = expected_rows[entry.relative_path]
            if _IS_WINDOWS:
                validation_handle, actual_identity = _open_win32_import_entry(
                    path,
                    entry_type,
                    allow_rename=False,
                )
                _win32_bindings().kernel32.CloseHandle(validation_handle)
            else:
                try:
                    path_stat = path.lstat()
                except OSError as exc:
                    raise ManagedProcessEnvironmentError(
                        f"portable SMT import entry became unavailable: {path}"
                    ) from exc
                if _stat_is_reparse(path_stat):
                    raise ManagedProcessEnvironmentError(
                        f"portable SMT import entry became a link: {path}"
                    )
                is_directory = stat.S_ISDIR(path_stat.st_mode)
                if is_directory != (entry_type == "directory"):
                    raise ManagedProcessEnvironmentError(
                        f"portable SMT import entry type changed: {path}"
                    )
                if entry_type == "file" and (
                    not stat.S_ISREG(path_stat.st_mode) or path_stat.st_nlink != 1
                ):
                    raise ManagedProcessEnvironmentError(
                        f"portable SMT import file identity changed: {path}"
                    )
                actual_identity = (int(path_stat.st_dev), int(path_stat.st_ino))
            if actual_identity != entry.identity:
                raise ManagedProcessEnvironmentError(
                    f"SMT import entry identity changed: {path}"
                )
        if self.root_type != "directory":
            return

        expected_types = {
            relative: entry_type
            for relative, (entry_type, _path) in expected_rows.items()
        }
        actual_types: dict[str, Literal["file", "directory"]] = {
            "": "directory"
        }
        expected_directories = sorted(
            relative
            for relative, entry_type in expected_types.items()
            if entry_type == "directory"
        )
        for relative in expected_directories:
            directory = expected_rows[relative][1]
            try:
                with os.scandir(directory) as children:
                    child_rows = sorted(children, key=lambda child: child.name)
            except OSError as exc:
                raise ManagedProcessEnvironmentError(
                    f"could not enumerate bound SMT import directory: {directory}"
                ) from exc
            for child in child_rows:
                child_path = directory / child.name
                child_relative = (
                    f"{relative}/{child.name}" if relative else child.name
                )
                try:
                    child_stat = child.stat(follow_symlinks=False)
                except OSError as exc:
                    raise ManagedProcessEnvironmentError(
                        f"could not inspect bound SMT import entry: {child_path}"
                    ) from exc
                if _stat_is_reparse(child_stat):
                    raise ManagedProcessEnvironmentError(
                        f"SMT import entry is a link or reparse point: {child_path}"
                    )
                if stat.S_ISDIR(child_stat.st_mode):
                    child_type: Literal["file", "directory"] = "directory"
                elif stat.S_ISREG(child_stat.st_mode):
                    child_type = "file"
                else:
                    raise ManagedProcessEnvironmentError(
                        f"SMT import entry has an unsupported type: {child_path}"
                    )
                actual_types[child_relative] = child_type

        if actual_types != expected_types:
            added = sorted(set(actual_types) - set(expected_types))
            deleted = sorted(set(expected_types) - set(actual_types))
            type_changed = sorted(
                relative
                for relative in set(actual_types) & set(expected_types)
                if actual_types[relative] != expected_types[relative]
            )
            differences: list[str] = []
            if added:
                differences.append("added=" + ",".join(added))
            if deleted:
                differences.append("deleted=" + ",".join(deleted))
            if type_changed:
                differences.append("type_changed=" + ",".join(type_changed))
            raise ManagedProcessEnvironmentError(
                "SMT import directory tree changed: " + "; ".join(differences)
            )

    def release(self) -> None:
        bound = self._bound
        self._bound = ()
        failures: list[str] = []
        if _IS_WINDOWS:
            bindings = _win32_bindings()
            for entry in reversed(bound):
                if entry.handle is not None and not bindings.kernel32.CloseHandle(
                    entry.handle
                ):
                    failures.append(
                        str(_last_winerror("CloseHandle failed for import binding"))
                    )
        if failures:
            raise ManagedProcessEnvironmentError(
                "SMT import binding cleanup failed: " + "; ".join(failures)
            )

    def __enter__(self) -> PinnedImportTree:
        return self.acquire()

    def __exit__(self, _exc_type: Any, _exc: Any, _traceback: Any) -> None:
        self.release()


def _pin_or_create_directory(
    path: Path | str,
    allowed_root: Path | str,
) -> PinnedDirectoryHandle:
    target = Path(os.path.abspath(path))
    root = Path(os.path.abspath(allowed_root))
    try:
        common = os.path.commonpath(
            (os.path.normcase(str(target)), os.path.normcase(str(root)))
        )
    except ValueError as exc:
        raise ManagedProcessEnvironmentError(
            "SMT lock parent is outside its allowed root"
        ) from exc
    if os.path.normcase(common) != os.path.normcase(str(root)):
        raise ManagedProcessEnvironmentError(
            "SMT lock parent is outside its allowed root"
        )

    missing: list[Path] = []
    current = target
    while not os.path.lexists(current):
        if current == root:
            raise ManagedProcessEnvironmentError(
                "SMT lock allowed root must already exist"
            )
        missing.append(current)
        parent = current.parent
        if parent == current:
            raise ManagedProcessEnvironmentError(
                "SMT lock parent has no existing ancestor"
            )
        current = parent

    pin = PinnedDirectoryHandle(current, root)
    pin.acquire()
    created: list[Path] = []
    try:
        for child in reversed(missing):
            was_created = False
            try:
                child.mkdir()
                was_created = True
            except FileExistsError:
                pass
            child_pin = PinnedDirectoryHandle(child, root)
            try:
                child_pin.acquire()
            except BaseException:
                if was_created:
                    child.rmdir()
                raise
            old_pin = pin
            pin = child_pin
            old_pin.release()
            if was_created:
                created.append(child)
        return pin
    except BaseException:
        pin.release()
        for child in reversed(created):
            try:
                child.rmdir()
            except OSError:
                pass
        raise


class SmtProcessFileLock:
    """A shared or exclusive process lock whose ownership is a Win32 handle."""

    def __init__(
        self,
        path: Path | str,
        mode: Literal["shared", "exclusive"],
        timeout_seconds: float,
        *,
        command: str | None = None,
        poll_interval_seconds: float = 0.025,
        allowed_root: Path | str | None = None,
    ) -> None:
        if timeout_seconds < 0:
            raise ValueError("timeout_seconds must be non-negative")
        if mode not in {"shared", "exclusive"}:
            raise ValueError("lock mode must be 'shared' or 'exclusive'")
        self.path = (
            canonical_windows_path(path)
            if _IS_WINDOWS
            else Path(os.path.abspath(path))
        )
        self.mode = mode
        self.exclusive = mode == "exclusive"
        self.timeout_seconds = timeout_seconds
        self.command = command
        self.poll_interval_seconds = poll_interval_seconds
        selected_root = allowed_root or Path(self.path.anchor)
        self.allowed_root = (
            canonical_windows_path(selected_root)
            if _IS_WINDOWS
            else Path(os.path.abspath(selected_root))
        )
        self._handle: int | None = None
        self._overlapped: _OVERLAPPED | None = None
        self._parent_pin: PinnedDirectoryHandle | None = None

    def _validate_lock_handle(self, handle: int) -> None:
        if self._parent_pin is None or self._parent_pin.final_path is None:
            raise ManagedProcessEnvironmentError(
                "SMT lock parent directory was not pinned"
            )
        _validate_regular_single_link_handle(
            handle,
            self.path,
            self._parent_pin.final_path,
            label="SMT lock file",
        )

    def acquire(self) -> SmtProcessFileLock:
        if self._handle is not None:
            raise RuntimeError("lock is already acquired")
        bindings = _win32_bindings()
        parent_pin = _pin_or_create_directory(self.path.parent, self.allowed_root)
        self._parent_pin = parent_pin
        handle = bindings.kernel32.CreateFileW(
            str(self.path),
            _GENERIC_READ | _GENERIC_WRITE,
            _FILE_SHARE_READ | _FILE_SHARE_WRITE,
            None,
            _OPEN_ALWAYS,
            _FILE_ATTRIBUTE_NORMAL | _FILE_FLAG_OPEN_REPARSE_POINT,
            None,
        )
        if handle == _INVALID_HANDLE_VALUE:
            self._parent_pin = None
            parent_pin.release()
            raise ManagedProcessEnvironmentError(str(_last_winerror("CreateFileW failed")))

        flags = _LOCKFILE_FAIL_IMMEDIATELY
        if self.exclusive:
            flags |= _LOCKFILE_EXCLUSIVE_LOCK
        overlapped = _OVERLAPPED()
        deadline = time.monotonic() + self.timeout_seconds
        try:
            self._validate_lock_handle(int(handle))
            while True:
                if bindings.kernel32.LockFileEx(
                    handle, flags, 0, 1, 0, ctypes.byref(overlapped)
                ):
                    self._handle = int(handle)
                    self._overlapped = overlapped
                    if self.exclusive:
                        self._write_metadata()
                    return self
                error = ctypes.get_last_error()
                if error not in {_ERROR_LOCK_VIOLATION, _ERROR_IO_PENDING}:
                    raise ManagedProcessEnvironmentError(
                        str(ctypes.WinError(error, "LockFileEx failed"))
                    )
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise SmtLockTimeoutError(f"timed out waiting for lock: {self.path}")
                time.sleep(min(self.poll_interval_seconds, remaining))
        except BaseException:
            self._handle = None
            self._overlapped = None
            bindings.kernel32.CloseHandle(handle)
            parent = self._parent_pin
            self._parent_pin = None
            if parent is not None:
                parent.release()
            raise

    def _write_metadata(self) -> None:
        if self._handle is None or not self.exclusive:
            return
        bindings = _win32_bindings()
        payload = json.dumps(
            {
                "pid": os.getpid(),
                "command": self.command,
                "started_at": datetime.now(timezone.utc).isoformat(),
            },
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
        if not bindings.kernel32.SetFilePointerEx(self._handle, 0, None, 0):
            raise ManagedProcessEnvironmentError(str(_last_winerror("SetFilePointerEx failed")))
        if not bindings.kernel32.SetEndOfFile(self._handle):
            raise ManagedProcessEnvironmentError(str(_last_winerror("SetEndOfFile failed")))
        buffer = ctypes.create_string_buffer(payload)
        written = wintypes.DWORD()
        if not bindings.kernel32.WriteFile(
            self._handle,
            buffer,
            len(payload),
            ctypes.byref(written),
            None,
        ) or written.value != len(payload):
            raise ManagedProcessEnvironmentError(str(_last_winerror("WriteFile failed")))
        if not bindings.kernel32.FlushFileBuffers(self._handle):
            raise ManagedProcessEnvironmentError(str(_last_winerror("FlushFileBuffers failed")))

    def release(self) -> None:
        handle = self._handle
        overlapped = self._overlapped
        if handle is None:
            return
        self._handle = None
        self._overlapped = None
        bindings = _win32_bindings()
        try:
            if overlapped is not None and not bindings.kernel32.UnlockFileEx(
                handle, 0, 1, 0, ctypes.byref(overlapped)
            ):
                raise ManagedProcessEnvironmentError(str(_last_winerror("UnlockFileEx failed")))
        finally:
            bindings.kernel32.CloseHandle(handle)
            parent = self._parent_pin
            self._parent_pin = None
            if parent is not None:
                parent.release()

    def __enter__(self) -> SmtProcessFileLock:
        return self.acquire()

    def __exit__(self, _exc_type: Any, _exc: Any, _traceback: Any) -> None:
        self.release()


def process_file_lock_is_available(
    path: Path | str,
    allowed_root: Path | str,
) -> bool:
    """Probe one existing lock byte without creating or rewriting lock state."""

    candidate = Path(os.path.abspath(path))
    root = Path(os.path.abspath(allowed_root))
    if not os.path.lexists(candidate):
        return True
    if not _IS_WINDOWS:
        # The production managed store is Windows-only.  Portable tests can
        # safely report an existing lock file as unavailable rather than
        # claiming an unimplemented probe succeeded.
        return False
    parent = PinnedDirectoryHandle(candidate.parent, root)
    parent.acquire()
    handle: int | None = None
    overlapped = _OVERLAPPED()
    locked = False
    try:
        bindings = _win32_bindings()
        raw_handle = bindings.kernel32.CreateFileW(
            str(candidate),
            _GENERIC_READ | _GENERIC_WRITE,
            _FILE_SHARE_READ | _FILE_SHARE_WRITE,
            None,
            _OPEN_EXISTING,
            _FILE_ATTRIBUTE_NORMAL | _FILE_FLAG_OPEN_REPARSE_POINT,
            None,
        )
        if raw_handle == _INVALID_HANDLE_VALUE:
            raise ManagedProcessEnvironmentError(
                str(_last_winerror("could not open SMT lock for read-only probe"))
            )
        handle = int(raw_handle)
        if parent.final_path is None:
            raise ManagedProcessEnvironmentError(
                "SMT lock probe parent directory was not pinned"
            )
        _validate_regular_single_link_handle(
            handle,
            candidate,
            parent.final_path,
            label="SMT lock probe file",
        )
        flags = _LOCKFILE_FAIL_IMMEDIATELY | _LOCKFILE_EXCLUSIVE_LOCK
        if bindings.kernel32.LockFileEx(
            handle,
            flags,
            0,
            1,
            0,
            ctypes.byref(overlapped),
        ):
            locked = True
            return True
        error = ctypes.get_last_error()
        if error in {_ERROR_LOCK_VIOLATION, _ERROR_IO_PENDING}:
            return False
        raise ManagedProcessEnvironmentError(
            str(ctypes.WinError(error, "SMT lock read-only probe failed"))
        )
    finally:
        if handle is not None:
            if locked:
                _win32_bindings().kernel32.UnlockFileEx(
                    handle,
                    0,
                    1,
                    0,
                    ctypes.byref(overlapped),
                )
            _win32_bindings().kernel32.CloseHandle(handle)
        parent.release()


def _create_kill_on_close_job() -> int:
    bindings = _win32_bindings()
    job = bindings.kernel32.CreateJobObjectW(None, None)
    if not job:
        raise ManagedProcessEnvironmentError(str(_last_winerror("CreateJobObjectW failed")))
    information = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    information.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not bindings.kernel32.SetInformationJobObject(
        job,
        _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
        ctypes.byref(information),
        ctypes.sizeof(information),
    ):
        error = _last_winerror("SetInformationJobObject failed")
        bindings.kernel32.CloseHandle(job)
        raise ManagedProcessEnvironmentError(str(error))
    return int(job)


def _assign_process_to_job(job_handle: int, process_handle: int) -> None:
    bindings = _win32_bindings()
    if not bindings.kernel32.AssignProcessToJobObject(job_handle, process_handle):
        raise OSError(str(_last_winerror("AssignProcessToJobObject failed")))


def _open_primary_thread(process_id: int) -> int:
    bindings = _win32_bindings()
    snapshot = bindings.kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPTHREAD, 0)
    if snapshot == _INVALID_HANDLE_VALUE:
        raise OSError(str(_last_winerror("CreateToolhelp32Snapshot failed")))
    try:
        entry = _THREADENTRY32()
        entry.dwSize = ctypes.sizeof(entry)
        found = bindings.kernel32.Thread32First(snapshot, ctypes.byref(entry))
        while found:
            if entry.th32OwnerProcessID == process_id:
                thread = bindings.kernel32.OpenThread(
                    _THREAD_SUSPEND_RESUME, False, entry.th32ThreadID
                )
                if not thread:
                    raise OSError(str(_last_winerror("OpenThread failed")))
                return int(thread)
            entry.dwSize = ctypes.sizeof(entry)
            found = bindings.kernel32.Thread32Next(snapshot, ctypes.byref(entry))
    finally:
        bindings.kernel32.CloseHandle(snapshot)
    raise OSError(f"suspended primary thread not found for process {process_id}")


def _resume_primary_thread(process_id: int) -> None:
    bindings = _win32_bindings()
    thread = _open_primary_thread(process_id)
    try:
        if bindings.kernel32.ResumeThread(thread) == 0xFFFFFFFF:
            raise OSError(str(_last_winerror("ResumeThread failed")))
    finally:
        bindings.kernel32.CloseHandle(thread)


def _taskkill_tree(process_id: int) -> None:
    try:
        subprocess.run(
            ["taskkill", "/PID", str(process_id), "/T", "/F"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        # This is an explicitly last-resort cleanup path.  The caller still
        # owns the Job/process handle and continues its own bounded cleanup.
        return


def _terminate_unresumed_process(process: subprocess.Popen[Any], job_handle: int) -> None:
    bindings = _win32_bindings()
    bindings.kernel32.TerminateJobObject(job_handle, 5)
    if process.poll() is None:
        bindings.kernel32.TerminateProcess(int(process._handle), 5)  # type: ignore[attr-defined]
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        _taskkill_tree(process.pid)
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass


class SmtManagedProcess:
    """A Popen child bound to a kill-on-close Windows Job before execution."""

    def __init__(self, process: subprocess.Popen[Any], job_handle: int) -> None:
        self._process = process
        self._job_handle: int | None = job_handle
        self._popen_handles_closed = False

    @property
    def pid(self) -> int:
        return self._process.pid

    @property
    def returncode(self) -> int | None:
        return self._process.returncode

    @property
    def stdin(self) -> IO[Any] | None:
        return self._process.stdin

    @property
    def stdout(self) -> IO[Any] | None:
        return self._process.stdout

    @property
    def stderr(self) -> IO[Any] | None:
        return self._process.stderr

    def poll(self) -> int | None:
        result = self._process.poll()
        if result is not None:
            self._close_job()
        return result

    def wait(self, timeout: float | None = None) -> int:
        try:
            result = self._process.wait(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            self.terminate_tree()
            raise ManagedProcessTimeoutError(
                f"managed process {self.pid} exceeded {timeout} seconds"
            ) from exc
        self._close_job()
        return result

    def communicate(
        self,
        input: str | bytes | None = None,
        *,
        timeout_seconds: float | None = None,
    ) -> tuple[Any, Any]:
        try:
            result = self._process.communicate(input=input, timeout=timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            self.terminate_tree()
            self._drain_after_termination()
            raise ManagedProcessTimeoutError(
                f"managed process {self.pid} exceeded {timeout_seconds} seconds"
            ) from exc
        except KeyboardInterrupt:
            self.interrupt_tree()
            raise
        self._close_job()
        return result

    def _drain_after_termination(self) -> None:
        try:
            self._process.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            _taskkill_tree(self.pid)
            try:
                self._process.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                pass

    def terminate_tree(self, exit_code: int = 124) -> None:
        if self._process.poll() is not None:
            self._close_job()
            return
        bindings = _win32_bindings()
        job = self._job_handle
        terminated = bool(job and bindings.kernel32.TerminateJobObject(job, exit_code))
        if not terminated and self._process.poll() is None:
            _taskkill_tree(self.pid)
        try:
            self._process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            _taskkill_tree(self.pid)
            try:
                self._process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
        self._close_job()

    def interrupt_tree(self, grace_seconds: float = 0.5) -> None:
        if self._process.poll() is not None:
            self._close_job()
            return
        bindings = _win32_bindings()
        bindings.kernel32.GenerateConsoleCtrlEvent(signal.CTRL_BREAK_EVENT, self.pid)
        try:
            self._process.wait(timeout=max(0.0, grace_seconds))
        except subprocess.TimeoutExpired:
            self.terminate_tree(exit_code=130)
        else:
            self._close_job()

    def _close_job(self) -> None:
        job = self._job_handle
        if job is None:
            return
        self._job_handle = None
        _win32_bindings().kernel32.CloseHandle(job)

    def _close_popen_handles(self) -> None:
        if self._popen_handles_closed:
            return
        self._popen_handles_closed = True
        seen_streams: set[int] = set()
        for stream in (self._process.stdin, self._process.stdout, self._process.stderr):
            if stream is None or id(stream) in seen_streams:
                continue
            seen_streams.add(id(stream))
            try:
                stream.close()
            except OSError:
                pass
        handle = getattr(self._process, "_handle", None)
        close = getattr(handle, "Close", None)
        if callable(close):
            close()

    def close(self, *, exit_code: int = 130) -> None:
        """Terminate any live tree and close every owned Job/Popen handle."""

        if self._popen_handles_closed:
            return
        try:
            if self._process.poll() is None:
                self.terminate_tree(exit_code=exit_code)
            else:
                self._close_job()
        finally:
            self._close_popen_handles()

    def __enter__(self) -> SmtManagedProcess:
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _traceback: Any) -> None:
        self.close(exit_code=130 if _exc_type is KeyboardInterrupt else 5)


def start_managed_process(
    args: Sequence[str | os.PathLike[str]],
    *,
    cwd: Path | str | None = None,
    env: Mapping[str, str] | None = None,
    stdin: int | IO[Any] | None = None,
    stdout: int | IO[Any] | None = None,
    stderr: int | IO[Any] | None = None,
    text: bool = False,
    encoding: str | None = None,
) -> SmtManagedProcess:
    """Start a suspended Popen child, bind its Job, then resume execution."""

    _require_windows()
    job_handle = _create_kill_on_close_job()
    process: subprocess.Popen[Any] | None = None
    try:
        process = subprocess.Popen(
            list(args),
            cwd=str(cwd) if cwd is not None else None,
            env=dict(env) if env is not None else None,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            text=text,
            encoding=encoding,
            creationflags=_CREATE_SUSPENDED | _CREATE_NEW_PROCESS_GROUP,
        )
        try:
            _assign_process_to_job(job_handle, int(process._handle))  # type: ignore[attr-defined]
            _resume_primary_thread(process.pid)
        except BaseException as exc:
            _terminate_unresumed_process(process, job_handle)
            raise ManagedProcessEnvironmentError(
                f"could not assign and resume managed process: {exc}"
            ) from exc
        return SmtManagedProcess(process, job_handle)
    except BaseException:
        _win32_bindings().kernel32.CloseHandle(job_handle)
        raise


@dataclass(frozen=True)
class ProcessResult:
    """Bounded in-memory result from a fully logged managed process."""

    exit_code: int
    output_tail: tuple[str, ...]
    timed_out: bool = False
    interrupted: bool = False


class ManagedProcess:
    """Run a command with Job supervision and incremental bounded logging."""

    def run(
        self,
        argv: Sequence[str | os.PathLike[str]],
        cwd: Path,
        env: Mapping[str, str],
        timeout_seconds: int | float,
        log_path: Path,
        output_encoding: str | None = None,
    ) -> ProcessResult:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        selected_encoding = (
            _windows_system_text_encoding()
            if output_encoding is None
            else output_encoding
        )
        try:
            codec = codecs.lookup(selected_encoding)
        except LookupError as exc:
            raise ValueError(
                f"unknown process output encoding: {selected_encoding}"
            ) from exc
        if codec.incrementaldecoder is None:
            raise ValueError(
                f"process output encoding has no incremental decoder: {selected_encoding}"
            )
        log_path.parent.mkdir(parents=True, exist_ok=True)
        output_tail: deque[str] = deque(maxlen=200)
        reader_errors: list[BaseException] = []
        with log_path.open("a", encoding="utf-8", newline="") as log_file:
            with start_managed_process(
                argv,
                cwd=cwd,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=False,
            ) as process:
                reader: threading.Thread | None = None
                reader_started = False
                timed_out = False
                interrupted = False
                try:
                    if process.stdout is None:
                        raise ManagedProcessEnvironmentError(
                            "managed process did not expose its redirected output pipe"
                        )
                    read_available_chunk = getattr(process.stdout, "read1", None)
                    if not callable(read_available_chunk):
                        raise ManagedProcessEnvironmentError(
                            "managed process output pipe has no incremental read1 support"
                        )

                    def copy_output() -> None:
                        decoder = codec.incrementaldecoder(errors="replace")
                        pending_line = ""

                        def accept_decoded_text(text: str) -> None:
                            nonlocal pending_line
                            if not text:
                                return
                            log_file.write(text)
                            log_file.flush()
                            parts = (pending_line + text).split("\n")
                            for complete_line in parts[:-1]:
                                output_tail.append(complete_line.rstrip("\r"))
                            pending_line = parts[-1]

                        try:
                            while True:
                                raw_chunk = read_available_chunk(
                                    _OUTPUT_READ_CHUNK_SIZE
                                )
                                if not raw_chunk:
                                    break
                                if not isinstance(raw_chunk, bytes):
                                    raise TypeError(
                                        "managed process output pipe must be binary"
                                    )
                                accept_decoded_text(
                                    decoder.decode(raw_chunk, final=False)
                                )
                            accept_decoded_text(decoder.decode(b"", final=True))
                            if pending_line:
                                output_tail.append(pending_line.rstrip("\r"))
                        except BaseException as exc:
                            reader_errors.append(exc)
                            process.terminate_tree(exit_code=5)

                    reader = threading.Thread(
                        target=copy_output,
                        name=f"smt-process-output-{process.pid}",
                        daemon=True,
                    )
                    reader.start()
                    reader_started = True
                    try:
                        exit_code = process.wait(timeout=float(timeout_seconds))
                    except ManagedProcessTimeoutError:
                        exit_code = 124
                        timed_out = True
                except KeyboardInterrupt:
                    process.interrupt_tree()
                    exit_code = 130
                    interrupted = True
                except BaseException:
                    process.terminate_tree(exit_code=5)
                    raise
                finally:
                    if process.poll() is None:
                        process.terminate_tree(exit_code=5)
                    if reader is not None and reader_started:
                        reader.join(timeout=5)

        if reader is not None and reader.is_alive():
            raise ManagedProcessEnvironmentError(
                f"managed process {process.pid} output reader did not terminate"
            )
        if reader_errors:
            raise ManagedProcessEnvironmentError(
                f"managed process output logging failed: {reader_errors[0]}"
            ) from reader_errors[0]
        return ProcessResult(
            exit_code=exit_code,
            output_tail=tuple(output_tail),
            timed_out=timed_out,
            interrupted=interrupted,
        )


def _windows_system_text_encoding() -> str:
    """Return the fixed system text codec used when no codec is explicit."""

    return (
        locale.getencoding()
        if hasattr(locale, "getencoding")
        else locale.getpreferredencoding(False)
    )


def is_process_running(process_id: int) -> bool:
    """Return whether a process is still active, without guessing from PID files."""

    _require_windows()
    bindings = _win32_bindings()
    process_query_limited_information = 0x1000
    handle = bindings.kernel32.OpenProcess(
        process_query_limited_information, False, process_id
    )
    if not handle:
        return False
    try:
        exit_code = wintypes.DWORD()
        if not bindings.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return False
        return exit_code.value == _STILL_ACTIVE
    finally:
        bindings.kernel32.CloseHandle(handle)
