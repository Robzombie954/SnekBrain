"""Every setting SnekBrain has, with its default, its limits and a plain-words explanation.

Two kinds:
- APP settings (one file for the whole app): how hard the PC works, videos, start with Windows...
- BRAIN settings (one per brain): the world it trains in and how it learns.
  Settings marked new_only shape the brain itself, so they can only be picked when a brain is made.
  Everything else can be changed any time and takes effect the next time training starts
  (the app restarts training for you when you save).

The UI builds its settings screen from SCHEMA, so a new setting only needs adding here.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path

# key: (type, default, min, max, label, help)
APP_SCHEMA = {
    "mode": ("choice:normal,turbo", "normal", None, None, "Training speed",
             "Normal stays in the background. Turbo uses almost the whole CPU to learn faster. The game is shown either way."),
    "cpu_cap_normal": ("int", 80, 10, 100, "CPU limit in Normal (%)",
                       "Hard limit on how much of the whole CPU training may use in Normal mode."),
    "cpu_cap_turbo": ("int", 90, 10, 100, "CPU limit in Turbo (%)",
                      "Hard limit on how much of the whole CPU training may use in Turbo mode."),
    "workers_normal": ("int", 0, 0, 256, "Game processes in Normal (0 = auto)",
                       "How many training games run side by side. Auto = CPU cores minus 3."),
    "workers_turbo": ("int", 0, 0, 256, "Game processes in Turbo (0 = auto)",
                      "Auto = CPU cores minus 1."),
    "low_priority": ("bool", True, None, None, "Run training at low priority",
                     "Your other programs always go first. Recommended."),
    "when_video": ("choice:pause,slow,ignore", "pause", None, None, "When a video is playing",
                   "pause = training waits until the video stops (the game keeps playing on screen); "
                   "slow = training drops to the lowest priority; ignore = keep going."),
    "video_apps": ("text", "brave,chrome,msedge,firefox,opera,vivaldi,vlc,mpc-hc,mpc-be,potplayer,mpv,"
                   "netflix,primevideo,disney,plex,kodi,hulu,youtube,movies,video,films,wmplayer,iina",
                   None, None, "Apps that count as video",
                   "A media session from one of these (comma separated, part of the name is enough) counts as a video. "
                   "Music apps are left out on purpose."),
    "pause_on_fullscreen": ("bool", True, None, None, "Pause for any full-screen app",
                            "Also treat a full-screen window in front (a movie or a game) as a video."),
    "start_with_windows": ("bool", False, None, None, "Keep learning after a restart",
                           "Starts quietly when you sign in to Windows, and carries on training only if it was training before."),
    "swap_seconds": ("int", 30, 5, 3600, "Brain upgrade every (seconds)",
                     "How often the snake on screen gets the newest brain from training."),
    "autosave_minutes": ("int", 5, 1, 120, "Safety save every (minutes)",
                         "Training also saves a full copy this often, so a crash or power cut loses very little."),
    "keep_recent": ("int", 20, 2, 100000, "Keep the newest N generations",
                    "Older generations are thinned out to save disk space."),
    "keep_every": ("int", 10, 1, 100000, "...and every Nth generation forever", ""),
    "min_free_gb": ("float", 10.0, 1.0, 10000.0, "Stop if the disk has less than (GB) free", ""),
    "live_fps": ("int", 8, 0, 30, "Inside-training view updates per second", "0 turns the inside-training view off."),
    "train_when_closed": ("bool", True, None, None, "Keep training when the window is closed",
                          "Off = training pauses about half a minute after you close the window."),
    "league_max": ("int", 30, 1, 1000, "Most old champions kept per brain", ""),
    "check_updates": ("bool", True, None, None, "Check for new versions",
                      "Once a day it asks GitHub if there's a newer SnekBrain. Nothing is downloaded or installed unless you say so. "
                      "Nothing about you or your PC is sent."),
    "gpu": ("bool", False, None, None, "GPU boost (NVIDIA)",
            "Trains the brain on an NVIDIA graphics card. Needs the free GPU add-on (a big one-time download). "
            "The game itself always runs on the CPU, so the gain depends on your PC."),
    "ai_hook": ("bool", False, None, None, "Let my own AI assistant connect",
                "Lets an AI assistant on this PC (for example Claude Desktop) read the stats and start or pause training. "
                "Only programs on this PC can connect, with a key only you can see. Off by default."),
    "intro_seen": ("bool", False, None, None, "Intro already shown", ""),
}

# The world and the learning. Defaults are the "Quick learner" preset.
BRAIN_SCHEMA = {
    # world (new_only)
    "wrap": ("bool", True, None, None, "No edge (leave one side, come back on the other)", "", True),
    "half_size": ("float", 1500.0, 300.0, 20000.0, "World size (half the side, no-edge world)", "", True),
    "world_radius": ("float", 1400.0, 300.0, 20000.0, "World radius (round world with an edge)", "", True),
    "view": ("float", 420.0, 100.0, 3000.0, "How far the snake sees", "", True),
    "net_size": ("int", 256, 16, 2048, "Brain size (neurons per layer)", "Two hidden layers of this many neurons.", True),
    "agents": ("int", 8, 1, 200, "AI snakes per game", "They all share one brain and learn together (self-play).", True),
    "league": ("int", 0, 0, 100, "Old-champion seats per game",
               "Frozen copies of earlier generations play too, so it can't forget how to beat them.", True),
    "bots": ("int", 6, 0, 200, "Simple NPC snakes per game", "Scripted snakes that never learn.", True),
    "arenas": ("int", 1, 1, 32, "Games per process", "", True),
    # world (changeable)
    "food": ("int", 900, 0, 50000, "Food on the map", "", False),
    "food_value": ("float", 2.0, 0.1, 50.0, "Food value", "", False),
    "bot_view": ("float", 420.0, 0.0, 3000.0, "How far NPCs see (0 = same as the AI)", "", False),
    "npc_life": ("text", "1800,7200", None, None, "NPC old age (min,max ticks, blank = never)",
                 "NPCs die of old age so they don't grow forever.", False),
    "max_steps_per_life": ("int", 4000, 100, 1000000, "Longest life (ticks)", "", False),
    "curriculum": ("bool", True, None, None, "World gets harder as it improves",
                   "Less food, sharper and older NPCs each time it masters the current level.", False),
    "explore_boost": ("bool", True, None, None, "Shake it up when it stops improving",
                      "If it hasn't beaten its best for a while, it tries more new moves until it does.", False),
    # reward: what the snake is paid for. Nobody tells it how to play; this is all it gets.
    "r_food": ("float", 0.1, 0.0, 100.0, "Reward per bit of growth", "", False),
    "r_kill": ("float", 2.0, -100.0, 100.0, "Reward for a kill", "", False),
    "r_death": ("float", -5.0, -1000.0, 0.0, "Penalty for dying", "", False),
    "r_alive": ("float", 0.0005, -1.0, 1.0, "Reward per tick alive", "", False),
    "reward_net": ("bool", True, None, None, "Boosting costs reward",
                   "Length burned by boosting counts against it (fixed the v1 'boost forever' habit).", False),
    # learning (PPO)
    "learning_rate": ("float", 0.0003, 1e-7, 0.1, "Learning rate", "How big each brain update is.", False),
    "n_steps": ("int", 256, 16, 100000, "Steps per update (per snake)", "", False),
    "batch_size": ("int", 0, 0, 10000000, "Batch size (0 = auto)", "", False),
    "n_epochs": ("int", 4, 1, 100, "Passes over each batch", "", False),
    "gamma": ("float", 0.995, 0.5, 0.99999, "Future reward weight (gamma)", "", False),
    "gae_lambda": ("float", 0.95, 0.5, 1.0, "GAE lambda", "", False),
    "ent_coef": ("float", 0.01, 0.0, 1.0, "Curiosity (entropy bonus)", "Higher = tries more new moves.", False),
    "clip_range": ("float", 0.2, 0.01, 1.0, "PPO clip range", "", False),
    "gen_every": ("int", 250000, 1000, 100000000, "Steps per generation", "A generation is saved this often.", False),
    "league_every": ("int", 5, 1, 100000, "A generation joins the old champions every N", "", False),
    "seed": ("int", 1, 0, 2**31 - 1, "Random seed", "", False),
}

PRESETS = {
    "quick": {"label": "Quick learner",
              "help": "Small no-edge world, 8 AI snakes per game. Visibly learns in minutes on most PCs.",
              "values": {}},
    "big": {"label": "Big world (like v1.2)",
            "help": "Huge no-edge world, 16 AI snakes, 8 old champions and 16 NPCs per game, long vision. Learns slower but goes further.",
            "values": {"half_size": 3000.0, "view": 700.0, "agents": 16, "league": 8, "bots": 16, "food": 3600,
                       "food_value": 2.0, "bot_view": 420.0, "gen_every": 1000000}},
    "classic": {"label": "Classic round world (like v1.1)",
                "help": "Round world with a deadly edge, 6 AI snakes and 4 NPCs, 2 games per process.",
                "values": {"wrap": False, "agents": 6, "bots": 4, "arenas": 2, "food": 600, "food_value": 1.0,
                           "bot_view": 0.0, "npc_life": "", "gen_every": 1000000}},
}


def defaults(schema: dict) -> dict:
    return {k: copy.deepcopy(v[1]) for k, v in schema.items()}


def _coerce(key: str, spec: tuple, value):
    kind, default, lo, hi = spec[0], spec[1], spec[2], spec[3]
    if kind == "bool":
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)
    if kind in ("int", "float"):
        try:
            v = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"{spec[4]}: '{value}' is not a number")
        if v != v:  # NaN
            raise ValueError(f"{spec[4]}: not a number")
        if lo is not None and v < lo:
            raise ValueError(f"{spec[4]}: at least {lo}")
        if hi is not None and v > hi:
            raise ValueError(f"{spec[4]}: at most {hi}")
        return int(round(v)) if kind == "int" else v
    if kind.startswith("choice:"):
        options = kind.split(":", 1)[1].split(",")
        if str(value) not in options:
            raise ValueError(f"{spec[4]}: pick one of {', '.join(options)}")
        return str(value)
    v = str(value)
    if len(v) > 2000:
        raise ValueError(f"{spec[4]}: too long")
    if key == "npc_life":
        parse_npc_life(v)  # raises when it's not "min,max"
    return v


def validate(schema: dict, values: dict, base: dict | None = None, allow_new_only: bool = True) -> dict:
    """Merge values into base (or the defaults), checking every value. Unknown keys are refused."""
    out = copy.deepcopy(base) if base is not None else defaults(schema)
    for k, v in (values or {}).items():
        if k not in schema:
            raise ValueError(f"unknown setting '{k}'")
        spec = schema[k]
        nv = _coerce(k, spec, v)
        if not allow_new_only and len(spec) > 6 and spec[6] and nv != out.get(k):
            raise ValueError(f"{spec[4]} can only be picked when a brain is made")
        out[k] = nv
    for k in schema:  # files from older versions: fill in new settings
        out.setdefault(k, copy.deepcopy(schema[k][1]))
    return out


def parse_npc_life(text: str):
    text = (text or "").strip()
    if not text:
        return None
    parts = [p.strip() for p in text.split(",")]
    if len(parts) != 2:
        raise ValueError("NPC old age: write it as min,max (for example 1800,7200) or leave it blank")
    lo, hi = int(parts[0]), int(parts[1])
    if lo < 1 or hi <= lo:
        raise ValueError("NPC old age: max must be bigger than min, both above 0")
    return (lo, hi)


def preset_values(name: str) -> dict:
    if name not in PRESETS:
        raise ValueError(f"unknown preset '{name}'")
    return validate(BRAIN_SCHEMA, PRESETS[name]["values"])


def schema_for_ui() -> dict:
    def conv(schema, brain):
        rows = []
        for k, s in schema.items():
            rows.append({"key": k, "type": s[0], "default": s[1], "min": s[2], "max": s[3], "label": s[4],
                         "help": s[5], "new_only": bool(brain and len(s) > 6 and s[6])})
        return rows
    return {"app": conv(APP_SCHEMA, False), "brain": conv(BRAIN_SCHEMA, True),
            "presets": {k: {"label": v["label"], "help": v["help"]} for k, v in PRESETS.items()}}


def read_json(path: Path, fallback):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return fallback


def write_json(path: Path, data):
    """Atomic write: never leaves a half file behind."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.part")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def load_app_settings(path: Path) -> dict:
    raw = read_json(path, {})
    try:
        return validate(APP_SCHEMA, {k: v for k, v in raw.items() if k in APP_SCHEMA})
    except ValueError:
        return defaults(APP_SCHEMA)
