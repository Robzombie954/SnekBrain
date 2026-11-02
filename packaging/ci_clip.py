"""CI helper: record a short demo clip of the installed SnekBrain in a real Microsoft Edge app window.

    python packaging/ci_clip.py --python ...\\pythonw.exe --launcher ...\\SnekBrain.launch.pyw --ffmpeg ffmpeg.exe --out demo.mp4

Standard library only. Uses SNEKBRAIN_HOME like the smoke test and shows the most trained brain.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

# the app's embedded Python (a ._pth file) doesn't put the script's folder on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ci_screenshots import api, data_dir, edge  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--python", required=True)
    ap.add_argument("--launcher", required=True)
    ap.add_argument("--ffmpeg", default="ffmpeg")
    ap.add_argument("--out", default="demo.mp4")
    ap.add_argument("--secs", type=int, default=20)
    ap.add_argument("--view", default="live")
    a = ap.parse_args()
    out = Path(a.out).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([a.python, a.launcher, "--background"], check=False, timeout=120)
    info = None
    for _ in range(60):
        try:
            info = json.loads((data_dir() / "server.json").read_text(encoding="utf-8"))
            api(info, "GET", "status")
            break
        except Exception:
            info = None
            time.sleep(1)
    if not info:
        raise SystemExit("engine did not start")
    api(info, "POST", "settings", {"app": {"intro_seen": True}})
    trained = sorted((b for b in api(info, "GET", "brains")["brains"] if b.get("generation")), key=lambda b: -b["generation"])
    if trained:
        api(info, "POST", f"brain/{trained[0]['id']}/select")
    api(info, "POST", "start")
    prof = out.parent / "_edge_clip_profile"
    win = subprocess.Popen([edge(), f"--app=http://127.0.0.1:{info['port']}/#{a.view}", "--no-first-run",
                            "--no-default-browser-check", f"--user-data-dir={prof}", "--window-position=0,0",
                            "--window-size=1024,740", "--disable-features=msEdgeWelcomePage"])
    time.sleep(12)  # page load, brain download and a few seconds of play
    cmd = [a.ffmpeg, "-y", "-loglevel", "error", "-f", "gdigrab", "-framerate", "20", "-t", str(a.secs),
           "-i", "title=Terrelbytes SnekBrain", "-vf", "scale=960:-2", "-c:v", "libx264", "-preset", "veryfast",
           "-crf", "26", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out)]
    r = subprocess.run(cmd, check=False, timeout=a.secs + 120)
    if r.returncode != 0 or not out.exists():  # window title not found: record the screen instead
        cmd[cmd.index("title=Terrelbytes SnekBrain")] = "desktop"
        cmd[cmd.index("scale=960:-2")] = "crop=1024:740:0:0,scale=960:-2"  # just the window, not the taskbar
        subprocess.run(cmd, check=False, timeout=a.secs + 120)
    win.terminate()
    subprocess.run(["taskkill", "/F", "/T", "/PID", str(win.pid)], check=False, capture_output=True)
    api(info, "POST", "quit", {"resume_later": False})
    time.sleep(3)
    shutil.rmtree(prof, ignore_errors=True)
    print("clip", out.name, f"{out.stat().st_size / 1e6:.1f} MB" if out.exists() else "missing", flush=True)
    if not out.exists():
        raise SystemExit(1)


if __name__ == "__main__":
    main()
