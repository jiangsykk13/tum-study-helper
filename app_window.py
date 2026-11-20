"""Run the local web UI from a desktop shortcut: no console, the page opens as a Chrome tab.

Started by the desktop shortcut through pythonw.exe (``tum_moodle_auto.py ui --app``).
The page opens as an ordinary tab in the user's own Chrome, next to their other tabs
(user's choice, 2026-10-23); without Chrome, in the default browser. A second start
does not run another server; it asks the running one to open another tab. Messages
use Windows message boxes because there is no console. Only standard library
and ctypes; pywin32 is used only to write the shortcut files.
"""

from __future__ import annotations

import os
import subprocess
import sys
import webbrowser
from pathlib import Path

APP_NAME = "TUM 学习小助理"
DATA_DIR = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "TUM-Study-Helper"
LOG_FILE = DATA_DIR / "ui.log"
ICON_FILE = Path(__file__).resolve().parent / "web" / "app.ico"
MUTEX_NAME = "Local\\TUM-Study-Helper-UI"
SHOW_EVENT_NAME = "Local\\TUM-Study-Helper-Show"

_CHROME = (
    r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
    r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
    r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe",
)


def find_chrome() -> str | None:
    for candidate in _CHROME:
        path = os.path.expandvars(candidate)
        if "%" not in path and os.path.isfile(path):
            return path
    return None


def browser_args(chrome: str, url: str) -> list[str]:
    # The user's normal Chrome and profile: no --app, no separate --user-data-dir.
    return [chrome, url]


def open_window(url: str) -> bool:
    """Open url as a new tab in Chrome, else in the default browser. True when Chrome was used."""
    chrome = find_chrome()
    if chrome is None:
        webbrowser.open_new_tab(url)
        return False
    # A Chrome started here keeps running after the helper quits (checked 2026-10-23:
    # pythonw is not in a job object, so its children are not killed with it).
    subprocess.Popen(browser_args(chrome, url), close_fds=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return True


def quiet_output():
    """pythonw has no console (sys.stdout is None): send output to a log file instead.

    The log holds only startup notes and server warnings; app mode never prints the URL token.
    """
    if sys.stdout is not None and sys.stderr is not None:
        return
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    log = open(LOG_FILE, "w", encoding="utf-8", buffering=1)
    sys.stdout = sys.stdout or log
    sys.stderr = sys.stderr or log


def message(text: str, error: bool = False):
    if os.name != "nt":
        print(text, file=sys.stderr)
        return
    import ctypes
    flags = 0x10 if error else 0x40  # MB_ICONERROR / MB_ICONINFORMATION
    ctypes.windll.user32.MessageBoxW(None, text, APP_NAME, flags | 0x10000)  # MB_SETFOREGROUND


class SingleInstance:
    """A named mutex marks the running copy; a named event asks it for another window."""

    def __init__(self):
        self.running_elsewhere = False
        self._mutex = self._event = None
        if os.name != "nt":
            return
        import ctypes
        from ctypes import wintypes
        self._kernel = kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateMutexW.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
        kernel.CreateMutexW.restype = wintypes.HANDLE
        kernel.CreateEventW.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR)
        kernel.CreateEventW.restype = wintypes.HANDLE
        kernel.SetEvent.argtypes = (wintypes.HANDLE,)
        kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        self._mutex = kernel.CreateMutexW(None, False, MUTEX_NAME)
        self.running_elsewhere = ctypes.get_last_error() == 183  # ERROR_ALREADY_EXISTS
        # Auto-reset event: each SetEvent wakes the running copy once.
        self._event = kernel.CreateEventW(None, False, False, SHOW_EVENT_NAME)

    def ask_to_show(self):
        if self._event:
            self._kernel.SetEvent(self._event)

    def wait_for_show(self, timeout_ms: int) -> bool:
        """True when another start asked for a window within timeout_ms."""
        if not self._event:
            return False
        return self._kernel.WaitForSingleObject(self._event, timeout_ms) == 0

    def close(self):
        for handle in (self._event, self._mutex):
            if handle:
                self._kernel.CloseHandle(handle)
        self._mutex = self._event = None


def install_shortcuts(python_exe: str, script: str, *, resident: bool = False,
                      tailscale: bool = False) -> list[str]:
    """Create desktop and Start menu shortcuts that start the app without a console.

    resident: the app keeps running without an open page (--stay), and a Startup entry
    starts it at sign-in without a tab, so other devices on the tailnet can reach it.
    tailscale: accept requests forwarded by `tailscale serve` from the user's own login.
    """
    from win32com.client import Dispatch

    pythonw = Path(python_exe).with_name("pythonw.exe")
    if not pythonw.is_file():
        raise FileNotFoundError(f"没有找到 {pythonw}")
    shell = Dispatch("WScript.Shell")
    made = []
    command = "ui --app" + (" --stay" if resident else "") + (" --tailscale" if tailscale else "")
    places = [(shell.SpecialFolders("Desktop"), ""), (shell.SpecialFolders("Programs"), "")]
    if resident:
        places.append((shell.SpecialFolders("Startup"), " --no-browser"))
    for folder, extra in places:
        link = Path(folder) / f"{APP_NAME}.lnk"
        shortcut = shell.CreateShortcut(str(link))
        shortcut.TargetPath = str(pythonw)
        shortcut.Arguments = f'-I -B "{script}" {command}{extra}'
        shortcut.WorkingDirectory = str(Path(script).parent)
        shortcut.Description = "打开 TUM 学习小助理（今日计划、课程、截止日期、同步）"
        if ICON_FILE.is_file():
            shortcut.IconLocation = f"{ICON_FILE},0"
        shortcut.Save()
        made.append(str(link))
    return made
