"""Small Windows helpers: CPU limit, priority, pause/resume, video detection, start with Windows, Recycle Bin.

Everything here is plain Windows API through ctypes (no admin rights needed) and does
nothing harmful on other systems, so the app and its tests also run on Linux/macOS.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

WIN = sys.platform == "win32"

if WIN:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32 = ctypes.WinDLL("user32", use_last_error=True)


# ------------------------------------------------------------------ CPU limit (Job Object)
class CpuLimiter:
    """Puts the training process (and every game process it starts) under one hard CPU limit.

    Uses a Windows Job Object with a hard CPU-rate cap: the whole group can never use more than
    `percent` of the machine's total CPU time, whatever it does. Closing the app closes the job.
    """

    def __init__(self):
        self.job = None
        if WIN:
            self.job = kernel32.CreateJobObjectW(None, None)

    def assign(self, pid: int) -> bool:
        if not self.job:
            return False
        PROCESS_SET_QUOTA, PROCESS_TERMINATE = 0x0100, 0x0001
        h = kernel32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, pid)
        if not h:
            return False
        try:
            return bool(kernel32.AssignProcessToJobObject(self.job, h))
        finally:
            kernel32.CloseHandle(h)

    def set_percent(self, percent: int) -> bool:
        if not self.job:
            return False

        class CPU_RATE(ctypes.Structure):
            _fields_ = [("ControlFlags", wintypes.DWORD), ("CpuRate", wintypes.DWORD)]

        ENABLE, HARD_CAP = 0x1, 0x4
        info = CPU_RATE(ENABLE | HARD_CAP, max(1, min(100, int(percent))) * 100)  # in 1/100 of a percent
        JobObjectCpuRateControlInformation = 15
        return bool(kernel32.SetInformationJobObject(self.job, JobObjectCpuRateControlInformation,
                                                     ctypes.byref(info), ctypes.sizeof(info)))


def creation_flags(low_priority: bool) -> int:
    """Flags for starting the trainer: no console window, and below-normal priority if asked."""
    if not WIN:
        return 0
    CREATE_NO_WINDOW, BELOW_NORMAL = 0x08000000, 0x00004000
    return CREATE_NO_WINDOW | (BELOW_NORMAL if low_priority else 0)


def set_tree_priority(pid: int, level: str):
    """level: idle, below, normal. For the trainer and all its game processes."""
    try:
        import psutil
        procs = [psutil.Process(pid)]
        procs += procs[0].children(recursive=True)
        if WIN:
            val = {"idle": psutil.IDLE_PRIORITY_CLASS, "below": psutil.BELOW_NORMAL_PRIORITY_CLASS,
                   "normal": psutil.NORMAL_PRIORITY_CLASS}[level]
        else:
            val = {"idle": 19, "below": 10, "normal": 0}[level]
        for p in procs:
            try:
                p.nice(val)
            except Exception:
                pass
    except Exception:
        pass


def suspend_tree(pid: int, suspend: bool):
    """Freeze or unfreeze training instantly (nothing is lost, it carries on exactly where it was)."""
    try:
        import psutil
        root = psutil.Process(pid)
        procs = root.children(recursive=True) + [root]
        for p in procs:
            try:
                p.suspend() if suspend else p.resume()
            except Exception:
                pass
    except Exception:
        pass


# ------------------------------------------------------------------ is a video playing?
def _fullscreen_app() -> str | None:
    """Name of the program whose window fills the whole screen in front, if any (not the desktop)."""
    if not WIN:
        return None
    try:
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return None
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls, 256)
        if cls.value in ("Progman", "WorkerW", "Shell_TrayWnd"):
            return None
        r = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(r))

        class MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                        ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]

        mon = user32.MonitorFromWindow(hwnd, 2)
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(mi)
        if not user32.GetMonitorInfoW(mon, ctypes.byref(mi)):
            return None
        m = mi.rcMonitor
        if not (r.left <= m.left and r.top <= m.top and r.right >= m.right and r.bottom >= m.bottom):
            return None
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        try:
            import psutil
            return psutil.Process(pid.value).name()
        except Exception:
            return "full-screen app"
    except Exception:
        return None


def _media_sessions() -> list[tuple[str, bool]]:
    """(app id, is playing) for each Windows media session (what the volume flyout shows)."""
    if not WIN:
        return []
    try:
        import asyncio
        from winrt.windows.media.control import \
            GlobalSystemMediaTransportControlsSessionManager as Manager
    except Exception:
        return []

    async def read():
        mgr = await Manager.request_async()
        out = []
        for s in mgr.get_sessions():
            try:
                st = s.get_playback_info().playback_status
                out.append((s.source_app_user_model_id or "", int(st) == 4))  # 4 = Playing
            except Exception:
                continue
        return out

    try:
        return asyncio.run(read())
    except Exception:
        return []


def video_playing(video_apps: str, fullscreen_counts: bool) -> str | None:
    """Returns a short description of the video that is playing, or None."""
    names = [n.strip().lower() for n in (video_apps or "").split(",") if n.strip()]
    for app_id, playing in _media_sessions():
        low = app_id.lower()
        if playing and any(n in low for n in names):
            return app_id.split("!")[-1].split("\\")[-1] or "a video"
    if fullscreen_counts:
        fs = _fullscreen_app()
        if fs and not fs.lower().startswith(("snekbrain", "pythonw", "python")):
            return fs
    return None


# ------------------------------------------------------------------ start with Windows
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "SnekBrain"


def set_autostart(enabled: bool, command: str) -> bool:
    """Per-user sign-in start (HKEY_CURRENT_USER, no admin). The installer's uninstaller removes it."""
    if not WIN:
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            if enabled:
                winreg.SetValueEx(k, RUN_NAME, 0, winreg.REG_SZ, command)
            else:
                try:
                    winreg.DeleteValue(k, RUN_NAME)
                except FileNotFoundError:
                    pass
        return True
    except OSError:
        return False


def get_autostart() -> bool:
    if not WIN:
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, RUN_NAME)
            return True
    except OSError:
        return False


# ------------------------------------------------------------------ files
def recycle(path: Path) -> bool:
    """Move a folder to the Recycle Bin. False if that isn't possible here."""
    if not WIN:
        return False

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT), ("pFrom", wintypes.LPCWSTR),
                    ("pTo", wintypes.LPCWSTR), ("fFlags", ctypes.c_uint16), ("fAnyOperationsAborted", wintypes.BOOL),
                    ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wintypes.LPCWSTR)]

    FO_DELETE, FOF_ALLOWUNDO, FOF_NOCONFIRMATION, FOF_SILENT, FOF_NOERRORUI = 3, 0x40, 0x10, 0x4, 0x400
    op = SHFILEOPSTRUCTW(None, FO_DELETE, str(Path(path).resolve()) + "\0", None,
                         FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI, False, None, None)
    shell32 = ctypes.WinDLL("shell32")
    return shell32.SHFileOperationW(ctypes.byref(op)) == 0 and not Path(path).exists()


def open_folder(path: Path):
    try:
        if WIN:
            os.startfile(str(path))  # noqa: S606 - opens Explorer on our own data folder
        elif sys.platform == "darwin":
            import subprocess
            subprocess.Popen(["open", str(path)])
        else:
            import subprocess
            subprocess.Popen(["xdg-open", str(path)])
    except Exception:
        pass
