"""The brain library: every brain is one folder you can save, load, copy, branch, export and import.

<data>/Brains/<id>/
    brain.json          name, where it came from, its settings
    progress.csv        one line per generation (the chart)
    generations/        gen_0000.zip ... full training checkpoints (Stable-Baselines3 PPO)
    latest.zip          the newest checkpoint (saved on pause and every few minutes)
    league/             frozen old champions it plays against (.npz)
    web/                small JSON copies of brains for the on-screen game
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
import secrets
import shutil
import sys
import time
import zipfile
from pathlib import Path

from . import settings as S

BRAIN_FILE = "brain.json"
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,60}$")


def data_dir() -> Path:
    """Where everything the user makes lives. Never inside the install folder."""
    env = os.environ.get("SNEKBRAIN_HOME")
    if env:
        p = Path(env)
    elif sys.platform == "win32":
        p = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "SnekBrain"
    else:
        p = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "snekbrain"
    p.mkdir(parents=True, exist_ok=True)
    return p


def brains_dir() -> Path:
    p = data_dir() / "Brains"
    p.mkdir(parents=True, exist_ok=True)
    return p


def clean_name(name: str) -> str:
    name = re.sub(r"[\x00-\x1f<>:\"/\\|?*]", "", str(name or "")).strip()[:40]
    return name or "Snake"


def _new_id(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:30] or "brain"
    return f"{slug}-{secrets.token_hex(3)}"


def brain_path(brain_id: str) -> Path:
    if not isinstance(brain_id, str) or not _ID_RE.match(brain_id):
        raise ValueError("bad brain id")
    p = brains_dir() / brain_id
    if not (p / BRAIN_FILE).exists():
        raise FileNotFoundError("no such brain")
    return p


def gen_files(bdir: Path) -> dict[int, Path]:
    out = {}
    for p in (bdir / "generations").glob("gen_*.zip"):
        try:
            out[int(p.stem.split("_")[1])] = p
        except ValueError:
            pass
    return out


def read_progress(bdir: Path) -> list[dict]:
    rows = []
    try:
        with open(bdir / "progress.csv", newline="") as f:
            for r in csv.DictReader(f):
                try:
                    rows.append({k: float(v) for k, v in r.items() if v not in (None, "")})
                except ValueError:
                    continue
    except FileNotFoundError:
        pass
    return rows


def info(bdir: Path) -> dict:
    meta = S.read_json(bdir / BRAIN_FILE, {})
    gens = gen_files(bdir)
    prog = read_progress(bdir)
    last = prog[-1] if prog else {}
    best = max((r.get("avg_max_len", 0) for r in prog), default=0)
    return {"id": bdir.name, "name": meta.get("name", bdir.name), "created": meta.get("created", 0),
            "parent": meta.get("parent"), "preset": meta.get("preset"), "notes": meta.get("notes", ""),
            "generation": int(last.get("generation", max(gens) if gens else 0)),
            "steps": int(last.get("timesteps", 0)), "avg_len": last.get("avg_max_len"),
            "best_avg_len": best, "saved_gens": sorted(gens)}


def list_brains() -> list[dict]:
    out = []
    for p in sorted(brains_dir().iterdir()):
        if p.is_dir() and (p / BRAIN_FILE).exists():
            try:
                out.append(info(p))
            except Exception:
                continue
    out.sort(key=lambda b: b["created"])
    return out


def load_meta(bdir: Path) -> dict:
    meta = S.read_json(bdir / BRAIN_FILE, {})
    meta["settings"] = S.validate(S.BRAIN_SCHEMA, meta.get("settings", {}))
    return meta


def save_meta(bdir: Path, meta: dict):
    S.write_json(bdir / BRAIN_FILE, meta)


def create(name: str, preset: str = "quick", overrides: dict | None = None, parent: dict | None = None) -> Path:
    """A brand-new random brain. Nothing is trained yet: generation 0 is made when training first starts."""
    name = clean_name(name)
    values = S.validate(S.BRAIN_SCHEMA, overrides or {}, base=S.preset_values(preset))
    bdir = brains_dir() / _new_id(name)
    (bdir / "generations").mkdir(parents=True)
    save_meta(bdir, {"version": 1, "name": name, "created": time.time(), "preset": preset,
                     "parent": parent, "settings": values, "notes": ""})
    return bdir


def update_settings(bdir: Path, values: dict) -> dict:
    meta = load_meta(bdir)
    trained = bool(gen_files(bdir))
    meta["settings"] = S.validate(S.BRAIN_SCHEMA, values, base=meta["settings"], allow_new_only=not trained)
    save_meta(bdir, meta)
    return meta["settings"]


def rename(bdir: Path, name: str):
    meta = S.read_json(bdir / BRAIN_FILE, {})
    meta["name"] = clean_name(name)
    save_meta(bdir, meta)


def _copy_progress(src: Path, dst: Path, upto: int):
    if not (src / "progress.csv").exists():
        return
    with open(src / "progress.csv", newline="") as f, open(dst / "progress.csv", "w", newline="") as g:
        r, w = csv.reader(f), csv.writer(g)
        head = next(r, None)
        if head:
            w.writerow(head)
            for row in r:
                try:
                    if int(float(row[0])) <= upto:
                        w.writerow(row)
                except (ValueError, IndexError):
                    continue


def branch(src: Path, gen: int | None, name: str) -> Path:
    """A new brain that starts from one generation of another. The original is never changed."""
    gens = gen_files(src)
    if not gens:
        raise ValueError("that brain has no saved generations yet")
    if gen is None:
        gen = max(gens)
    if gen not in gens:
        raise ValueError(f"generation {gen} isn't saved (saved: {', '.join(map(str, sorted(gens)[-8:]))} ...)")
    meta = load_meta(src)
    dst = brains_dir() / _new_id(clean_name(name))
    (dst / "generations").mkdir(parents=True)
    keep = {gen}
    if 0 in gens:
        keep.add(0)
    for g in keep:
        shutil.copy2(gens[g], dst / "generations" / gens[g].name)
    for sub in ("league",):
        if (src / sub).exists():
            shutil.copytree(src / sub, dst / sub)
    if (src / "web").exists():
        (dst / "web").mkdir()
        for g in keep:
            j = src / "web" / f"gen_{g:04d}.json"
            if j.exists():
                shutil.copy2(j, dst / "web" / j.name)
    _copy_progress(src, dst, gen)
    save_meta(dst, {"version": 1, "name": clean_name(name), "created": time.time(), "preset": meta.get("preset"),
                    "parent": {"id": src.name, "name": meta.get("name"), "generation": gen},
                    "settings": meta["settings"], "notes": ""})
    return dst


def delete(bdir: Path):
    """Moves the brain to the Recycle Bin on Windows (so it can be brought back), else removes it."""
    from . import winsys
    if not winsys.recycle(bdir):
        shutil.rmtree(bdir)


def prune(bdir: Path, keep_recent: int, keep_every: int):
    """Thin out old generations so endless training doesn't fill the disk. Gen 0 always stays."""
    gens = gen_files(bdir)
    if not gens:
        return []
    order = sorted(gens)
    recent = set(order[-keep_recent:])
    famous = hall_gens(bdir)
    gone = []
    for g in order:
        if g == 0 or g in recent or g in famous or (keep_every and g % keep_every == 0):
            continue
        for p in (gens[g], bdir / "web" / f"gen_{g:04d}.json"):
            try:
                p.unlink()
            except FileNotFoundError:
                pass
        gone.append(g)
    return gone


# ------------------------------------------------------------- Hall of Fame
HALL_FILE = "hall.json"


def read_hall(bdir: Path) -> list[dict]:
    return S.read_json(bdir / HALL_FILE, {}).get("records", [])


def hall_gens(bdir: Path) -> set[int]:
    out = set()
    for r in read_hall(bdir)[-40:]:  # the 40 newest records keep their generation forever
        try:
            out.add(int(r["generation"]))
        except (KeyError, TypeError, ValueError):
            continue
    return out


EVENTS_FILE = "events.jsonl"


def add_event(bdir: Path, gen, text: str, kind: str = "info"):
    """One line for the "what's happening" panel."""
    try:
        with open(bdir / EVENTS_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps({"t": time.time(), "generation": gen, "kind": kind, "text": text}) + "\n")
    except OSError:
        pass


def read_events(bdir: Path, n: int = 50) -> list[dict]:
    try:
        lines = (bdir / EVENTS_FILE).read_text(encoding="utf-8").splitlines()[-n:]
    except OSError:
        return []
    out = []
    for ln in lines:
        try:
            out.append(json.loads(ln))
        except ValueError:
            continue
    return out


def add_hall(bdir: Path, record: dict):
    """A generation that set a record. Hall of Fame generations are never thinned out."""
    recs = read_hall(bdir)
    recs.append(record)
    S.write_json(bdir / HALL_FILE, {"records": recs[-500:]})


def prune_league(bdir: Path, keep: int = 30):
    files = sorted((bdir / "league").glob("*.npz"), key=lambda p: p.stat().st_mtime)
    seeds = [p for p in files if not p.name.startswith(bdir.name)]  # champions it was given at the start
    own = [p for p in files if p.name.startswith(bdir.name)]
    for p in own[: max(0, len(own) - max(1, keep - len(seeds)))]:
        p.unlink(missing_ok=True)


# ------------------------------------------------------------- export / import
EXPORT_EXT = ".snekbrain"
MAX_IMPORT_BYTES = 2_000_000_000


def export_bytes(bdir: Path, all_gens: bool = False) -> bytes:
    """One file to back up or share: the settings, the chart, gen 0, the newest generation and its league."""
    gens = gen_files(bdir)
    pick = set(gens) if all_gens else (({0, max(gens)} | hall_gens(bdir)) & set(gens) if gens else set())
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        meta = S.read_json(bdir / BRAIN_FILE, {})
        z.writestr(BRAIN_FILE, json.dumps(meta, indent=1))
        if (bdir / "progress.csv").exists():
            z.write(bdir / "progress.csv", "progress.csv")
        for g in sorted(pick):
            z.write(gens[g], f"generations/{gens[g].name}")
        for p in sorted((bdir / "league").glob("*.npz")):
            z.write(p, f"league/{p.name}")
        if (bdir / HALL_FILE).exists():
            z.write(bdir / HALL_FILE, HALL_FILE)
        for p in sorted((bdir / "highlights").glob("*.json"))[-50:]:
            z.write(p, f"highlights/{p.name}")
    return buf.getvalue()


def _safe_members(z: zipfile.ZipFile):
    """Only the files a brain can hold; refuses paths that try to climb out of the folder."""
    ok = re.compile(r"^(brain\.json|progress\.csv|hall\.json|generations/gen_\d{4,6}\.zip|"
                    r"league/[\w.-]{1,120}\.npz|highlights/[\w.-]{1,120}\.json)$")
    total = 0
    for m in z.infolist():
        if m.is_dir():
            continue
        if not ok.match(m.filename):
            continue
        total += m.file_size
        if total > MAX_IMPORT_BYTES:
            raise ValueError("that file is too big")
        yield m


def import_bytes(data: bytes, name: str | None = None) -> Path:
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise ValueError("that isn't a SnekBrain brain file")
    with z:
        names = set(z.namelist())
        if BRAIN_FILE not in names:
            raise ValueError("that isn't a SnekBrain brain file")
        meta = json.loads(z.read(BRAIN_FILE).decode("utf-8"))
        meta["settings"] = S.validate(S.BRAIN_SCHEMA, {k: v for k, v in meta.get("settings", {}).items()
                                                        if k in S.BRAIN_SCHEMA})
        nm = clean_name(name or meta.get("name") or "Imported")
        dst = brains_dir() / _new_id(nm)
        dst.mkdir(parents=True)
        (dst / "generations").mkdir()
        for m in _safe_members(z):
            if m.filename == BRAIN_FILE:
                continue
            out = dst / m.filename
            out.parent.mkdir(parents=True, exist_ok=True)
            with z.open(m) as src, open(out, "wb") as f:
                shutil.copyfileobj(src, f)
    meta.update({"name": nm, "created": time.time(), "version": 1})
    save_meta(dst, meta)
    return dst


def settings_from_run_config(cfg: dict) -> dict:
    """Translate a training run folder's arena_config.json (the PC runs v1 to v1.2) into brain settings."""
    a = cfg.get("arena", {})
    m = {"wrap": a.get("wrap"), "half_size": a.get("half_size"), "world_radius": a.get("world_radius"),
         "view": a.get("view_radius"), "food": a.get("food_count"), "food_value": a.get("food_value"),
         "bot_view": a.get("bot_view_radius"), "max_steps_per_life": a.get("max_steps_per_life"),
         "r_food": a.get("r_food"), "r_kill": a.get("r_kill"), "r_death": a.get("r_death"), "r_alive": a.get("r_alive"),
         "agents": cfg.get("agents"), "bots": cfg.get("bots"), "arenas": cfg.get("arenas"),
         "league": cfg.get("league"), "league_every": cfg.get("league_every")}
    if a.get("npc_life"):
        m["npc_life"] = f"{int(a['npc_life'][0])},{int(a['npc_life'][1])}"
    elif "arena" in cfg:
        m["npc_life"] = ""
    if not a:  # v1 / v1.1 runs saved only the reward mode: the classic round world
        m.update(S.PRESETS["classic"]["values"])
        m["bots"], m["arenas"] = 4, 2
    m["reward_net"] = cfg.get("reward", "net") == "net"
    return S.validate(S.BRAIN_SCHEMA, {k: v for k, v in m.items() if v is not None})


def import_run_folder(src: Path, name: str, gens: list[int] | None = None, max_gens: int = 12) -> Path:
    """Bring in a training run folder made by train.py (runs/<run>/ with generations/ and progress.csv)."""
    src = Path(src)
    have = gen_files(src)
    if not have:
        raise ValueError("no generations/gen_XXXX.zip in that folder")
    cfg = S.read_json(src / "arena_config.json", {})
    values = settings_from_run_config(cfg)
    order = sorted(have)
    if gens is None:
        step = max(1, len(order) // max(1, max_gens - 1))
        gens = sorted(set(order[::step]) | {order[0], order[-1]})
    dst = brains_dir() / _new_id(clean_name(name))
    (dst / "generations").mkdir(parents=True)
    for g in gens:
        shutil.copy2(have[g], dst / "generations" / have[g].name)
    if (src / "league").exists():
        shutil.copytree(src / "league", dst / "league")
    _copy_progress(src, dst, max(gens))
    save_meta(dst, {"version": 1, "name": clean_name(name), "created": time.time(), "preset": "imported",
                    "parent": {"id": None, "name": src.name, "generation": max(gens)},
                    "settings": values, "notes": f"Imported from training run {src.name}"})
    return dst


def install_starters(starter_root: Path):
    """First run: copy the brains that ship with the app into the library (once each)."""
    marker = data_dir() / "starters.json"
    done = set(S.read_json(marker, []))
    if not starter_root.exists():
        return
    for p in sorted(starter_root.iterdir()):
        if not p.is_dir() or p.name in done or not (p / BRAIN_FILE).exists():
            continue
        meta = S.read_json(p / BRAIN_FILE, {})
        dst = brains_dir() / _new_id(clean_name(meta.get("name", p.name)))
        shutil.copytree(p, dst)
        meta["created"] = time.time()
        save_meta(dst, meta)
        done.add(p.name)
    S.write_json(marker, sorted(done))
