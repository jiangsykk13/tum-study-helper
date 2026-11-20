"""Small cross-process directory mutexes without third-party dependencies.

Windows mutexes never create a file and are released by the OS after a crash.
The POSIX fallback keeps its flock file in the system temporary directory, so
locking a missing destination (including a preview) does not create it.
"""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import tempfile


class DirectoryBusyError(RuntimeError):
    """The directory is already in use by another process."""


@contextmanager
def directory_lock(root, *, wait=False):
    """Hold one mutex per normalized absolute directory; fail fast by default."""
    path = os.path.normcase(str(Path(root).expanduser().resolve(strict=False)))
    digest = hashlib.sha256(path.encode("utf-8", errors="surrogatepass")).hexdigest()
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateMutexW.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
        kernel.CreateMutexW.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.ReleaseMutex.argtypes = (wintypes.HANDLE,)
        kernel.ReleaseMutex.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel.CloseHandle.restype = wintypes.BOOL
        handle = kernel.CreateMutexW(None, False, "Local\\TUM-study-" + digest)
        if not handle:
            raise RuntimeError("无法取得目录同步锁，未开始写入。")
        acquired = False
        try:
            result = kernel.WaitForSingleObject(handle, 0xFFFFFFFF if wait else 0)
            if result == 0x102:
                raise DirectoryBusyError("此目录正在由另一进程处理，请稍后重试。")
            if result not in (0, 0x80):  # WAIT_OBJECT_0, WAIT_ABANDONED
                raise RuntimeError("无法取得目录同步锁，未开始写入。")
            acquired = True
            yield
        finally:
            if acquired:
                kernel.ReleaseMutex(handle)
            kernel.CloseHandle(handle)
    else:
        import fcntl
        lock_path = Path(tempfile.gettempdir()) / ("tum-study-" + digest + ".lock")
        with lock_path.open("a+b") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
            except BlockingIOError:
                raise DirectoryBusyError("此目录正在由另一进程处理，请稍后重试。") from None
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
