"""The optional extras: highlight replays, the update check, the GPU add-on and the AI hook.

Network use is limited to two things, both only when the user asks for them or turns them on:
- the update check (GitHub's public releases API, once a day, can be turned off), and
- downloading an update or the GPU add-on, only after the user clicks to install it.
Every download is checked against a SHA-256 checksum before it is used.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import zipfile
from html.parser import HTMLParser
from pathlib import Path

from . import UPDATE_REPO, __version__, brains
from . import settings as S

UA = f"SnekBrain/{__version__}"


# =================================================================== highlights
HL_DIR = "highlights"
HL_MAX = 60
HL_KINDS = {"record", "close", "kill", "manual"}
_num = (int, float)


def _valid_clip(c: dict) -> dict:
    """Highlights are drawn by the window, so only plain numbers and short text are kept."""
    if not isinstance(c, dict):
        raise ValueError("bad highlight")
    kind = str(c.get("kind", "manual"))
    if kind not in HL_KINDS:
        raise ValueError("bad highlight kind")
    frames_in = c.get("frames")
    if not isinstance(frames_in, list) or not (1 <= len(frames_in) <= 600):
        raise ValueError("bad highlight frames")
    frames = []
    for f in frames_in:
        snakes = []
        for s in (f.get("snakes") or [])[:80]:
            b = s.get("b") or []
            if not isinstance(b, list) or len(b) > 20000 or not all(isinstance(v, _num) for v in b):
                raise ValueError("bad highlight snake")
            k = str(s.get("k", "b"))[:1]
            snakes.append({"k": k if k in "ahltb" else "b", "c": int(s.get("c", 0)) % 1000, "n": int(s.get("n", 12)),
                           "h": float(s.get("h", 0)), "b": b,
                           **({"label": str(s["label"])[:40]} if s.get("label") else {})})
        food = f.get("food") or []
        if not isinstance(food, list) or len(food) > 30000 or not all(isinstance(v, _num) for v in food):
            raise ValueError("bad highlight food")
        frames.append({"snakes": snakes, "food": food})
    gen = c.get("generation")
    return {"kind": kind, "label": str(c.get("label", ""))[:120], "fps": max(1, min(60, int(c.get("fps", 15)))),
            "generation": float(gen) if isinstance(gen, _num) else None, "wrap": bool(c.get("wrap")),
            "L": float(c.get("L") or 0), "R": float(c.get("R") or 0), "t": time.time(), "frames": frames}


def save_highlight(bdir: Path, clip: dict) -> str:
    clip = _valid_clip(clip)
    d = bdir / HL_DIR
    d.mkdir(exist_ok=True)
    name = time.strftime("%Y%m%d-%H%M%S") + f"-{clip['kind']}-{secrets.token_hex(2)}.json"
    tmp = d / (name + ".part")
    tmp.write_text(json.dumps(clip, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp, d / name)
    files = sorted(d.glob("*.json"))
    for old in files[:-HL_MAX]:
        old.unlink(missing_ok=True)
    return name


def list_highlights(bdir: Path) -> list[dict]:
    out = []
    for p in sorted((bdir / HL_DIR).glob("*.json"), reverse=True):
        try:
            with open(p, encoding="utf-8") as f:
                head = f.read(4096)  # kind/label/generation come first in the file
            m = {k: re.search(rf'"{k}":\s*("([^"\\]|\\.)*"|[-\d.e]+|null)', head) for k in ("kind", "label", "generation", "t")}
            item = {"name": p.name}
            for k, v in m.items():
                if v:
                    item[k] = json.loads(v.group(1))
            out.append(item)
        except (OSError, ValueError):
            continue
    return out


def highlight_path(bdir: Path, name: str) -> Path:
    if not re.fullmatch(r"[\w.-]{1,120}\.json", name or ""):
        raise ValueError("bad highlight name")
    return bdir / HL_DIR / name


# =================================================================== downloads
def _get(url: str, timeout: float = 15.0) -> bytes:
    if not url.startswith("https://"):
        raise ValueError("only https downloads are allowed")
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - https only, checked above
        return r.read()


def _download(url: str, dest: Path, progress, expect_sha256: str | None = None) -> str:
    """Streams a file to dest, reporting progress(done, total). Returns its SHA-256."""
    if not url.startswith("https://"):
        raise ValueError("only https downloads are allowed")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    h = hashlib.sha256()
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as f:  # noqa: S310
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
            h.update(chunk)
            done += len(chunk)
            progress(done, total)
    digest = h.hexdigest()
    if expect_sha256 and digest.lower() != expect_sha256.lower():
        tmp.unlink(missing_ok=True)
        raise ValueError("the download didn't match its checksum, so it was thrown away")
    os.replace(tmp, dest)
    return digest


def _vtuple(v: str):
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3]) or (0,)


# =================================================================== update check
class Updater:
    def __init__(self, data: Path):
        self.file = data / "update.json"
        self.dir = data / "updates"
        self.state = S.read_json(self.file, {})
        self.job = {"state": "idle"}

    def check(self, force: bool = False) -> dict:
        now = time.time()
        if not force and now - float(self.state.get("checked", 0)) < 86400:
            return self.state
        try:
            rel = json.loads(_get(f"https://api.github.com/repos/{UPDATE_REPO}/releases/latest", 8))
        except Exception as e:  # noqa: BLE001 - offline is normal
            self.state.update(checked=now, error=f"couldn't reach GitHub ({type(e).__name__})")
            S.write_json(self.file, self.state)
            return self.state
        tag = str(rel.get("tag_name", ""))
        assets = {a.get("name", ""): a.get("browser_download_url", "") for a in rel.get("assets", [])}
        setup = next((n for n in assets if n.lower().endswith(".exe") and "setup" in n.lower()), None)
        sums = next((n for n in assets if n.lower() in ("sha256sums.txt", "checksums.txt")), None)
        self.state = {"checked": now, "latest": tag, "current": __version__,
                      "newer": _vtuple(tag) > _vtuple(__version__), "page": str(rel.get("html_url", "")),
                      "notes": str(rel.get("body", ""))[:4000], "setup": setup, "setup_url": assets.get(setup or "", ""),
                      "sums_url": assets.get(sums or "", "")}
        S.write_json(self.file, self.state)
        return self.state

    def install(self, quit_app):
        """Download the new setup.exe, check its SHA-256 against the release's checksum file, run it."""
        st = self.state
        if not st.get("newer") or not st.get("setup_url") or not st.get("sums_url"):
            raise ValueError("no newer version with a checksum to install")
        url, sums_url, name = st["setup_url"], st["sums_url"], st["setup"]
        allowed = ("https://github.com/", "https://objects.githubusercontent.com/")
        if not url.startswith(allowed) or not sums_url.startswith(allowed):
            raise ValueError("unexpected download address")
        if not re.fullmatch(r"[\w.+-]{1,120}\.exe", name or ""):
            raise ValueError("unexpected file name")

        def work():
            try:
                self.job = {"state": "downloading", "done": 0, "total": 0}
                sums = _get(sums_url).decode("utf-8", "replace")
                want = None
                for line in sums.splitlines():
                    parts = line.strip().split()
                    if len(parts) >= 2 and parts[-1].lstrip("*") == name and re.fullmatch(r"[0-9a-fA-F]{64}", parts[0]):
                        want = parts[0]
                if not want:
                    raise ValueError("the release has no checksum for the installer")
                dest = self.dir / name

                def prog(d, t):
                    self.job.update(done=d, total=t)
                _download(url, dest, prog, want)
                self.job = {"state": "starting installer"}
                if sys.platform == "win32":
                    subprocess.Popen([str(dest)], close_fds=True)  # noqa: S603 - our own checked installer
                    quit_app()
                self.job = {"state": "done", "file": str(dest)}
            except Exception as e:  # noqa: BLE001
                self.job = {"state": "error", "error": str(e)}

        threading.Thread(target=work, daemon=True).start()


# =================================================================== GPU add-on
class _Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.links.append(href)


class Gpu:
    """Optional CUDA build of PyTorch, kept in the data folder so the installer stays small.

    Older NVIDIA cards (Maxwell, Pascal like the Quadro P5000, Volta) need PyTorch 2.14 with CUDA 12.6,
    the last build that supports them. Newer cards use the current PyTorch with CUDA 13.
    """
    PICKS = [((7, 5), "2.15.0", "cu132"), ((5, 0), "2.14.0", "cu126")]

    def __init__(self, data: Path):
        self.root = data / "gpu"
        self.job = {"state": "idle"}
        self._card = None

    def card(self) -> dict | None:
        if self._card is not None:
            return self._card or None
        self._card = {}
        try:
            kw = {"creationflags": 0x08000000} if sys.platform == "win32" else {}
            out = subprocess.run(["nvidia-smi", "--query-gpu=name,compute_cap,memory.total", "--format=csv,noheader"],
                                 capture_output=True, text=True, timeout=8, **kw).stdout.strip().splitlines()
            if out:
                name, cap, mem = [x.strip() for x in out[0].split(",")[:3]]
                self._card = {"name": name, "compute_cap": cap, "memory": mem}
        except Exception:  # noqa: BLE001 - no NVIDIA card or driver
            pass
        return self._card or None

    def pick(self) -> tuple[str, str] | None:
        c = self.card()
        if not c:
            return None
        try:
            cap = tuple(int(x) for x in c["compute_cap"].split(".")[:2])
        except ValueError:
            return None
        for need, ver, cu in self.PICKS:
            if cap >= need:
                return ver, cu
        return None

    def installed(self) -> Path | None:
        for d in sorted(self.root.glob("torch-*"), reverse=True):
            if (d / "torch" / "__init__.py").exists() and (d / "ok.json").exists():
                return d
        return None

    def info(self) -> dict:
        p = self.pick()
        inst = self.installed()
        return {"card": self.card(), "build": f"PyTorch {p[0]} + {p[1]}" if p else None,
                "installed": inst.name if inst else None, "job": self.job,
                "supported": sys.platform == "win32" and p is not None}

    def install(self):
        p = self.pick()
        if not p:
            raise ValueError("no supported NVIDIA graphics card found")
        if sys.platform != "win32":
            raise ValueError("the GPU add-on is for Windows")
        ver, cu = p
        py = f"cp{sys.version_info.major}{sys.version_info.minor}"
        want = f"torch-{ver}+{cu}-{py}-{py}-win_amd64.whl"

        def work():
            try:
                self.job = {"state": "finding the download"}
                index = f"https://download.pytorch.org/whl/{cu}/torch/"
                parser = _Links()
                parser.feed(_get(index, 30).decode("utf-8", "replace"))
                href = next((h for h in parser.links if h.split("#")[0].split("/")[-1].replace("%2B", "+") == want), None)
                if not href:
                    raise ValueError(f"couldn't find {want} on the PyTorch site")
                sha = None
                if "#sha256=" in href:
                    sha = href.split("#sha256=")[1][:64]
                url = urllib.request.urljoin(index, href.split("#")[0])
                if not url.startswith("https://download.pytorch.org/"):
                    raise ValueError("unexpected download address")
                whl = self.root / "downloads" / want
                self.job = {"state": "downloading", "done": 0, "total": 0}

                def prog(d, t):
                    self.job.update(done=d, total=t)
                _download(url, whl, prog, sha)
                self.job = {"state": "unpacking"}
                dest = self.root / f"torch-{ver}-{cu}"
                if dest.exists():
                    shutil.rmtree(dest)
                with zipfile.ZipFile(whl) as z:
                    root = dest.resolve()
                    for m in z.infolist():
                        out = (dest / m.filename).resolve()
                        if root not in out.parents and out != root:
                            raise ValueError("unsafe file in the download")
                    z.extractall(dest)
                S.write_json(dest / "ok.json", {"version": ver, "cuda": cu, "sha256": sha, "t": time.time()})
                whl.unlink(missing_ok=True)
                self.job = {"state": "done"}
            except Exception as e:  # noqa: BLE001
                self.job = {"state": "error", "error": str(e)}

        threading.Thread(target=work, daemon=True).start()

    def remove(self):
        if self.root.exists():
            shutil.rmtree(self.root, ignore_errors=True)
        self.job = {"state": "idle"}


# =================================================================== AI hook
AI_ROUTES_GET = {("status",), ("brains",), ("brain", "*"), ("brain", "*", "progress"), ("brain", "*", "hall"),
                 ("brain", "*", "events")}
AI_ROUTES_POST = {("start",), ("pause",), ("mode",), ("brain", "*", "select")}


def ai_route_ok(method: str, parts: list[str]) -> bool:
    shape = tuple("*" if i == 1 and parts[0] == "brain" else p for i, p in enumerate(parts))
    return shape in (AI_ROUTES_GET if method == "GET" else AI_ROUTES_POST)


def ai_hook_file() -> Path:
    return brains.data_dir() / "ai_hook.json"


def ai_hook_set(enabled: bool, port: int) -> str | None:
    """Writes (or removes) the key file an AI assistant on this PC reads. Returns the key."""
    f = ai_hook_file()
    if not enabled:
        f.unlink(missing_ok=True)
        return None
    old = S.read_json(f, {})
    key = old.get("key") or secrets.token_urlsafe(24)
    S.write_json(f, {"port": port, "key": key, "note": "SnekBrain AI hook. Delete this file or turn the hook off to revoke."})
    return key
