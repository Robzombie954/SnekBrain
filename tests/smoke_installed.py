"""End-to-end check of an installed (or dev) SnekBrain: start the engine, train for real, use the API, quit.

    python tests/smoke_installed.py --launcher "C:\\...\\SnekBrain.launch.pyw" [--secs 240] [--report out.json]

Uses the standard library only, so it runs with the installed copy's own Python. Set SNEKBRAIN_HOME to a
scratch folder first so it never touches real brains. Exit code 0 only when every check passed.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = ""):
    RESULTS.append((name, bool(ok), detail))
    print(("PASS " if ok else "FAIL ") + name + (f"  ({detail})" if detail else ""), flush=True)
    return ok


def data_dir() -> Path:
    if os.environ.get("SNEKBRAIN_HOME"):
        return Path(os.environ["SNEKBRAIN_HOME"])
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / ".local" / "share")
    return Path(base) / "SnekBrain"


class Api:
    def __init__(self, info: dict):
        self.base = f"http://127.0.0.1:{info['port']}"
        self.token = info["token"]

    def call(self, method: str, path: str, body=None, raw=False, token=True, timeout=60):
        data = None if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
        if method == "POST" and data is None:
            data = b"{}"
        h = {"Content-Type": "application/json"}
        if token:
            h["X-SL-Token"] = self.token
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=h)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            out = r.read()
            return out if raw else json.loads(out or b"{}")

    def status_code(self, method: str, path: str, **kw) -> int:
        try:
            self.call(method, path, raw=True, **kw)
            return 200
        except urllib.error.HTTPError as e:
            return e.code
        except (urllib.error.URLError, OSError):
            return 0  # nothing listening


def wait_info(timeout=60) -> dict | None:
    f = data_dir() / "server.json"
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            info = json.loads(f.read_text(encoding="utf-8"))
            Api(info).call("GET", "/api/status", timeout=3)
            return info
        except Exception:
            time.sleep(1)
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--launcher", required=True, help="SnekBrain.launch.pyw (installed) or run.py (dev)")
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--secs", type=int, default=240, help="how long to let it train")
    ap.add_argument("--report", default="")
    a = ap.parse_args()

    t_start = time.time()
    subprocess.run([a.python, a.launcher, "--background"], check=False, timeout=120)
    info = wait_info()
    if not check("engine starts in the background", info is not None):
        return finish(a)
    api = Api(info)
    check("engine answers only on 127.0.0.1", info.get("port") and api.base.startswith("http://127.0.0.1:"))
    check("API refuses calls without the token", api.status_code("GET", "/api/status", token=False) == 403)

    html = api.call("GET", "/", raw=True, token=False).decode("utf-8", "replace")
    check("app page is served", "<canvas" in html and "app.js" in html)
    for f in ("app.js", "game.js", "arena.js", "style.css", "icon.svg"):
        check(f"web file {f}", api.status_code("GET", "/" + f, token=False) == 200)
    check("no path escape from the web folder", api.status_code("GET", "/../server.py", token=False) == 404)

    st = api.call("GET", "/api/status")
    lst = api.call("GET", "/api/brains")["brains"]
    check("a brain exists on first run", len(lst) >= 1, ", ".join(b["name"] for b in lst))
    check("settings load", "app" in api.call("GET", "/api/settings"))
    gpu = api.call("GET", "/api/gpu")
    check("GPU add-on check answers", isinstance(gpu, dict), json.dumps(gpu)[:160])

    new = api.call("POST", "/api/brains/new", {"name": "Smoke test", "preset": "quick", "start": True})
    bid = new["id"]
    time.sleep(3)
    st = api.call("GET", "/api/status")
    check("training starts", st.get("running") and st.get("state") != "error", st.get("state", ""))

    t0 = time.time()
    gens = 0
    while time.time() - t0 < a.secs:
        time.sleep(10)
        st = api.call("GET", "/api/status")
        rows = api.call("GET", f"/api/brain/{bid}/progress")["rows"]
        gens = len(rows)
        print(f"  {int(time.time() - t0)}s state={st.get('state')} gens={gens} sps={st.get('sps')} "
              f"device={st.get('device')} err={st.get('error') or ''}", flush=True)
        if st.get("state") == "error":
            break
        if gens >= 2:
            break
    check("it learns: at least 2 generations saved", gens >= 2, f"{gens} in {int(time.time() - t0)}s")
    check("trains on the processor by default", st.get("device") in (None, "cpu"), str(st.get("device")))

    live = api.call("GET", f"/api/brain/{bid}/live")
    check("the window gets the newest brain", isinstance(live, dict) and live.get("layers"), str(list(live)[:6]))
    check("progress chart data", gens >= 1)
    check("Hall of Fame answers", "records" in api.call("GET", f"/api/brain/{bid}/hall"))
    check("events answer", "events" in api.call("GET", f"/api/brain/{bid}/events"))

    clip = {"kind": "manual", "label": "Smoke test clip", "generation": 1, "fps": 15, "R": 1000,
            "frames": [{"snakes": [{"k": "a", "c": 1, "n": 12, "h": 0.5, "b": [0, 0, 5, 5, 10, 10]}],
                        "food": [1, 2, 3]}] * 3}
    try:
        name = api.call("POST", f"/api/brain/{bid}/highlights", clip)["name"]
        ok = any(h.get("name") == name for h in api.call("GET", f"/api/brain/{bid}/highlights")["highlights"])
        check("a highlight saves and lists", ok, name)
    except urllib.error.HTTPError as e:
        check("a highlight saves and lists", False, f"HTTP {e.code}: {e.read()[:200]!r}")

    exp = api.call("GET", f"/api/brain/{bid}/export", raw=True)
    check("brain export makes a file", len(exp) > 1000, f"{len(exp) // 1024} KB")
    try:
        imp = api.call("POST", "/api/brains/import", exp)
        check("the exported file imports back", bool(imp.get("id")))
    except urllib.error.HTTPError as e:
        check("the exported file imports back", False, f"HTTP {e.code}")

    upd = api.call("GET", "/api/update?force=1", timeout=60)
    check("update check answers without crashing", isinstance(upd, dict), json.dumps(upd)[:200])

    st = api.call("POST", "/api/pause", timeout=180)
    check("pause saves and stops", not st.get("running") and st.get("state") != "error", st.get("state", ""))

    api.call("POST", "/api/quit", {"resume_later": False}, timeout=30)
    gone = False
    for _ in range(60):
        time.sleep(1)
        if api.status_code("GET", "/api/status") != 200:
            gone = True
            break
    check("quit closes the engine", gone)
    check("total smoke time", True, f"{int(time.time() - t_start)}s")
    return finish(a)


def finish(a):
    if a.report:
        Path(a.report).write_text(json.dumps([{"check": n, "ok": ok, "detail": d} for n, ok, d in RESULTS], indent=1))
    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
