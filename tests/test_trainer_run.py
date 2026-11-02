"""End-to-end: a real (tiny) training run starts, saves generations, pauses cleanly, and resumes."""
import json, os, subprocess, sys, tempfile, time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
home = tempfile.mkdtemp(prefix="snek_")
os.environ["SNEKBRAIN_HOME"] = home
from snekbrain import brains
from snekbrain.server import bootstrap

TINY = {"half_size": 600, "view": 300, "agents": 4, "bots": 2, "league": 1, "food": 200, "net_size": 64,
        "n_steps": 64, "gen_every": 3000, "league_every": 1}

def job(bdir, workers):
    j = {"brain_dir": str(bdir), "workers": workers, "swap_seconds": 5, "autosave_minutes": 1, "keep_recent": 3,
         "keep_every": 4, "min_free_gb": 1, "live_fps": 5, "league_max": 2, "torch_threads": 1}
    (bdir / "run.json").write_text(json.dumps(j))
    return bdir / "run.json"

def run(bdir, workers, seconds):
    p = subprocess.Popen(bootstrap("snekbrain.trainer", ["--job", str(job(bdir, workers))]), cwd=str(bdir))
    time.sleep(seconds)
    t = time.time()
    (bdir / "STOP").write_text("stop")
    code = p.wait(120)
    return code, time.time() - t

def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg, flush=True)
    if not cond:
        print(open(bdir / "train.log").read()[-3000:])
        sys.exit(1)

bdir = brains.create("Test", "quick", TINY)
for workers in tuple(int(x) for x in os.environ.get("WORKERS", "2,1").split(",")):
    code, stop_s = run(bdir, workers, int(os.environ.get("SECS", "75")))
    st = json.loads((bdir / "status.json").read_text())
    gens = brains.gen_files(bdir)
    check(code == 0, f"[{workers} workers] trainer exited cleanly (code {code}), stop took {stop_s:.1f}s")
    check(st["state"] == "paused", f"[{workers} workers] status is paused ({st['state']})")
    check(0 in gens and max(gens) >= 2, f"[{workers} workers] generations saved: {sorted(gens)}")
    check((bdir / "latest.zip").exists() and (bdir / "web" / "live.json").exists(), f"[{workers} workers] latest.zip + live.json")
    check((bdir / "live_game.json").exists(), f"[{workers} workers] live game snapshot written")
    lg = json.loads((bdir / "live_game.json").read_text())
    check(len(lg["snakes"]) >= 4 and len(lg["food"]) > 0, f"[{workers} workers] snapshot has snakes {len(lg['snakes'])} + food")
    rows = brains.read_progress(bdir)
    check(len(rows) >= 2, f"[{workers} workers] progress rows {len(rows)}")
    check(not (bdir / "STOP").exists(), f"[{workers} workers] STOP file removed")
    lj = json.loads((bdir / "web" / "live.json").read_text())
    check(len(lj["layers"]) == 3 and lj["layers"][0]["in"] == 68, f"[{workers} workers] web brain json has 3 layers, 68 inputs")
    league = list((bdir / "league").glob("*.npz"))
    check(1 <= len(league) <= 2, f"[{workers} workers] league kept to cap: {len(league)}")
print("first run gens", sorted(gens))
steps = [int(r["timesteps"]) for r in brains.read_progress(bdir)]
check(steps == sorted(steps), "resume kept counting timesteps upward (no reset)")
print("ALL PASS", home)
