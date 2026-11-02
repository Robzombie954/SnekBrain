"""What the Start menu / desktop shortcut runs: makes sure the engine is running, then opens the window.

    app.py               open the window (starts the engine first if needed)
    app.py --background  start the engine only (used at Windows sign-in; resumes training if it was training)
    app.py --quit        stop the engine cleanly, saving everything (the installer uses this before updating
                         or uninstalling; training carries on after an update if it was training)
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

from . import FULL_NAME as APP_NAME, brains, winsys
from .server import bootstrap, server_file


def _ping() -> dict | None:
    info = None
    try:
        info = json.loads(server_file().read_text(encoding="utf-8"))
        req = urllib.request.Request(f"http://127.0.0.1:{info['port']}/api/status",
                                     headers={"X-SL-Token": info["token"], "Host": f"127.0.0.1:{info['port']}"})
        with urllib.request.urlopen(req, timeout=2) as r:
            if r.status == 200:
                return info
    except Exception:
        return None
    return None


def ensure_engine(timeout: float = 30.0) -> dict | None:
    info = _ping()
    if info:
        return info
    flags = 0
    if winsys.WIN:
        DETACHED, NEW_GROUP, NO_WINDOW = 0x00000008, 0x00000200, 0x08000000
        flags = DETACHED | NEW_GROUP | NO_WINDOW
    kw = {"start_new_session": True} if not winsys.WIN else {}
    subprocess.Popen(bootstrap("snekbrain.server", []), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, creationflags=flags, close_fds=True, cwd=str(brains.data_dir()), **kw)
    t0 = time.time()
    while time.time() - t0 < timeout:
        time.sleep(0.4)
        info = _ping()
        if info:
            return info
    return None


def _edge() -> str | None:
    if not winsys.WIN:
        return None
    cands = []
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"), os.environ.get("LOCALAPPDATA")):
        if base:
            cands.append(Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe")
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe") as k:
            cands.insert(0, Path(winreg.QueryValue(k, None)))
    except OSError:
        pass
    for c in cands:
        if c.exists():
            return str(c)
    return shutil.which("msedge")


def open_window(url: str):
    """Its own app window (Microsoft Edge's app mode, which every Windows 10/11 PC has), else the default browser."""
    edge = _edge()
    if edge:
        prof = brains.data_dir() / "window"
        try:
            subprocess.Popen([edge, f"--app={url}", f"--user-data-dir={prof}", "--no-first-run",
                              "--no-default-browser-check", "--disable-sync", "--window-size=1360,880"],
                             close_fds=True)
            return
        except OSError:
            pass
    webbrowser.open(url)


def _message(text: str):
    if winsys.WIN:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, text, APP_NAME, 0x10)
    else:
        print(text, file=sys.stderr)


def quit_running(timeout: float = 120.0) -> bool:
    """Ask a running engine to save and stop. True when nothing is running any more."""
    info = _ping()
    if not info:
        return True
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{info['port']}/api/quit", data=b'{"resume_later": true}', method="POST",
                                     headers={"X-SL-Token": info["token"], "Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=5).read()
    except Exception:
        pass
    t0 = time.time()
    while time.time() - t0 < timeout:
        time.sleep(0.5)
        if not _ping():
            pid = int(info.get("pid", 0))
            try:
                import psutil
                if pid and psutil.pid_exists(pid):
                    psutil.Process(pid).wait(timeout=max(1, timeout - (time.time() - t0)))
            except Exception:
                pass
            return True
    return False


def main():
    if "--quit" in sys.argv:
        sys.exit(0 if quit_running() else 1)
    background = "--background" in sys.argv
    info = ensure_engine()
    if not info:
        _message(f"{APP_NAME} couldn't start its engine. The log is in:\n{brains.data_dir() / 'app.log'}")
        sys.exit(1)
    if not background:
        open_window(f"http://127.0.0.1:{info['port']}/")


if __name__ == "__main__":
    main()
