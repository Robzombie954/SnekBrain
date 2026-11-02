"""CI helper: start the installed SnekBrain, then take real Microsoft Edge screenshots of each view.

    python packaging/ci_screenshots.py --python C:\\...\\Python\\python.exe --launcher C:\\...\\SnekBrain.launch.pyw --out shots

Standard library only. Uses SNEKBRAIN_HOME like the smoke test, so a trained brain from it is shown.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path

VIEWS = ["live", "compare", "inside", "highlights", "stream"]


def data_dir() -> Path:
    if os.environ.get("SNEKBRAIN_HOME"):
        return Path(os.environ["SNEKBRAIN_HOME"])
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "SnekBrain"


def api(info, method, path, body=None):
    req = urllib.request.Request(f"http://127.0.0.1:{info['port']}/api/{path}", method=method,
                                 data=None if method == "GET" else json.dumps(body or {}).encode(),
                                 headers={"X-SL-Token": info["token"], "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read() or b"{}")


def edge() -> str:
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles")):
        if base and Path(base, "Microsoft", "Edge", "Application", "msedge.exe").exists():
            return str(Path(base, "Microsoft", "Edge", "Application", "msedge.exe"))
    return shutil.which("msedge") or "msedge"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--python", required=True)
    ap.add_argument("--launcher", required=True)
    ap.add_argument("--out", default="shots")
    ap.add_argument("--train-secs", type=int, default=60)
    a = ap.parse_args()
    out = Path(a.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
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
    api(info, "POST", "start")  # so the 'inside' tab has a real training game to show
    time.sleep(a.train_secs)
    prof = out / "_edge_profile"
    for v in VIEWS:
        png = out / f"{v}.png"
        subprocess.run([edge(), "--headless=new", "--disable-gpu", "--hide-scrollbars", "--no-first-run",
                        f"--user-data-dir={prof}", "--window-size=1360,880", "--virtual-time-budget=12000",
                        f"--screenshot={png}", f"http://127.0.0.1:{info['port']}/#{v}"], check=False, timeout=180)
        print(v, "ok" if png.exists() else "missing", flush=True)
    api(info, "POST", "quit", {"resume_later": False})
    shutil.rmtree(prof, ignore_errors=True)


if __name__ == "__main__":
    main()
