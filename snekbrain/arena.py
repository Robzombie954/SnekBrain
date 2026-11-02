"""SnekBrain game world: a snake arena for training game-playing AI.

A small, fast, original re-creation of the core rules of "snake .io" games:
- a round world with a deadly edge
- snakes steer with a limited turn rate and can boost (faster, but it burns length)
- food pellets make you longer
- your head touching another snake's body kills you, and your body turns into food
- your own body never kills you

Everything is numpy. One process can run many arenas at once.
ArenaVecEnv exposes every snake in every arena as one "env slot" for
Stable-Baselines3, so a single policy controls all snakes (self-play with
parameter sharing). Optional scripted bots add easy opponents.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

try:  # SB3 is only needed for training; the arena itself runs without it.
    from stable_baselines3.common.vec_env.base_vec_env import VecEnv
    from gymnasium import spaces
except Exception:  # pragma: no cover
    VecEnv = object
    spaces = None


# ---------------------------------------------------------------- settings
@dataclass
class ArenaConfig:
    world_radius: float = 1400.0
    food_count: int = 600            # pellets kept on the map
    food_value: float = 1.0
    seg_spacing: float = 6.0         # distance moved per normal step
    start_len: int = 12              # segments
    mass_per_seg: float = 3.0        # food mass needed for one extra segment
    max_turn: float = 0.16           # radians per step for a small snake
    boost_cost: float = 0.6          # mass burned per boost step
    min_boost_len: int = 14
    death_food_frac: float = 0.6     # share of a dead snake's mass dropped as food
    view_radius: float = 420.0
    sectors: int = 16
    max_steps_per_life: int = 4000
    # rewards
    r_food: float = 0.1              # per unit of mass gained
    r_death: float = -5.0
    r_kill: float = 2.0
    r_alive: float = 0.0005
    # v1.1: count mass LOST (boosting) as negative food reward. In v1 only gains
    # counted, so boosting was free and the AI burned its length chasing kills.
    reward_net_mass: bool = False
    # v1.2: square world with no edge (leave one side, come back on the other), side 2*half_size
    wrap: bool = False
    half_size: float = 3000.0
    # how far scripted NPCs see (0 = same as view_radius); NPCs stay at the standard 420
    bot_view_radius: float = 0.0
    # [min, max] ticks before an NPC (or league) snake dies of old age, so they don't grow forever; None = never
    npc_life: tuple | None = None


ACTIONS = [(-1, 0), (0, 0), (1, 0), (-1, 1), (0, 1), (1, 1)]  # (turn, boost)
N_ACTIONS = len(ACTIONS)


def obs_size(cfg: ArenaConfig) -> int:
    return cfg.sectors * 4 + 4


@dataclass
class Snake:
    body: np.ndarray                 # (n, 2) points, head first
    heading: float
    mass: float
    alive: bool = True
    steps: int = 0
    kills: int = 0
    ep_return: float = 0.0
    max_len: int = 0
    is_bot: bool = False
    view: float = 0.0                # how far this snake sees
    life: int = 0                    # old-age limit in ticks (0 = none)
    net: int = -1                    # league opponents: which frozen brain drives it

    @property
    def head(self) -> np.ndarray:
        return self.body[0]

    @property
    def length(self) -> int:
        return len(self.body)

    def radius(self) -> float:
        return 5.0 + min(14.0, 0.03 * len(self.body))


class Arena:
    """One world with several snakes."""

    def __init__(self, cfg: ArenaConfig, n_agents: int, n_bots: int, rng: np.random.Generator,
                 n_league: int = 0, league=None):
        self.cfg = cfg
        self.rng = rng
        self.n_agents = n_agents
        self.n_bots = n_bots
        self.n_league = n_league if league is not None else 0
        self.league = league           # LeaguePool: frozen older brains that play as opponents
        self.wrap = bool(cfg.wrap)
        self.L = float(cfg.half_size)
        self.S = 2.0 * self.L
        self.snakes: list[Snake] = []
        self.food_pos = np.zeros((0, 2), np.float32)
        self.food_val = np.zeros((0,), np.float32)
        self._pts = None
        self._version = 0
        self._obs_cache = None
        self.reset()

    # ------------------------------------------------------------ helpers
    def _wd(self, rel: np.ndarray) -> np.ndarray:
        """Shortest offset on the wrapped map (no-op on the round map)."""
        if self.wrap:
            rel = rel - self.S * np.round(rel / self.S)
        return rel

    def _wp(self, p: np.ndarray) -> np.ndarray:
        """Position back into [-L, L)."""
        if self.wrap:
            p = p - self.S * np.floor((p + self.L) / self.S)
        return p

    def _random_point(self, margin: float = 150.0) -> np.ndarray:
        if self.wrap:
            return ((self.rng.random(2) * 2 - 1) * self.L).astype(np.float32)
        r = (self.cfg.world_radius - margin) * math.sqrt(self.rng.random())
        a = self.rng.random() * 2 * math.pi
        return np.array([r * math.cos(a), r * math.sin(a)], np.float32)

    def _new_snake(self, is_bot: bool, league: bool = False) -> Snake:
        c = self.cfg
        head = self._random_point(300.0)
        heading = self.rng.random() * 2 * math.pi
        back = -np.array([math.cos(heading), math.sin(heading)], np.float32)
        body = head[None, :] + back[None, :] * (np.arange(c.start_len, dtype=np.float32)[:, None] * c.seg_spacing)
        s = Snake(body=self._wp(body).astype(np.float32), heading=heading,
                  mass=c.start_len * c.mass_per_seg, is_bot=is_bot, max_len=c.start_len)
        s.view = float(c.bot_view_radius or c.view_radius) if is_bot else float(c.view_radius)
        if (is_bot or league) and c.npc_life:
            lo, hi = c.npc_life
            s.life = int(lo + math.floor(self.rng.random() * (hi - lo)))
        if league:
            s.net, s.view = self.league.pick(self.rng)
        return s

    def _kind(self, i: int) -> str:
        if i < self.n_agents:
            return "agent"
        return "league" if i < self.n_agents + self.n_league else "bot"

    def _spawn_food(self, n: int):
        if n <= 0:
            return
        if self.wrap:
            pos = ((self.rng.random((n, 2)) * 2 - 1) * self.L).astype(np.float32)
        else:
            r = self.cfg.world_radius * np.sqrt(self.rng.random(n))
            a = self.rng.random(n) * 2 * np.pi
            pos = np.stack([r * np.cos(a), r * np.sin(a)], 1).astype(np.float32)
        val = (self.cfg.food_value * (0.5 + self.rng.random(n))).astype(np.float32)
        self.food_pos = np.concatenate([self.food_pos, pos])
        self.food_val = np.concatenate([self.food_val, val])

    def _drop_food(self, points: np.ndarray, total: float):
        if len(points) == 0 or total <= 0:
            return
        idx = np.arange(0, len(points), 2)
        pts = self._wp(points[idx] + self.rng.normal(0, 4, (len(idx), 2)).astype(np.float32)).astype(np.float32)
        val = np.full(len(idx), total / len(idx), np.float32)
        self.food_pos = np.concatenate([self.food_pos, pts])
        self.food_val = np.concatenate([self.food_val, val])

    def reset(self):
        self.snakes = [self._new_snake(False) for _ in range(self.n_agents)] + \
                      [self._new_snake(False, league=True) for _ in range(self.n_league)] + \
                      [self._new_snake(True) for _ in range(self.n_bots)]
        self.food_pos = np.zeros((0, 2), np.float32)
        self.food_val = np.zeros((0,), np.float32)
        self._spawn_food(self.cfg.food_count)

    def _points(self):
        """All living snakes' body points in one array (cached until the next change)."""
        if self._pts is None:
            alive = [(j, o) for j, o in enumerate(self.snakes) if o.alive]
            if alive:
                pts = np.concatenate([o.body for _, o in alive])
                owner = np.concatenate([np.full(o.length, j) for j, o in alive])
                rad = np.concatenate([np.full(o.length, o.radius(), np.float32) for _, o in alive])
                is_head = np.zeros(len(pts), bool)
                is_head[np.cumsum([0] + [o.length for _, o in alive[:-1]])] = True
            else:
                pts = np.zeros((0, 2), np.float32); owner = np.zeros(0, int)
                rad = np.zeros(0, np.float32); is_head = np.zeros(0, bool)
            lens = np.array([o.length for o in self.snakes])
            self._pts = (pts, owner, rad, is_head, lens)
        return self._pts

    # ------------------------------------------------------------ observation
    def observe(self, i: int) -> np.ndarray:
        """Ego-centric sector view: what is around the head, rotated so 'ahead' is sector 0."""
        if self.wrap:
            return self.observe_all()[i].copy()
        c = self.cfg
        s = self.snakes[i]
        K = c.sectors
        out = np.zeros((4, K), np.float32)
        h = s.head
        cos_h, sin_h = math.cos(-s.heading), math.sin(-s.heading)

        def sector_of(rel):
            x = rel[:, 0] * cos_h - rel[:, 1] * sin_h
            y = rel[:, 0] * sin_h + rel[:, 1] * cos_h
            ang = np.arctan2(y, x)
            return ((ang + np.pi / K) % (2 * np.pi) / (2 * np.pi) * K).astype(np.int64) % K

        # food: sum of value weighted by closeness
        rel = self.food_pos - h
        d = np.hypot(rel[:, 0], rel[:, 1])
        m = d < c.view_radius
        if m.any():
            sec = sector_of(rel[m])
            w = self.food_val[m] * (1.0 - d[m] / c.view_radius)
            np.add.at(out[0], sec, w)
        out[0] = np.tanh(out[0] / 4.0)

        # other snakes' bodies (danger) and heads, all snakes in one vector op
        pts, owner, rad, is_head, lens = self._points()
        rel = pts - h
        d = np.hypot(rel[:, 0], rel[:, 1]) - rad
        m = (owner != i) & (d < c.view_radius)
        if m.any():
            sec = sector_of(rel[m])
            close = 1.0 - np.clip(d[m], 0, None) / c.view_radius
            np.maximum.at(out[1], sec, close)
            hm = m & is_head
            if hm.any():
                hsec = sector_of(rel[hm])
                for hs, hd, ol in zip(hsec, d[hm], lens[owner[hm]]):
                    size = math.tanh((ol - s.length) / 50.0)
                    val = 1.0 - max(float(hd), 0.0) / c.view_radius
                    if val > abs(out[2, hs]):
                        out[2, hs] = val * (1.0 if size >= 0 else -1.0) * (0.5 + 0.5 * abs(size))

        # wall: closeness of the edge along each sector direction
        angs = s.heading + np.arange(K) * 2 * np.pi / K
        dirs = np.stack([np.cos(angs), np.sin(angs)], 1)
        b = dirs @ h
        cc = float(h @ h) - c.world_radius ** 2
        t = -b + np.sqrt(np.maximum(b * b - cc, 0.0))  # distance to the circle along dir
        out[3] = 1.0 - np.clip(t / c.view_radius, 0, 1)

        own = np.array([
            math.tanh(s.length / 200.0),
            1.0 if s.length > c.min_boost_len else 0.0,
            float(np.hypot(*h) / c.world_radius),
            math.tanh(s.steps / 2000.0),
        ], np.float32)
        return np.concatenate([out.reshape(-1), own])

    def observe_all(self) -> np.ndarray:
        """Same as observe() for every snake at once (vectorized, much faster)."""
        if self._obs_cache is not None and self._obs_cache[0] == self._version:
            return self._obs_cache[1]
        res = self._observe_all()
        self._obs_cache = (self._version, res)
        return res

    def _observe_all(self) -> np.ndarray:
        c = self.cfg
        K = c.sectors
        n = len(self.snakes)
        V = np.array([s.view or c.view_radius for s in self.snakes], np.float32)
        Vc = V[:, None]
        out = np.zeros((n, 4, K), np.float32)
        H = np.stack([s.head for s in self.snakes]).astype(np.float32)
        hd = np.array([s.heading for s in self.snakes], np.float32)
        alive = np.array([s.alive for s in self.snakes])
        two_pi = 2 * np.pi

        def sectors(si, rx, ry):
            ang = np.arctan2(ry, rx) - hd[si]
            return (((ang + np.pi / K) % two_pi) / two_pi * K).astype(np.int64) % K

        # food
        if len(self.food_pos):
            rel = self._wd(self.food_pos[None, :, :] - H[:, None, :])
            d = np.hypot(rel[..., 0], rel[..., 1])
            si, fi = np.nonzero((d < Vc) & alive[:, None])
            if len(si):
                sec = sectors(si, rel[si, fi, 0], rel[si, fi, 1])
                np.add.at(out[:, 0], (si, sec), self.food_val[fi] * (1.0 - d[si, fi] / V[si]))
        out[:, 0] = np.tanh(out[:, 0] / 4.0)

        # other snakes
        pts, owner, rad, is_head, lens = self._points()
        if len(pts):
            rel = self._wd(pts[None, :, :] - H[:, None, :])
            d = np.hypot(rel[..., 0], rel[..., 1]) - rad[None, :]
            m = (owner[None, :] != np.arange(n)[:, None]) & (d < Vc) & alive[:, None]
            si, pi = np.nonzero(m)
            if len(si):
                sec = sectors(si, rel[si, pi, 0], rel[si, pi, 1])
                close = 1.0 - np.clip(d[si, pi], 0, None) / V[si]
                np.maximum.at(out[:, 1], (si, sec), close)
                hsel = is_head[pi]
                for a, b, hs, cl in zip(si[hsel], pi[hsel], sec[hsel], close[hsel]):
                    size = math.tanh((lens[owner[b]] - lens[a]) / 50.0)
                    if cl > abs(out[a, 2, hs]):
                        out[a, 2, hs] = cl * (1.0 if size >= 0 else -1.0) * (0.5 + 0.5 * abs(size))

        # wall (none on the wrapped map: those sectors stay 0, like the web page)
        if not self.wrap:
            angs = hd[:, None] + np.arange(K)[None, :] * two_pi / K
            dx, dy = np.cos(angs), np.sin(angs)
            b = dx * H[:, :1] + dy * H[:, 1:]
            cc = (H ** 2).sum(1, keepdims=True) - c.world_radius ** 2
            t = -b + np.sqrt(np.maximum(b * b - cc, 0.0))
            out[:, 3] = 1.0 - np.clip(t / Vc, 0, 1)

        steps = np.array([s.steps for s in self.snakes], np.float32)
        centre = np.zeros(n, np.float32) if self.wrap else np.hypot(H[:, 0], H[:, 1]) / c.world_radius
        own = np.stack([np.tanh(lens / 200.0), (lens > c.min_boost_len).astype(np.float32),
                        centre, np.tanh(steps / 2000.0)], 1)
        res = np.concatenate([out.reshape(n, -1), own.astype(np.float32)], 1)
        res[~alive] = 0.0
        return res

    # ------------------------------------------------------------ bot brain
    def bot_action(self, i: int, o: np.ndarray | None = None) -> int:
        """Simple scripted opponent: go for food, turn away from danger and the wall."""
        if o is None:
            o = self.observe(i)
        K = self.cfg.sectors
        food, danger, wall = o[:K], o[K:2 * K], o[3 * K:4 * K]
        threat = np.maximum(danger, wall)
        score = food - 2.5 * threat
        score = np.convolve(np.r_[score[-1:], score, score[:1]], [0.25, 0.5, 0.25], "valid")
        best = int(np.argmax(score))
        rel = best if best <= K // 2 else best - K
        turn = 0 if rel == 0 else (1 if rel > 0 else -1)
        if threat[0] > 0.7:
            turn = 1 if threat[1:K // 2].sum() < threat[K // 2 + 1:].sum() else -1
        return ACTIONS.index((turn, 0))

    # ------------------------------------------------------------ step
    def step(self, actions: dict[int, int]):
        """Advance one tick. actions: agent index -> action id. Returns per-snake rewards, deaths."""
        c = self.cfg
        n = len(self.snakes)
        rewards = np.zeros(n, np.float32)
        died = np.zeros(n, bool)
        mass_before = np.array([s.mass for s in self.snakes], np.float32)
        bot_obs = self.observe_all() if (self.n_bots or self.n_league) else None
        league_act = {}
        if self.n_league:
            lo, hi = self.n_agents, self.n_agents + self.n_league
            idx = [i for i in range(lo, hi) if self.snakes[i].alive]
            if idx:
                league_act = self.league.act([self.snakes[i].net for i in idx], bot_obs[idx], idx)

        for i, s in enumerate(self.snakes):
            if not s.alive:
                continue
            if s.is_bot or (s.net < 0 and self._kind(i) == "league"):
                a = self.bot_action(i, bot_obs[i])  # a league seat with no brain yet plays like an NPC
            elif i in league_act:
                a = league_act[i]
            else:
                a = actions.get(i, 1)
            turn, boost = ACTIONS[a]
            size_factor = 1.0 / (1.0 + s.length / 400.0)
            s.heading = (s.heading + turn * c.max_turn * size_factor) % (2 * math.pi)
            moves = 1
            if boost and s.length > c.min_boost_len:
                moves = 2
                s.mass -= c.boost_cost
                if self.rng.random() < 0.5:
                    self._drop_food(s.body[-1:], c.boost_cost * 0.8)
            d = np.array([math.cos(s.heading), math.sin(s.heading)], np.float32) * c.seg_spacing
            target_len = max(c.start_len - 2, int(s.mass / c.mass_per_seg))
            for _ in range(moves):
                new_head = self._wp(s.body[0] + d)
                if target_len > len(s.body):
                    s.body = np.concatenate([new_head[None], s.body])
                else:
                    s.body = np.concatenate([new_head[None], s.body[:-1]])
            while len(s.body) > target_len and len(s.body) > 2:
                s.body = s.body[:-1]
            s.steps += 1

        # eat food (all snakes at once; a pellet two heads reach goes to the lower-numbered snake,
        # exactly like checking the snakes one by one)
        live = [i for i, s in enumerate(self.snakes) if s.alive]
        if len(self.food_pos) and live:
            Hh = np.stack([self.snakes[i].head for i in live]).astype(np.float32)
            reach = np.array([(self.snakes[i].radius() + 8.0) ** 2 for i in live], np.float32)
            rel = self._wd(self.food_pos[None, :, :] - Hh[:, None, :])
            hit = (rel[..., 0] ** 2 + rel[..., 1] ** 2) < reach[:, None]
            eaten = hit.any(0)
            if eaten.any():
                first = np.argmax(hit[:, eaten], 0)
                gain = np.zeros(len(live), np.float64)
                np.add.at(gain, first, self.food_val[eaten].astype(np.float64))
                for k, i in enumerate(live):
                    if gain[k]:
                        self.snakes[i].mass += float(gain[k])
                self.food_pos = self.food_pos[~eaten]
                self.food_val = self.food_val[~eaten]

        # collisions: head vs other bodies (first snake hit in order is the killer), head vs wall
        killer = [-1] * n
        self._pts = None; self._version += 1
        pts, owner, rad, _, _ = self._points()
        if live and len(pts):
            Hh = np.stack([self.snakes[i].head for i in live]).astype(np.float32)
            ri = np.array([self.snakes[i].radius() for i in live], np.float32)
            rel = self._wd(pts[None, :, :] - Hh[:, None, :])
            hit = (owner[None, :] != np.array(live)[:, None]) & \
                  ((rel[..., 0] ** 2 + rel[..., 1] ** 2) < (rad[None, :] + 0.6 * ri[:, None]) ** 2)
            any_hit = hit.any(1)
            first = np.argmax(hit, 1)
        for k, i in enumerate(live):
            s = self.snakes[i]
            if not self.wrap and float(s.head @ s.head) > c.world_radius ** 2:
                died[i] = True
                continue
            if len(pts) and any_hit[k]:
                died[i] = True
                killer[i] = int(owner[first[k]])

        for i, s in enumerate(self.snakes):
            if not s.alive:
                continue
            gain = s.mass - mass_before[i]
            rewards[i] += c.r_food * (gain if c.reward_net_mass else max(0.0, gain)) + c.r_alive
            s.max_len = max(s.max_len, s.length)
            if s.life and s.steps >= s.life:
                died[i] = True  # NPC old age (no death penalty matters: bots don't learn)
            if died[i]:
                rewards[i] += c.r_death
                self._drop_food(s.body, s.mass * c.death_food_frac)
                s.alive = False
                if killer[i] >= 0:
                    k = self.snakes[killer[i]]
                    k.kills += 1
                    rewards[killer[i]] += c.r_kill

        # keep the map stocked
        if len(self.food_pos) < c.food_count:
            self._spawn_food(min(20, c.food_count - len(self.food_pos)))
        if len(self.food_pos) > c.food_count * 3:  # trim old pellets
            self.food_pos = self.food_pos[-c.food_count * 3:]
            self.food_val = self.food_val[-c.food_count * 3:]

        # bots and league opponents respawn on their own
        for i, s in enumerate(self.snakes):
            if not s.alive and i >= self.n_agents:
                self.snakes[i] = self._new_snake(s.is_bot, league=not s.is_bot)
        self._pts = None; self._version += 1
        return rewards, died

    def snapshot(self, max_food: int = 4000) -> dict:
        """A small picture of this world for the live "inside training" view (ints, every 2nd body point)."""
        c = self.cfg
        snakes = []
        for i, o in enumerate(self.snakes):
            if not o.alive:
                continue
            b = o.body[::2]
            snakes.append({"k": self._kind(i)[0], "h": round(float(o.heading), 2), "n": int(o.length),
                           "b": np.round(b).astype(np.int32).ravel().tolist()})
        fp, fv = self.food_pos, self.food_val
        if len(fp) > max_food:
            sel = np.linspace(0, len(fp) - 1, max_food).astype(int)
            fp, fv = fp[sel], fv[sel]
        food = np.concatenate([np.round(fp), np.round(fv[:, None], 1)], 1).ravel().tolist() if len(fp) else []
        return {"wrap": self.wrap, "L": self.L, "R": float(c.world_radius), "snakes": snakes, "food": food}

    def respawn(self, i: int):
        self.snakes[i] = self._new_snake(self.snakes[i].is_bot, league=(self._kind(i) == "league"))
        self._pts = None; self._version += 1


# ---------------------------------------------------------------- league
class LeaguePool:
    """Frozen older brains that play as opponents (league training).

    Reads league_dir/*.npz (w0,b0,w1,b1,w2,b2 = the policy MLP, view = how far it sees).
    New files are picked up every few minutes, so champions saved during the run join the league.
    Half the time an opponent seat gets one of the 3 newest brains, otherwise any brain in the pool.
    """

    def __init__(self, league_dir, recent: int = 3, refresh_seconds: float = 300.0):
        from pathlib import Path
        self.dir = Path(league_dir)
        self.recent = recent
        self.refresh_seconds = refresh_seconds
        self.nets: list[tuple[str, list, float]] = []
        self.names: set[str] = set()
        self._last = -1e18
        self.refresh()

    def refresh(self):
        """Sync with the folder: new champions join, files the app pruned away leave."""
        import time
        self._last = time.time()
        if not self.dir.exists():
            return
        have = {name: (layers, view) for name, layers, view in self.nets}
        nets = []
        for p in sorted(self.dir.glob("*.npz")):
            if p.name in have:
                nets.append((p.name, *have[p.name]))
                continue
            try:
                with np.load(p) as z:
                    layers = [(z[f"w{k}"].astype(np.float32), z[f"b{k}"].astype(np.float32)) for k in range(3)]
                    view = float(z["view"])
            except Exception:
                continue  # still being written: try again next refresh
            nets.append((p.name, layers, view))
        self.nets = nets
        self.names = {n for n, _, _ in nets}

    def pick(self, rng: np.random.Generator) -> tuple[int, float]:
        import time
        if time.time() - self._last > self.refresh_seconds:
            self.refresh()
        if not self.nets:
            return -1, 0.0
        n = len(self.nets)
        if rng.random() < 0.5:
            k = n - 1 - int(rng.integers(min(self.recent, n)))
        else:
            k = int(rng.integers(n))
        return k, self.nets[k][2]

    def act(self, nets: list[int], obs: np.ndarray, idx: list[int]) -> dict[int, int]:
        out = {}
        nets = np.asarray(nets)
        for k in np.unique(nets):
            if k < 0 or k >= len(self.nets):
                continue
            sel = np.nonzero(nets == k)[0]
            x = obs[sel]
            (w0, b0), (w1, b1), (w2, b2) = self.nets[k][1]
            x = np.tanh(x @ w0.T + b0)
            x = np.tanh(x @ w1.T + b1)
            a = np.argmax(x @ w2.T + b2, axis=1)
            for j, act in zip(sel, a):
                out[idx[j]] = int(act)
        return out


def save_league_brain(policy_state: dict, path, view: float):
    """Write one brain for the league from an SB3 policy state_dict (atomic: never a half file)."""
    import os
    names = ["mlp_extractor.policy_net.0", "mlp_extractor.policy_net.2", "action_net"]
    arrs = {"view": np.float32(view)}
    for k, n in enumerate(names):
        arrs[f"w{k}"] = policy_state[n + ".weight"].detach().cpu().numpy().astype(np.float32)
        arrs[f"b{k}"] = policy_state[n + ".bias"].detach().cpu().numpy().astype(np.float32)
    path = str(path)
    tmp = path + ".part"
    with open(tmp, "wb") as f:
        np.savez(f, **arrs)
    os.replace(tmp, path)


# ---------------------------------------------------------------- SB3 vec env
class ArenaVecEnv(VecEnv):
    """Every agent snake in every arena is one env slot; all share one policy."""

    def __init__(self, n_arenas: int = 8, agents_per_arena: int = 6, bots_per_arena: int = 4,
                 cfg: ArenaConfig | None = None, seed: int = 0, league_per_arena: int = 0,
                 league_dir: str | None = None):
        self.cfg = cfg or ArenaConfig()
        self.rng = np.random.default_rng(seed)
        self.league = LeaguePool(league_dir) if (league_per_arena and league_dir) else None
        self.arenas = [Arena(self.cfg, agents_per_arena, bots_per_arena,
                             np.random.default_rng(self.rng.integers(1 << 31)),
                             n_league=league_per_arena, league=self.league)
                       for _ in range(n_arenas)]
        self.apa = agents_per_arena
        obs_space = spaces.Box(-1.0, 1.0, (obs_size(self.cfg),), np.float32)
        act_space = spaces.Discrete(N_ACTIONS)
        super().__init__(n_arenas * agents_per_arena, obs_space, act_space)
        self._actions = None

    def _slot(self, k):
        return self.arenas[k // self.apa], k % self.apa

    def reset(self):
        for a in self.arenas:
            a.reset()
        return np.concatenate([a.observe_all()[: self.apa] for a in self.arenas])

    def step_async(self, actions):
        self._actions = np.asarray(actions)

    def step_wait(self):
        n = self.num_envs
        rews = np.zeros(n, np.float32)
        dones = np.zeros(n, bool)
        infos = [{} for _ in range(n)]
        for ai, arena in enumerate(self.arenas):
            acts = {i: int(self._actions[ai * self.apa + i]) for i in range(self.apa)}
            r, died = arena.step(acts)
            end_obs = None
            for i in range(self.apa):
                k = ai * self.apa + i
                s = arena.snakes[i]
                rews[k] = r[i]
                s.ep_return += float(r[i])
                trunc = s.alive and s.steps >= self.cfg.max_steps_per_life
                if died[i] or trunc:
                    dones[k] = True
                    if end_obs is None:  # one observation pass for everyone who ends this tick
                        end_obs = arena.observe_all().copy()
                    infos[k]["terminal_observation"] = end_obs[i] if s.alive else np.zeros(self.observation_space.shape, np.float32)
                    infos[k]["TimeLimit.truncated"] = bool(trunc and not died[i])
                    infos[k]["episode"] = {"r": s.ep_return, "l": s.steps}
                    infos[k]["snake"] = {"max_len": s.max_len, "kills": s.kills, "steps": s.steps}
                    arena.respawn(i)
        obs = np.concatenate([a.observe_all()[: self.apa] for a in self.arenas])
        return obs, rews, dones, infos

    def set_cfg(self, **kw):
        """Change world settings while training runs (the auto-curriculum uses this). All arenas share one cfg."""
        for k, v in kw.items():
            if hasattr(self.cfg, k):
                setattr(self.cfg, k, v)

    def close(self):
        pass

    def get_attr(self, attr_name, indices=None):
        return [getattr(self, attr_name, None) for _ in self._idx(indices)]

    def set_attr(self, attr_name, value, indices=None):
        setattr(self, attr_name, value)

    def env_method(self, method_name, *args, indices=None, **kwargs):
        return [None for _ in self._idx(indices)]

    def env_is_wrapped(self, wrapper_class, indices=None):
        return [False for _ in self._idx(indices)]

    def seed(self, seed=None):
        self.rng = np.random.default_rng(seed)
        return [seed] * self.num_envs

    def _idx(self, indices):
        if indices is None:
            return range(self.num_envs)
        if isinstance(indices, int):
            return [indices]
        return indices


# ---------------------------------------------------------------- multi-core version
def write_snapshot(arena: "Arena", path: str, extra: dict | None = None):
    """Atomically write arena.snapshot() as JSON. Never raises: the live view is best effort."""
    import json, os
    try:
        d = arena.snapshot()
        if extra:
            d.update(extra)
        tmp = f"{path}.{os.getpid()}.part"
        with open(tmp, "w") as f:
            json.dump(d, f, separators=(",", ":"))
        os.replace(tmp, path)
    except Exception:
        pass


def _worker(remote, n_arenas, apa, bots, cfg, seed, league_per_arena=0, league_dir=None,
            snap_path=None, snap_fps=10.0):
    # Priority is inherited from the trainer process (the app starts it below normal).
    import time
    env = ArenaVecEnv(n_arenas, apa, bots, cfg, seed, league_per_arena, league_dir)
    gap = 1.0 / snap_fps if snap_fps and snap_fps > 0 else 0.0
    last_snap = 0.0
    while True:
        cmd, data = remote.recv()
        if cmd == "step":
            env.step_async(data)
            remote.send(env.step_wait())
            if snap_path and gap:
                now = time.time()
                if now - last_snap >= gap:
                    last_snap = now
                    write_snapshot(env.arenas[0], snap_path, {"t": round(now, 2)})
        elif cmd == "reset":
            remote.send(env.reset())
        elif cmd == "set_cfg":
            env.set_cfg(**data)
        elif cmd == "close":
            remote.close()
            break


class SubprocArenaVecEnv(VecEnv):
    """Splits the arenas across worker processes so several CPU cores run the game."""

    def __init__(self, n_workers: int, arenas_per_worker: int = 2, agents_per_arena: int = 6,
                 bots_per_arena: int = 4, cfg: ArenaConfig | None = None, seed: int = 0,
                 league_per_arena: int = 0, league_dir: str | None = None,
                 snap_path: str | None = None, snap_fps: float = 10.0):
        import multiprocessing as mp
        self.cfg = cfg or ArenaConfig()
        ctx = mp.get_context("spawn")
        self.per = arenas_per_worker * agents_per_arena
        self.remotes, self.procs = [], []
        for w in range(n_workers):
            a, b = ctx.Pipe()
            p = ctx.Process(target=_worker, args=(b, arenas_per_worker, agents_per_arena,
                                                  bots_per_arena, self.cfg, seed * 1000 + w,
                                                  league_per_arena, league_dir,
                                                  snap_path if w == 0 else None, snap_fps), daemon=True)
            p.start()
            b.close()
            self.remotes.append(a)
            self.procs.append(p)
        obs_space = spaces.Box(-1.0, 1.0, (obs_size(self.cfg),), np.float32)
        super().__init__(n_workers * self.per, obs_space, spaces.Discrete(N_ACTIONS))

    def reset(self):
        for r in self.remotes:
            r.send(("reset", None))
        return np.concatenate([r.recv() for r in self.remotes])

    def step_async(self, actions):
        actions = np.asarray(actions)
        for w, r in enumerate(self.remotes):
            r.send(("step", actions[w * self.per:(w + 1) * self.per]))

    def step_wait(self):
        res = [r.recv() for r in self.remotes]
        obs = np.concatenate([x[0] for x in res])
        rews = np.concatenate([x[1] for x in res])
        dones = np.concatenate([x[2] for x in res])
        infos = [i for x in res for i in x[3]]
        return obs, rews, dones, infos

    def set_cfg(self, **kw):
        for k, v in kw.items():
            if hasattr(self.cfg, k):
                setattr(self.cfg, k, v)
        for r in self.remotes:
            r.send(("set_cfg", kw))

    def close(self):
        for r in self.remotes:
            try:
                r.send(("close", None))
            except Exception:
                pass
        for p in self.procs:
            p.join(timeout=5)

    def get_attr(self, attr_name, indices=None):
        return [getattr(self, attr_name, None) for _ in ArenaVecEnv._idx(self, indices)]

    def set_attr(self, attr_name, value, indices=None):
        setattr(self, attr_name, value)

    def env_method(self, method_name, *args, indices=None, **kwargs):
        return [None for _ in ArenaVecEnv._idx(self, indices)]

    def env_is_wrapped(self, wrapper_class, indices=None):
        return [False for _ in ArenaVecEnv._idx(self, indices)]

    def seed(self, seed=None):
        return [seed] * self.num_envs
