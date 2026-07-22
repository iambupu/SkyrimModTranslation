"""Delayed Windows platform primitives for the public SMT controller.

This module intentionally performs no Win32 loading or output at import time so
repository checks can import and compile it on non-Windows hosts.
"""

from __future__ import annotations

import codecs
import ctypes
import json
import locale
import os
import signal
import subprocess
import threading
import time
from collections import deque
from ctypes import wintypes
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import IO, Any, Literal, Mapping, Sequence


_IS_WINDOWS = os.name == "nt"

_ERROR_LOCK_VIOLATION = 33
_ERROR_IO_PENDING = 997
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

_GENERIC_READ = 0x80000000
_GENERIC_WRITE = 0x40000000
_FILE_SHARE_READ = 0x00000001
_FILE_SHARE_WRITE = 0x00000002
_OPEN_ALWAYS = 4
_FILE_ATTRIBUTE_NORMAL = 0x00000080
_LOCKFILE_FAIL_IMMEDIATELY = 0x00000001
_LOCKFILE_EXCLUSIVE_LOCK = 0x00000002

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
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
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
    ) -> None:
        if timeout_seconds < 0:
            raise ValueError("timeout_seconds must be non-negative")
        if mode not in {"shared", "exclusive"}:
            raise ValueError("lock mode must be 'shared' or 'exclusive'")
        self.path = Path(path)
        self.mode = mode
        self.exclusive = mode == "exclusive"
        self.timeout_seconds = timeout_seconds
        self.command = command
        self.poll_interval_seconds = poll_interval_seconds
        self._handle: int | None = None
        self._overlapped: _OVERLAPPED | None = None

    def acquire(self) -> SmtProcessFileLock:
        if self._handle is not None:
            raise RuntimeError("lock is already acquired")
        bindings = _win32_bindings()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = bindings.kernel32.CreateFileW(
            str(self.path),
            _GENERIC_READ | _GENERIC_WRITE,
            _FILE_SHARE_READ | _FILE_SHARE_WRITE,
            None,
            _OPEN_ALWAYS,
            _FILE_ATTRIBUTE_NORMAL,
            None,
        )
        if handle == _INVALID_HANDLE_VALUE:
            raise ManagedProcessEnvironmentError(str(_last_winerror("CreateFileW failed")))

        flags = _LOCKFILE_FAIL_IMMEDIATELY
        if self.exclusive:
            flags |= _LOCKFILE_EXCLUSIVE_LOCK
        overlapped = _OVERLAPPED()
        deadline = time.monotonic() + self.timeout_seconds
        try:
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

    def __enter__(self) -> SmtProcessFileLock:
        return self.acquire()

    def __exit__(self, _exc_type: Any, _exc: Any, _traceback: Any) -> None:
        self.release()


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
        encoding: str | None = None,
    ) -> ProcessResult:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if encoding is not None:
            try:
                codecs.lookup(encoding)
            except LookupError as exc:
                raise ValueError(f"unknown process output encoding: {encoding}") from exc
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

                    def copy_output() -> None:
                        try:
                            for raw_line in process.stdout:
                                line = _decode_process_output(raw_line, encoding=encoding)
                                log_file.write(line)
                                log_file.flush()
                                output_tail.append(line.rstrip("\r\n"))
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


def _decode_process_output(raw_line: bytes, *, encoding: str | None) -> str:
    """Decode one binary output line without letting diagnostics kill workflow."""

    if encoding is not None:
        return raw_line.decode(encoding, errors="replace")

    candidates = ["utf-8"]
    windows_encoding = (
        locale.getencoding()
        if hasattr(locale, "getencoding")
        else locale.getpreferredencoding(False)
    )
    if windows_encoding.casefold() != "utf-8":
        candidates.append(windows_encoding)
    for candidate in candidates:
        try:
            return raw_line.decode(candidate, errors="strict")
        except UnicodeDecodeError:
            continue
    return raw_line.decode(candidates[-1], errors="replace")


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
