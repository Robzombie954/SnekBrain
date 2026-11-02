"""The learner: trains one brain with PPO (Stable-Baselines3), forever, until it's told to pause.

Started by the app as its own process:  python -m snekbrain.trainer --job <run.json>

What it writes into the brain's folder while it runs:
- generations/gen_XXXX.zip  a full checkpoint every `gen_every` steps (a "generation")
- progress.csv              one line per generation (the chart)
- web/live.json             the newest brain, every `swap_seconds` (the snake on screen picks it up)
- web/gen_XXXX.json         a small copy of each kept generation (for the before/after view)
- latest.zip                a safety save every few minutes and when it pauses
- live_game.json            a picture of one real training game, several times a second
- status.json               what it's doing right now
It stops cleanly (saving everything) when a file called STOP appears in the brain's folder.

Loading never unpickles anything from a brain file: only the weights (torch weights_only) and plain
JSON numbers are read, so a brain someone sends you can't run code on your PC.
"""
from __future__ import annotations

import argparse
import base64
import csv
import json
import os
import shutil
import sys
import time
import traceback
import zipfile
from pathlib import Path

import numpy as np


def arena_config(st: dict):
    from .arena import ArenaConfig
    from .settings import parse_npc_life
    cfg = ArenaConfig()
    cfg.wrap = bool(st["wrap"])
    cfg.half_size = float(st["half_size"])
    cfg.world_radius = float(st["world_radius"])
    cfg.view_radius = float(st["view"])
    cfg.bot_view_radius = float(st["bot_view"])
    cfg.food_count = int(st["food"])
    cfg.food_value = float(st["food_value"])
    cfg.npc_life = parse_npc_life(st["npc_life"])
    cfg.max_steps_per_life = int(st["max_steps_per_life"])
    cfg.r_food, cfg.r_kill = float(st["r_food"]), float(st["r_kill"])
    cfg.r_death, cfg.r_alive = float(st["r_death"]), float(st["r_alive"])
    cfg.reward_net_mass = bool(st["reward_net"])
    return cfg


def world_info(st: dict) -> dict:
    """What the on-screen game needs to play this brain the way it was trained."""
    return {"wrap": bool(st["wrap"]), "half_size": float(st["half_size"]), "world_radius": float(st["world_radius"]),
            "view_radius": float(st["view"]), "food_count": int(st["food"]), "food_value": float(st["food_value"]),
            "npc_life": st["npc_life"], "agents": int(st["agents"]), "bots": int(st["bots"]) + int(st["league"]),
            "bot_view": float(st["bot_view"]) or float(st["view"])}


CURRICULUM_MAX = 6


def curriculum_cfg(st: dict, stage: int) -> dict:
    """World settings for a difficulty level. Level 0 is exactly what the brain's settings say."""
    from .settings import parse_npc_life
    stage = max(0, min(CURRICULUM_MAX, int(stage)))
    base_bot = float(st["bot_view"]) or float(st["view"])
    life = parse_npc_life(st["npc_life"])
    out = {"food_count": max(20, int(round(int(st["food"]) * (1 - 0.08 * stage)))),
           "bot_view_radius": base_bot * (1 + 0.12 * stage)}
    if life:
        k = 1 + 0.3 * stage
        out["npc_life"] = (int(life[0] * k), int(life[1] * k))
    return out


def stage_target(stage: int) -> float:
    """Average best length over the last 3 generations needed to move up a level."""
    return 30.0 + 25.0 * stage


def make_model(env, st: dict, n_envs: int, device: str = "cpu"):
    from stable_baselines3 import PPO
    batch = int(st["batch_size"]) or max(256, 256 * n_envs // 4)
    batch = min(batch, int(st["n_steps"]) * n_envs)
    n = int(st["net_size"])
    return PPO("MlpPolicy", env, device=device, seed=int(st["seed"]), verbose=0,
               n_steps=int(st["n_steps"]), batch_size=batch, n_epochs=int(st["n_epochs"]),
               learning_rate=float(st["learning_rate"]), gamma=float(st["gamma"]),
               gae_lambda=float(st["gae_lambda"]), ent_coef=float(st["ent_coef"]),
               clip_range=float(st["clip_range"]), policy_kwargs=dict(net_arch=[n, n]))


def read_checkpoint(path: Path):
    """(policy weights, optimizer state or None, timesteps) from an SB3 .zip, without unpickling anything."""
    import io
    import torch
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        pol = torch.load(io.BytesIO(z.read("policy.pth")), map_location="cpu", weights_only=True)
        opt = None
        if "policy.optimizer.pth" in names:
            try:
                opt = torch.load(io.BytesIO(z.read("policy.optimizer.pth")), map_location="cpu", weights_only=True)
            except Exception:
                opt = None
        steps = 0
        try:
            steps = int(json.loads(z.read("data").decode("utf-8")).get("num_timesteps", 0))
        except Exception:
            pass
    return pol, opt, steps


def load_into(model, path: Path) -> int:
    pol, opt, steps = read_checkpoint(path)
    model.policy.load_state_dict(pol)
    if opt is not None:
        try:
            model.policy.optimizer.load_state_dict(opt)
        except Exception:
            pass  # a different brain size or optimizer: start the optimizer fresh
    model.num_timesteps = steps
    return steps


def _b64(t) -> str:
    return base64.b64encode(np.ascontiguousarray(t.detach().cpu().numpy(), dtype="<f4").tobytes()).decode()


def brain_json(state: dict, gen, info: dict) -> dict:
    """The policy network as small JSON for the on-screen game (same format the web game reads)."""
    names = [("mlp_extractor.policy_net.0", "tanh"), ("mlp_extractor.policy_net.2", "tanh"), ("action_net", "linear")]
    layers = []
    for n, act in names:
        w, b = state[n + ".weight"], state[n + ".bias"]
        layers.append({"in": int(w.shape[1]), "out": int(w.shape[0]), "act": act, "w": _b64(w), "b": _b64(b)})
    return {"generation": gen, "info": info, "layers": layers}


def export_checkpoint(path: Path, gen: int, info: dict) -> dict:
    pol, _, steps = read_checkpoint(path)
    return brain_json(pol, gen, dict(info, timesteps=steps))


def write_json(path: Path, data):
    tmp = path.with_name(path.name + ".part")
    with open(tmp, "w") as f:
        json.dump(data, f, separators=(",", ":"))
    for _ in range(20):  # Windows: the app may be reading the old copy this instant
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.05)


def free_gb(path: Path) -> float:
    return shutil.disk_usage(path).free / 1e9


class Learner:
    def __init__(self, job: dict):
        from . import brains
        from .settings import BRAIN_SCHEMA, validate
        self.job = job
        self.bdir = Path(job["brain_dir"])
        meta = json.loads((self.bdir / "brain.json").read_text(encoding="utf-8"))
        self.st = validate(BRAIN_SCHEMA, meta.get("settings", {}))
        self.name = meta.get("name", self.bdir.name)
        self.brains = brains
        (self.bdir / "generations").mkdir(exist_ok=True)
        (self.bdir / "web").mkdir(exist_ok=True)
        self.info = world_info(self.st)
        self.status = {"state": "starting", "brain": self.bdir.name, "pid": os.getpid()}
        self._write_status()

    def _write_status(self, **kw):
        self.status.update(kw, updated=time.time())
        try:
            write_json(self.bdir / "status.json", self.status)
        except Exception:
            pass

    def run(self):
        import torch
        from stable_baselines3.common.callbacks import BaseCallback
        from .arena import ArenaVecEnv, SubprocArenaVecEnv, save_league_brain, write_snapshot

        st, job, bdir = self.st, self.job, self.bdir
        if free_gb(bdir) < float(job["min_free_gb"]):
            raise SystemExit(f"Not starting: less than {job['min_free_gb']} GB free on the disk")
        torch.set_num_threads(max(1, min(4, int(job.get("torch_threads", 2)))))
        cfg = arena_config(st)
        self.stage = int(self._prog_state().get("stage", 0)) if st["curriculum"] else 0
        if self.stage:
            for k, v in curriculum_cfg(st, self.stage).items():
                setattr(cfg, k, v)
        league_dir = bdir / "league"
        if int(st["league"]):
            league_dir.mkdir(exist_ok=True)
        workers = max(1, int(job["workers"]))
        snap = str(bdir / "live_game.json") if float(job.get("live_fps", 0)) > 0 else None
        if workers == 1:
            env = ArenaVecEnv(int(st["arenas"]), int(st["agents"]), int(st["bots"]), cfg, seed=int(st["seed"]),
                                league_per_arena=int(st["league"]), league_dir=str(league_dir))
        else:
            env = SubprocArenaVecEnv(workers, int(st["arenas"]), int(st["agents"]), int(st["bots"]), cfg,
                                       seed=int(st["seed"]), league_per_arena=int(st["league"]),
                                       league_dir=str(league_dir), snap_path=snap,
                                       snap_fps=float(job.get("live_fps", 0)))
        self.env = env
        try:
            device = "cpu"
            if job.get("device") == "cuda":
                if torch.cuda.is_available():
                    device = "cuda"
                else:
                    self.brains.add_event(bdir, None, "GPU boost is on but the graphics card isn't usable, so it's using the CPU", "info")
            model = make_model(env, st, env.num_envs, device)
            self.base_ent = float(st["ent_coef"])
            ps = self._prog_state()
            self.boosting = bool(ps.get("boosting")) and bool(st["explore_boost"])
            if self.boosting:
                model.ent_coef = self._boost_ent()
            gens = self.brains.gen_files(bdir)
            start_gen = 0
            if gens:
                src = gens[max(gens)]
                latest = bdir / "latest.zip"
                if latest.exists() and latest.stat().st_mtime > src.stat().st_mtime:
                    src = latest
                load_into(model, src)
                start_gen = max(gens)
            else:
                model.save(str(bdir / "generations" / "gen_0000"))  # the untrained baby, for before/after
                write_json(bdir / "web" / "gen_0000.json",
                           brain_json(model.policy.state_dict(), 0, dict(self.info, timesteps=0)))
            learner = self
            self.cur_gen = float(start_gen)
            snap_here = workers == 1 and float(job.get("live_fps", 0)) > 0
            snap_gap = 1.0 / max(0.1, float(job.get("live_fps", 1)))

            class CB(BaseCallback):
                def __init__(self):
                    super().__init__()
                    self.gen = start_gen
                    self.buf = []
                    self.calls = 0

                def _on_training_start(self):
                    now = time.time()
                    self.next_at = (self.num_timesteps // int(st["gen_every"]) + 1) * int(st["gen_every"])
                    self.t_swap = 0.0
                    self.t_save = now
                    self.t_status = 0.0
                    self.t_gen = now
                    self.s_gen = self.num_timesteps
                    self.sps_t, self.sps_s, self.sps = now, self.num_timesteps, 0.0
                    self.t_snap = 0.0

                def _on_step(self) -> bool:
                    for inf in self.locals.get("infos", []):
                        if "snake" in inf:
                            self.buf.append((inf["episode"]["r"], inf["snake"]["max_len"],
                                             inf["snake"]["steps"], inf["snake"]["kills"]))
                    now = time.time()
                    if now - self.sps_t >= 5:
                        self.sps = (self.num_timesteps - self.sps_s) / (now - self.sps_t)
                        self.sps_t, self.sps_s = now, self.num_timesteps
                    if self.num_timesteps >= self.next_at:
                        self.next_at += int(st["gen_every"])
                        self.gen += 1
                        learner._generation(self.model, self.gen, self.buf, self.num_timesteps,
                                            (now - self.t_gen) / 60, (self.num_timesteps - self.s_gen) / max(1e-6, now - self.t_gen),
                                            save_league_brain)
                        self.buf, self.t_gen, self.s_gen = [], now, self.num_timesteps
                        if free_gb(bdir) < float(job["min_free_gb"]):
                            learner._write_status(state="stopped", reason="low disk space")
                            return False
                    if snap_here and now - self.t_snap >= snap_gap:  # one process: draw its own game
                        self.t_snap = now
                        write_snapshot(env.arenas[0], str(bdir / "live_game.json"), {"t": round(now, 2)})
                    if now - self.t_swap >= float(job["swap_seconds"]):
                        self.t_swap = now
                        frac = self.gen + min(0.99, max(0.0, 1 - (self.next_at - self.num_timesteps) / int(st["gen_every"])))
                        learner.cur_gen = round(frac, 2)
                        write_json(bdir / "web" / "live.json",
                                   brain_json(self.model.policy.state_dict(), round(frac, 2),
                                              dict(learner.info, timesteps=int(self.num_timesteps), updated=now)))
                    if now - self.t_save >= 60 * float(job["autosave_minutes"]):
                        self.t_save = now
                        self.model.save(str(bdir / "latest"))
                    if now - self.t_status >= 2:
                        self.t_status = now
                        learner._write_status(state="training", generation=self.gen, steps=int(self.num_timesteps),
                                              sps=int(self.sps), next_gen_at=int(self.next_at),
                                              stage=learner.stage, exploring=learner.boosting,
                                              device=str(self.model.device))
                    self.calls += 1
                    if self.calls % 20 == 0 and (bdir / "STOP").exists():
                        return False
                    return True

            self._write_status(state="training", generation=start_gen)
            model.learn(total_timesteps=10 ** 13, callback=CB(), reset_num_timesteps=False)
        finally:
            try:
                model.save(str(bdir / "latest"))
                write_json(bdir / "web" / "live.json",
                           brain_json(model.policy.state_dict(), getattr(self, "cur_gen", None),
                                      dict(self.info, timesteps=int(model.num_timesteps), updated=time.time())))
            except Exception:
                pass
            env.close()
            try:
                (bdir / "STOP").unlink()
            except FileNotFoundError:
                pass
        if self.status.get("state") != "stopped":
            self._write_status(state="paused")

    # ------------------------------------------------ progress state (curriculum level, records)
    def _prog_state(self) -> dict:
        from .settings import read_json
        return read_json(self.bdir / "progress_state.json", {})

    def _save_prog_state(self, d: dict):
        from .settings import write_json as wj
        wj(self.bdir / "progress_state.json", d)

    def _boost_ent(self) -> float:
        return min(0.05, max(0.02, self.base_ent * 3))

    def _after_generation(self, model, gen: int, row: dict):
        """Records (Hall of Fame), the auto-curriculum and the exploration boost. Never stops training."""
        st, bdir = self.st, self.bdir
        ps = self._prog_state()
        best_len = float(ps.get("best_len", 0))
        best_ret = float(ps.get("best_return", -1e9))
        since = int(ps.get("since_improve", 0))
        improved = False
        if row["lives"] >= 3 and row["avg_max_len"] > best_len * 1.05 + 1:
            if best_len > 0:
                self.brains.add_hall(bdir, {"generation": gen, "kind": "length", "value": row["avg_max_len"],
                                            "t": time.time(), "label": f"New record: average length {row['avg_max_len']:.0f}"})
                self.brains.add_event(bdir, gen, f"New record! Average length {row['avg_max_len']:.0f}", "record")
            best_len, improved = row["avg_max_len"], True
        if row["lives"] >= 3 and row["avg_return"] > best_ret + 0.05 * max(1.0, abs(best_ret)):
            best_ret, improved = row["avg_return"], True
        if row["best_max_len"] > float(ps.get("longest_ever", 0)) * 1.1:
            if float(ps.get("longest_ever", 0)) > 0:
                self.brains.add_hall(bdir, {"generation": gen, "kind": "longest", "value": row["best_max_len"],
                                            "t": time.time(), "label": f"Longest snake ever: {row['best_max_len']}"})
            ps["longest_ever"] = row["best_max_len"]
        since = 0 if improved else since + 1
        # exploration boost: stuck for 8 generations -> try more new moves until it improves again
        if st["explore_boost"]:
            if since >= 8 and not self.boosting:
                self.boosting = True
                model.ent_coef = self._boost_ent()
                self.brains.add_event(bdir, gen, "Stuck for a while, so it's trying more new moves", "explore")
            elif improved and self.boosting:
                self.boosting = False
                model.ent_coef = self.base_ent
                self.brains.add_event(bdir, gen, "Improving again, back to normal curiosity", "explore")
        elif self.boosting:
            self.boosting, model.ent_coef = False, self.base_ent
        # curriculum: mastered this level -> make the world harder
        hist = ps.get("recent_len", [])[-2:] + [row["avg_max_len"]]
        ps["recent_len"] = hist
        if st["curriculum"] and self.stage < CURRICULUM_MAX and len(hist) >= 3 \
                and min(hist) >= stage_target(self.stage):
            self.stage += 1
            ps["recent_len"] = []
            try:
                self.env.set_cfg(**curriculum_cfg(st, self.stage))
                self.brains.add_event(bdir, gen, f"Level up! The world is now level {self.stage + 1}: "
                                                 f"less food and tougher NPCs", "level")
            except Exception as e:  # noqa: BLE001
                print("curriculum change failed:", e, flush=True)
        ps.update(best_len=best_len, best_return=best_ret, since_improve=since,
                  stage=self.stage, boosting=self.boosting)
        self._save_prog_state(ps)

    def _generation(self, model, gen, buf, steps, minutes, sps, save_league_brain):
        bdir, st, job = self.bdir, self.st, self.job
        model.save(str(bdir / "generations" / f"gen_{gen:04d}"))
        state = model.policy.state_dict()
        write_json(bdir / "web" / f"gen_{gen:04d}.json", brain_json(state, gen, dict(self.info, timesteps=int(steps))))
        if int(st["league"]) and gen % int(st["league_every"]) == 0:
            save_league_brain(state, bdir / "league" / f"{bdir.name}_gen_{gen:04d}.npz", float(st["view"]))
            self.brains.prune_league(bdir, int(job.get("league_max", 30)))
        b = np.array(buf) if buf else np.zeros((1, 4))
        path = bdir / "progress.csv"
        new = not path.exists()
        with open(path, "a", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["generation", "timesteps", "minutes", "lives", "avg_return", "avg_max_len", "best_max_len",
                            "avg_survival_steps", "kills_per_life", "steps_per_sec"])
            row = [gen, int(steps), round(minutes, 2), len(buf), round(float(b[:, 0].mean()), 3),
                   round(float(b[:, 1].mean()), 1), int(b[:, 1].max()), round(float(b[:, 2].mean()), 1),
                   round(float(b[:, 3].mean()), 3), int(sps)]
            w.writerow(row)
        try:
            self._after_generation(model, gen, {"lives": len(buf), "avg_return": row[4], "avg_max_len": row[5],
                                                "best_max_len": row[6]})
        except Exception:  # noqa: BLE001 - records are a bonus, training goes on
            traceback.print_exc()
        self.brains.prune(bdir, int(job["keep_recent"]), int(job["keep_every"]))


def main():
    ap = argparse.ArgumentParser(description="SnekBrain learner (started by the app)")
    ap.add_argument("--job", required=True, help="run.json written by the app")
    a = ap.parse_args()
    job = json.loads(Path(a.job).read_text(encoding="utf-8"))
    bdir = Path(job["brain_dir"])
    if job.get("device") == "cuda" and job.get("gpu_path"):
        sys.path.insert(0, str(job["gpu_path"]))  # the GPU add-on's PyTorch wins over the built-in CPU one
    log = open(bdir / "train.log", "a", buffering=1, encoding="utf-8")
    sys.stdout = sys.stderr = log
    print(f"--- {time.strftime('%Y-%m-%d %H:%M:%S')} start, {job['workers']} game processes", flush=True)
    L = None
    try:
        L = Learner(job)
        L.run()
        print("paused cleanly", flush=True)
    except SystemExit as e:
        msg = str(e)
        print(msg, flush=True)
        if L:
            L._write_status(state="error", error=msg)
        sys.exit(2)
    except BaseException as e:
        traceback.print_exc()
        if L:
            L._write_status(state="error", error=f"{type(e).__name__}: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
