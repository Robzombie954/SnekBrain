"""The app's engine room: a small web server on this PC only (127.0.0.1) plus the training controller.

The window (index.html) talks to it. Training itself runs in a separate process (trainer.py),
so the window can close and open again without stopping it.

Safety: it only listens on 127.0.0.1, every /api call needs the random token handed to the
window, and requests whose Host isn't this machine are refused (stops other websites from
poking it). Nothing is ever sent anywhere: no telemetry, no internet use at all.
"""
from __future__ import annotations

import json
import mimetypes
import os
import secrets
import subprocess
import sys
import threading
import time
import traceback
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import __version__, brains, extras, winsys
from . import settings as S

PKG = Path(__file__).resolve().parent
WEB = PKG / "web"
PREFERRED_PORT = 47317


def python_for_children() -> str:
    """pythonw.exe on Windows (no console windows), else the current interpreter."""
    exe = Path(sys.executable)
    if winsys.WIN and exe.name.lower() == "python.exe" and (exe.parent / "pythonw.exe").exists():
        return str(exe.parent / "pythonw.exe")
    return str(exe)


def bootstrap(module: str, args: list[str]) -> list[str]:
    """Command that runs `module.main()` with our package importable, whatever launched us."""
    code = f"import sys; sys.path.insert(0, {str(PKG.parent)!r}); from {module} import main; main()"
    return [python_for_children(), "-c", code, *args]


class Controller:
    def __init__(self):
        self.data = brains.data_dir()
        self.settings_file = self.data / "settings.json"
        self.state_file = self.data / "state.json"
        self.app = S.load_app_settings(self.settings_file)
        self.state = S.read_json(self.state_file, {})
        self.lock = threading.RLock()
        self.proc: subprocess.Popen | None = None
        self.limiter = winsys.CpuLimiter()
        self.suspended_for: str | None = None
        self.slowed_for: str | None = None
        self.last_seen = time.time()   # last time a window asked for status
        self.closed_pause = False
        self.crashes: list[float] = []
        self.last_error = ""
        self.cpu = 0.0
        self.updater = extras.Updater(self.data)
        self.gpu = extras.Gpu(self.data)
        self.ai_key: str | None = None
        first_run = not brains.list_brains()
        brains.install_starters(PKG / "starter")  # a pre-trained "Starter" to show what it becomes
        self._ensure_default_brain(first_run)

    # ---------------------------------------------------------------- state
    def _save_state(self):
        S.write_json(self.state_file, self.state)

    def _ensure_default_brain(self, first_run: bool = False):
        """First run: make an untrained brain (the active one, so people watch it learn from nothing)."""
        if first_run or not brains.list_brains():
            b = brains.create("Newborn", "quick")
            self.state["active"] = b.name
            self._save_state()
        active = self.state.get("active")
        if not active or not (brains.brains_dir() / str(active)).exists():
            have = brains.list_brains()
            self.state["active"] = have[0]["id"]
            self._save_state()

    def active_dir(self) -> Path:
        return brains.brain_path(self.state["active"])

    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def workers(self) -> int:
        n = os.cpu_count() or 2
        if self.app["mode"] == "turbo":
            return self.app["workers_turbo"] or max(1, n - 1)
        return self.app["workers_normal"] or max(1, n - 3)

    def cap(self) -> int:
        return self.app["cpu_cap_turbo"] if self.app["mode"] == "turbo" else self.app["cpu_cap_normal"]

    # ---------------------------------------------------------------- training
    def start(self, brain_id: str | None = None):
        with self.lock:
            if brain_id and brain_id != self.state.get("active"):
                self.stop(remember=False)
                brains.brain_path(brain_id)
                self.state["active"] = brain_id
            if self.running():
                self.state["wanted_running"] = True
                self._save_state()
                return
            bdir = self.active_dir()
            (bdir / "STOP").unlink(missing_ok=True)
            job = {"brain_dir": str(bdir), "workers": self.workers(), "swap_seconds": self.app["swap_seconds"],
                   "autosave_minutes": self.app["autosave_minutes"], "keep_recent": self.app["keep_recent"],
                   "keep_every": self.app["keep_every"], "min_free_gb": self.app["min_free_gb"],
                   "live_fps": self.app["live_fps"], "league_max": self.app["league_max"], "torch_threads": 2,
                   "device": "cpu", "gpu_path": None}
            gp = self.gpu.installed() if self.app["gpu"] else None
            if gp:
                job.update(device="cuda", gpu_path=str(gp))
            S.write_json(bdir / "run.json", job)
            S.write_json(bdir / "status.json", {"state": "starting", "updated": time.time()})
            self.proc = subprocess.Popen(bootstrap("snekbrain.trainer", ["--job", str(bdir / "run.json")]),
                                         cwd=str(bdir), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                         stderr=subprocess.DEVNULL, creationflags=winsys.creation_flags(self.app["low_priority"]),
                                         close_fds=True)
            if self.limiter.assign(self.proc.pid):
                self.limiter.set_percent(self.cap())
            if not winsys.WIN and self.app["low_priority"]:
                winsys.set_tree_priority(self.proc.pid, "below")
            self.suspended_for = self.slowed_for = None
            self.closed_pause = False
            self.state["wanted_running"] = True
            self._save_state()

    def stop(self, remember: bool = True, timeout: float = 90.0):
        """Pause: ask the learner to save and stop, wait for it, force it only if it hangs."""
        with self.lock:
            if remember:
                self.state["wanted_running"] = False
                self._save_state()
            if not self.running():
                self.proc = None
                return
            if self.suspended_for:
                winsys.suspend_tree(self.proc.pid, False)
                self.suspended_for = None
            bdir = Path(self.proc.args[-1]).parent if isinstance(self.proc.args, list) else self.active_dir()
            (bdir / "STOP").write_text("stop")
            try:
                self.proc.wait(timeout)
            except subprocess.TimeoutExpired:
                self._kill_tree(self.proc.pid)
            self.proc = None

    def _kill_tree(self, pid: int):
        try:
            import psutil
            root = psutil.Process(pid)
            for p in root.children(recursive=True) + [root]:
                try:
                    p.kill()
                except Exception:
                    pass
        except Exception:
            pass

    def restart_if_running(self):
        if self.running():
            self.stop(remember=False)
            self.start()

    # ---------------------------------------------------------------- the watchdog
    def tick(self):
        """Every few seconds: videos, crashes, closed window."""
        with self.lock:
            try:
                import psutil
                self.cpu = psutil.cpu_percent(interval=None)
            except Exception:
                pass
            if self.proc is not None and self.proc.poll() is not None:
                code = self.proc.returncode
                self.proc = None
                st = S.read_json(self.active_dir() / "status.json", {})
                if code != 0 and self.state.get("wanted_running"):
                    self.last_error = st.get("error") or f"training stopped unexpectedly (code {code})"
                    now = time.time()
                    self.crashes = [t for t in self.crashes if now - t < 600] + [now]
                    retry = len(self.crashes) <= 3 and "GB free" not in self.last_error
                    if retry:
                        self.start()
                    else:
                        self.state["wanted_running"] = False
                        self._save_state()
            if not self.running():
                if (self.state.get("wanted_running") and self.closed_pause is False and not self.proc
                        and S.read_json(self.active_dir() / "status.json", {}).get("state") == "stopped"):
                    self.state["wanted_running"] = False
                    self._save_state()
                return
            # window closed and the user doesn't want training without it
            if not self.app["train_when_closed"] and time.time() - self.last_seen > 30:
                self.stop(remember=False)
                self.closed_pause = True
                return
            mode = self.app["when_video"]
            video = None
            if mode != "ignore":
                video = winsys.video_playing(self.app["video_apps"], self.app["pause_on_fullscreen"])
            if mode == "pause":
                if video and not self.suspended_for:
                    winsys.suspend_tree(self.proc.pid, True)
                    self.suspended_for = video
                elif not video and self.suspended_for:
                    winsys.suspend_tree(self.proc.pid, False)
                    self.suspended_for = None
            else:
                if self.suspended_for:
                    winsys.suspend_tree(self.proc.pid, False)
                    self.suspended_for = None
                if mode == "slow" and video and not self.slowed_for:
                    winsys.set_tree_priority(self.proc.pid, "idle")
                    self.slowed_for = video
                elif self.slowed_for and not (mode == "slow" and video):
                    winsys.set_tree_priority(self.proc.pid, "below" if self.app["low_priority"] else "normal")
                    self.slowed_for = None

    def window_seen(self):
        self.last_seen = time.time()
        if self.closed_pause and self.state.get("wanted_running"):
            self.closed_pause = False
            self.start()

    # ---------------------------------------------------------------- views
    def status(self) -> dict:
        with self.lock:
            bdir = None
            try:
                bdir = self.active_dir()
            except Exception:
                pass
            st = S.read_json(bdir / "status.json", {}) if bdir else {}
            running = self.running()
            if running:
                state = "waiting_video" if self.suspended_for else st.get("state", "starting")
                if state in ("paused", "stopped", "error"):
                    state = "starting"
            else:
                state = "paused" if st.get("state") in (None, "training", "starting", "paused") else st.get("state")
                if self.closed_pause:
                    state = "paused_closed"
            return {"version": __version__, "state": state, "mode": self.app["mode"], "running": running,
                    "active": self.state.get("active"), "generation": st.get("generation"), "steps": st.get("steps"),
                    "sps": st.get("sps") if running and not self.suspended_for else 0, "video": self.suspended_for or self.slowed_for,
                    "error": st.get("error") if st.get("state") == "error" else (self.last_error if not running else ""),
                    "reason": st.get("reason"), "workers": self.workers(), "cpu_cap": self.cap(), "machine_cpu": self.cpu,
                    "wanted_running": bool(self.state.get("wanted_running")), "cores": os.cpu_count(),
                    "stage": st.get("stage", S.read_json(bdir / "progress_state.json", {}).get("stage", 0) if bdir else 0),
                    "exploring": bool(st.get("exploring")) and running, "device": st.get("device", "cpu") if running else None}

    def set_app_settings(self, values: dict) -> dict:
        with self.lock:
            old = dict(self.app)
            new = S.validate(S.APP_SCHEMA, values, base=self.app)
            self.app = new
            S.write_json(self.settings_file, new)
            if new["start_with_windows"] != old["start_with_windows"] or new["start_with_windows"]:
                winsys.set_autostart(new["start_with_windows"], autostart_command())
            if new["ai_hook"] != old["ai_hook"]:
                self.ai_key = extras.ai_hook_set(new["ai_hook"], Api.port)
            if self.running():
                if any(new[k] != old[k] for k in ("mode", "workers_normal", "workers_turbo", "swap_seconds",
                                                    "autosave_minutes", "keep_recent", "keep_every", "min_free_gb",
                                                    "live_fps", "league_max", "low_priority", "gpu")):
                    self.restart_if_running()
                elif new[f"cpu_cap_{new['mode']}"] != old[f"cpu_cap_{old['mode']}"]:
                    self.limiter.set_percent(self.cap())
            return new


def autostart_command() -> str:
    exe = python_for_children()
    launcher = Path(sys.argv[0]).resolve() if sys.argv and sys.argv[0] else None
    if launcher and launcher.suffix.lower() in (".pyw", ".py") and launcher.exists():
        return f'"{exe}" "{launcher}" --background'
    code = f"import sys; sys.path.insert(0, {str(PKG.parent)!r}); from snekbrain.app import main; main()"
    return f'"{exe}" -c "{code}" --background'


# =================================================================== HTTP
class Api(BaseHTTPRequestHandler):
    server_version = "SnekBrain"
    ctl: Controller = None  # set by serve()
    token: str = ""
    port: int = 0

    def log_message(self, *a):
        pass

    # ---------------- helpers
    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").lower()
        return host in (f"127.0.0.1:{self.port}", f"localhost:{self.port}")

    def _send(self, code: int, body: bytes, ctype: str = "application/json", extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200):
        self._send(code, json.dumps(obj).encode())

    def _err(self, msg: str, code: int = 400):
        self._json({"error": msg}, code)

    def _body(self, limit: int = 2_000_000) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        if n > limit:
            raise ValueError("too big")
        return self.rfile.read(n) if n else b""

    def _file(self, path: Path):
        try:
            data = path.read_bytes()
        except (FileNotFoundError, PermissionError):
            return self._err("not found", 404)
        self._send(200, data, "application/json")

    # ---------------- routing
    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def _route(self, method: str):
        if not self._host_ok():
            return self._err("forbidden", 403)
        url = urlparse(self.path)
        path = url.path
        try:
            if not path.startswith("/api/"):
                if method != "GET":
                    return self._err("not found", 404)
                return self._static(path)
            parts = [p for p in path.split("/") if p][1:]
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            ai = self.headers.get("X-SL-AI-Key")
            if ai is not None:  # an AI assistant on this PC: only when the hook is on, only the safe routes
                key = self.ctl.ai_key
                if not (key and self.ctl.app["ai_hook"] and secrets.compare_digest(ai, key)
                        and parts and extras.ai_route_ok(method, parts)):
                    return self._err("forbidden", 403)
            elif not secrets.compare_digest(self.headers.get("X-SL-Token", ""), self.token):
                return self._err("forbidden", 403)
            return self._api(method, parts, q)
        except FileNotFoundError as e:
            return self._err(str(e) or "not found", 404)
        except ValueError as e:
            return self._err(str(e), 400)
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            return self._err(f"{type(e).__name__}: {e}", 500)

    def _static(self, path: str):
        if path in ("/", "/index.html"):
            html = (WEB / "index.html").read_text(encoding="utf-8").replace("__SL_TOKEN__", self.token)
            csp = ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
                   "img-src 'self' data: blob:; connect-src 'self'; frame-ancestors 'none'")
            return self._send(200, html.encode(), "text/html; charset=utf-8", {"Content-Security-Policy": csp})
        name = path.lstrip("/")
        f = (WEB / name).resolve()
        if WEB.resolve() not in f.parents or not f.is_file():
            return self._err("not found", 404)
        ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
        if f.suffix == ".js":
            ctype = "text/javascript"
        self._send(200, f.read_bytes(), ctype)

    def _api(self, method: str, p: list[str], q: dict):
        ctl = self.ctl
        if method == "GET":
            if p == ["status"]:
                ctl.window_seen()
                return self._json(ctl.status())
            if p == ["brains"]:
                return self._json({"brains": brains.list_brains(), "active": ctl.state.get("active")})
            if p == ["settings"]:
                return self._json({"app": ctl.app, "schema": S.schema_for_ui(),
                                   "data_dir": str(ctl.data), "autostart": winsys.get_autostart()})
            if p == ["live-game"]:
                return self._file(ctl.active_dir() / "live_game.json")
            if p == ["update"]:
                st = ctl.updater.check(force=q.get("force") == "1") if (ctl.app["check_updates"] or q.get("force") == "1") \
                    else ctl.updater.state
                return self._json(dict(st, job=ctl.updater.job))
            if p == ["gpu"]:
                return self._json(ctl.gpu.info())
            if p == ["ai-hook"]:
                return self._json({"enabled": ctl.app["ai_hook"], "key": ctl.ai_key if ctl.app["ai_hook"] else None,
                                   "port": self.port, "file": str(extras.ai_hook_file()),
                                   "mcp_command": [python_for_children().replace("pythonw", "python"), "-c",
                                                   f"import sys; sys.path.insert(0, {str(PKG.parent)!r}); "
                                                   "from snekbrain.mcp_server import main; main()"]})
            if len(p) >= 2 and p[0] == "brain":
                bdir = brains.brain_path(p[1])
                if len(p) == 2:
                    meta = brains.load_meta(bdir)
                    from .trainer import world_info
                    return self._json({"info": brains.info(bdir), "settings": meta["settings"],
                                       "world": world_info(S.validate(S.BRAIN_SCHEMA, meta["settings"])),
                                       "progress": S.read_json(bdir / "progress_state.json", {}),
                                       "trained": bool(brains.gen_files(bdir))})
                if p[2:] == ["progress"]:
                    return self._json({"rows": brains.read_progress(bdir)})
                if p[2:] == ["hall"]:
                    return self._json({"records": brains.read_hall(bdir)})
                if p[2:] == ["events"]:
                    return self._json({"events": brains.read_events(bdir, int(q.get("n", 50)))})
                if p[2:] == ["live-version"]:
                    f = bdir / "web" / "live.json"
                    gens = brains.gen_files(bdir)
                    return self._json({"v": f.stat().st_mtime if f.exists() else (max(gens) if gens else 0)})
                if p[2:] == ["highlights"]:
                    return self._json({"highlights": extras.list_highlights(bdir)})
                if len(p) == 4 and p[2] == "highlight":
                    return self._file(extras.highlight_path(bdir, p[3]))
                if p[2:] == ["live"]:
                    f = bdir / "web" / "live.json"
                    if f.exists():
                        return self._file(f)
                    gens = brains.gen_files(bdir)
                    if not gens:
                        return self._err("not trained yet", 404)
                    return self._gen(bdir, max(gens))
                if len(p) == 4 and p[2] == "gen":
                    return self._gen(bdir, int(p[3]))
                if p[2:] == ["export"]:
                    data = brains.export_bytes(bdir, all_gens=q.get("all") == "1")
                    name = brains.clean_name(brains.info(bdir)["name"]).replace(" ", "_") + brains.EXPORT_EXT
                    return self._send(200, data, "application/octet-stream",
                                      {"Content-Disposition": f'attachment; filename="{name}"'})
            return self._err("not found", 404)

        # POST
        if len(p) == 3 and p[0] == "brain" and p[2] == "highlights":
            clip = json.loads(self._body(limit=8_000_000) or b"{}")
            return self._json({"ok": True, "name": extras.save_highlight(brains.brain_path(p[1]), clip)})
        if p == ["brains", "import"]:
            data = self._body(limit=brains.MAX_IMPORT_BYTES)
            b = brains.import_bytes(data, self.headers.get("X-SL-Name") or None)
            return self._json({"ok": True, "id": b.name})
        body = json.loads(self._body() or b"{}")
        if p == ["start"]:
            ctl.start(body.get("brain"))
            return self._json(ctl.status())
        if p == ["pause"]:
            ctl.stop()
            return self._json(ctl.status())
        if p == ["mode"]:
            ctl.set_app_settings({"mode": body.get("mode")})
            return self._json(ctl.status())
        if p == ["settings"]:
            return self._json({"app": ctl.set_app_settings(body.get("app", {}))})
        if p == ["brains", "new"]:
            b = brains.create(body.get("name") or "Newborn", body.get("preset") or "quick", body.get("settings") or {})
            if body.get("start"):
                ctl.start(b.name)
            return self._json({"ok": True, "id": b.name})
        if p == ["brains", "import-folder"]:
            b = brains.import_run_folder(Path(str(body.get("path", ""))).expanduser(), body.get("name") or "Imported run")
            return self._json({"ok": True, "id": b.name})
        if p == ["open-folder"]:
            target = ctl.data
            if body.get("brain"):
                target = brains.brain_path(body["brain"])
            winsys.open_folder(target)
            return self._json({"ok": True})
        if p == ["update", "install"]:
            ctl.updater.install(lambda: threading.Thread(target=shutdown, args=(True,), daemon=True).start())
            return self._json({"ok": True})
        if p == ["gpu", "install"]:
            ctl.gpu.install()
            return self._json({"ok": True})
        if p == ["gpu", "remove"]:
            if ctl.app["gpu"]:
                ctl.set_app_settings({"gpu": False})
            ctl.gpu.remove()
            return self._json({"ok": True})
        if len(p) == 5 and p[0] == "brain" and p[2] == "highlight" and p[4] == "delete":
            extras.highlight_path(brains.brain_path(p[1]), p[3]).unlink(missing_ok=True)
            return self._json({"ok": True})
        if p == ["quit"]:
            self._json({"ok": True})
            threading.Thread(target=shutdown, args=(bool(body.get("resume_later")),), daemon=True).start()
            return
        if len(p) == 3 and p[0] == "brain":
            bdir = brains.brain_path(p[1])
            is_active = p[1] == ctl.state.get("active")
            if p[2] == "rename":
                brains.rename(bdir, body.get("name", ""))
                return self._json({"ok": True})
            if p[2] == "settings":
                st = brains.update_settings(bdir, body.get("settings", {}))
                if is_active:
                    ctl.restart_if_running()
                return self._json({"ok": True, "settings": st})
            if p[2] == "branch":
                g = body.get("generation")
                b = brains.branch(bdir, None if g in (None, "") else int(g), body.get("name") or "Branch")
                return self._json({"ok": True, "id": b.name})
            if p[2] == "select":
                was = ctl.running()
                if was:
                    ctl.start(p[1])
                else:
                    ctl.state["active"] = p[1]
                    ctl._save_state()
                return self._json(ctl.status())
            if p[2] == "delete":
                if is_active and ctl.running():
                    raise ValueError("pause training before deleting the brain that is training")
                brains.delete(bdir)
                if is_active:
                    ctl.state["active"] = None
                    ctl._ensure_default_brain()
                return self._json({"ok": True})
        return self._err("not found", 404)

    def _gen(self, bdir: Path, g: int):
        f = bdir / "web" / f"gen_{g:04d}.json"
        if not f.exists():
            gens = brains.gen_files(bdir)
            if g not in gens:
                return self._err("that generation isn't saved", 404)
            from .trainer import export_checkpoint, world_info, write_json
            meta = brains.load_meta(bdir)
            f.parent.mkdir(exist_ok=True)
            write_json(f, export_checkpoint(gens[g], g, world_info(meta["settings"])))
        return self._file(f)


_httpd: ThreadingHTTPServer | None = None
_ctl: Controller | None = None


def shutdown(resume_later: bool = False):
    """Stop training (saving everything) and close the engine. resume_later: carry on training next start."""
    time.sleep(0.3)
    if _ctl:
        _ctl.stop(remember=not resume_later)
    if _httpd:
        _httpd.shutdown()


def server_file() -> Path:
    return brains.data_dir() / "server.json"


def serve(background: bool = False):
    global _httpd, _ctl
    _ctl = Controller()
    Api.ctl = _ctl
    Api.token = secrets.token_urlsafe(24)
    try:
        _httpd = ThreadingHTTPServer(("127.0.0.1", PREFERRED_PORT), Api)
    except OSError:
        _httpd = ThreadingHTTPServer(("127.0.0.1", 0), Api)
    _httpd.daemon_threads = True
    Api.port = _httpd.server_address[1]
    S.write_json(server_file(), {"port": Api.port, "token": Api.token, "pid": os.getpid(), "version": __version__})
    _ctl.ai_key = extras.ai_hook_set(_ctl.app["ai_hook"], Api.port)
    if _ctl.state.get("wanted_running"):  # it was training when the PC shut down: carry on
        try:
            _ctl.start()
        except Exception:
            traceback.print_exc()

    def watchdog():
        while True:
            time.sleep(3)
            try:
                _ctl.tick()
            except Exception:
                traceback.print_exc()

    threading.Thread(target=watchdog, daemon=True).start()
    try:
        _httpd.serve_forever(poll_interval=0.5)
    finally:
        try:
            server_file().unlink()
        except FileNotFoundError:
            pass


def main():
    log = brains.data_dir() / "app.log"
    try:
        if log.exists() and log.stat().st_size > 2_000_000:
            log.replace(log.with_suffix(".old.log"))
    except OSError:
        pass
    f = open(log, "a", buffering=1, encoding="utf-8")
    sys.stdout = sys.stderr = f
    print(f"--- {time.strftime('%Y-%m-%d %H:%M:%S')} SnekBrain {__version__} server start", flush=True)
    serve(background="--background" in sys.argv)


if __name__ == "__main__":
    main()
