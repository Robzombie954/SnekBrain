// SnekBrain snake arena, JavaScript port of arena.py (same rules, same observation).
// Works in the browser (global SA) and in Node (module.exports) for parity tests.
(function (root, factory) { factory(root); root.SA_FACTORY = factory; })(typeof window !== "undefined" ? window : globalThis, function (root) {
  "use strict";

  const CFG = {
    world_radius: 1400, food_count: 600, food_value: 1.0, seg_spacing: 6.0, start_len: 12,
    mass_per_seg: 3.0, max_turn: 0.16, boost_cost: 0.6, min_boost_len: 14, death_food_frac: 0.6,
    view_radius: 420, sectors: 16,
    // wrap: true makes a square world of side 2*half_size with no edge (leave one side, come back on the other)
    wrap: false, half_size: 3000, cell: 120,
    // npc_life: [min, max] ticks before an NPC snake dies of old age and drops its food, so NPCs don't grow forever (0 = never)
    npc_life: 0,
    // agent_view: how far the first nAgents snakes see (a brain plays with the vision it was trained with; 0 = view_radius)
    agent_view: 0,
  };
  const ACTIONS = [[-1, 0], [0, 0], [1, 0], [-1, 1], [0, 1], [1, 1]];
  const TWO_PI = Math.PI * 2;

  function mulberry32(a) {
    return function () {
      a |= 0; a = (a + 0x6D2B79F5) | 0;
      let t = Math.imul(a ^ (a >>> 15), 1 | a);
      t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }
  // Python's % on floats: result has the sign of the divisor
  const pmod = (a, b) => a - b * Math.floor(a / b);

  function radius(s) { return 5.0 + Math.min(14.0, 0.03 * s.body.length); }

  class Arena {
    constructor(nAgents, nBots, seed, cfg) {
      this.cfg = Object.assign({}, CFG, cfg || {});
      this.rand = mulberry32(seed || 1);
      this.nAgents = nAgents; this.nBots = nBots;
      this.wrap = !!this.cfg.wrap;
      if (this.wrap) {
        const L = this.cfg.half_size;
        this.L = L; this.S = 2 * L;
        this.N = Math.max(3, Math.floor(this.S / this.cfg.cell));
        this.cw = this.S / this.N;
      }
      this.reset();
    }
    gauss() { // Box-Muller
      let u = 0, v = 0;
      while (u === 0) u = this.rand();
      while (v === 0) v = this.rand();
      return Math.sqrt(-2 * Math.log(u)) * Math.cos(TWO_PI * v);
    }
    wd(d) { return this.wrap ? d - this.S * Math.round(d / this.S) : d; }       // shortest offset on the wrapped map
    wp(x) { return this.wrap ? x - this.S * Math.floor((x + this.L) / this.S) : x; } // position back into [-L, L)
    cellIx(x) { let k = Math.floor((x + this.L) / this.cw); return k >= this.N ? k - this.N : k < 0 ? k + this.N : k; }
    // counting-sort grid of points so nearby lookups stay cheap on a big map
    buildFoodGrid() {
      const N = this.N, cnt = new Int32Array(N * N + 1), F = this.food, cell = new Int32Array(F.length);
      for (let f = 0; f < F.length; f++) { const c = this.cellIx(F[f][1]) * N + this.cellIx(F[f][0]); cell[f] = c; cnt[c + 1]++; }
      for (let c = 0; c < N * N; c++) cnt[c + 1] += cnt[c];
      const pos = cnt.slice(0, N * N), items = new Int32Array(F.length);
      for (let f = 0; f < F.length; f++) items[pos[cell[f]]++] = f;
      this.fg = { start: cnt, items }; this.foodDirty = false;
    }
    buildBodyGrid() {
      const N = this.N; let total = 0;
      for (const s of this.snakes) if (s.alive) total += s.body.length;
      const cnt = new Int32Array(N * N + 1), cell = new Int32Array(total), sj = new Int32Array(total), sp = new Int32Array(total);
      let t = 0;
      for (let j = 0; j < this.snakes.length; j++) {
        const s = this.snakes[j];
        if (!s.alive) continue;
        for (let p = 0; p < s.body.length; p++, t++) { const c = this.cellIx(s.body[p][1]) * N + this.cellIx(s.body[p][0]); cell[t] = c; sj[t] = j; sp[t] = p; cnt[c + 1]++; }
      }
      for (let c = 0; c < N * N; c++) cnt[c + 1] += cnt[c];
      const pos = cnt.slice(0, N * N), ij = new Int32Array(total), ip = new Int32Array(total);
      for (let u = 0; u < total; u++) { const k = pos[cell[u]]++; ij[k] = sj[u]; ip[k] = sp[u]; }
      this.bg = { start: cnt, j: ij, p: ip }; this.bodyDirty = false;
    }
    ensureGrids() { if (this.foodDirty) this.buildFoodGrid(); if (this.bodyDirty) this.buildBodyGrid(); }
    // calls fn(cellIndex) for every grid cell within r of (x, y), each cell once
    nearCells(x, y, r, fn) {
      const N = this.N, R = Math.min(Math.ceil(r / this.cw), (N - 1) >> 1), ci = this.cellIx(x), cj = this.cellIx(y);
      for (let dy = -R; dy <= R; dy++) {
        let yy = cj + dy; if (yy < 0) yy += N; else if (yy >= N) yy -= N;
        for (let dx = -R; dx <= R; dx++) {
          let xx = ci + dx; if (xx < 0) xx += N; else if (xx >= N) xx -= N;
          fn(yy * N + xx);
        }
      }
    }
    randomPoint(margin) {
      if (this.wrap) return [(this.rand() * 2 - 1) * this.L, (this.rand() * 2 - 1) * this.L];
      const r = (this.cfg.world_radius - margin) * Math.sqrt(this.rand());
      const a = this.rand() * TWO_PI;
      return [r * Math.cos(a), r * Math.sin(a)];
    }
    newSnake(isBot) {
      const c = this.cfg;
      const head = this.randomPoint(300);
      const heading = this.rand() * TWO_PI;
      const bx = -Math.cos(heading), by = -Math.sin(heading);
      const body = [];
      for (let i = 0; i < c.start_len; i++) body.push([this.wp(head[0] + bx * i * c.seg_spacing), this.wp(head[1] + by * i * c.seg_spacing)]);
      const life = isBot && c.npc_life ? c.npc_life[0] + Math.floor(this.rand() * (c.npc_life[1] - c.npc_life[0])) : 0;
      const view = !isBot && c.agent_view ? c.agent_view : c.view_radius;
      return { body, heading, mass: c.start_len * c.mass_per_seg, alive: true, steps: 0, kills: 0, isBot, maxLen: c.start_len, life, view };
    }
    spawnFood(n) {
      this.foodDirty = true;
      for (let i = 0; i < n; i++) {
        if (this.wrap) { this.food.push([(this.rand() * 2 - 1) * this.L, (this.rand() * 2 - 1) * this.L, this.cfg.food_value * (0.5 + this.rand())]); continue; }
        const r = this.cfg.world_radius * Math.sqrt(this.rand());
        const a = this.rand() * TWO_PI;
        this.food.push([r * Math.cos(a), r * Math.sin(a), this.cfg.food_value * (0.5 + this.rand())]);
      }
    }
    dropFood(points, total) {
      if (!points.length || total <= 0) return;
      const idx = [];
      for (let i = 0; i < points.length; i += 2) idx.push(i);
      const v = total / idx.length;
      for (const i of idx) this.food.push([this.wp(points[i][0] + this.gauss() * 4), this.wp(points[i][1] + this.gauss() * 4), v]);
      this.foodDirty = true;
    }
    reset() {
      this.snakes = [];
      for (let i = 0; i < this.nAgents; i++) this.snakes.push(this.newSnake(false));
      for (let i = 0; i < this.nBots; i++) this.snakes.push(this.newSnake(true));
      this.food = [];
      this.spawnFood(this.cfg.food_count);
      this.foodDirty = this.bodyDirty = true;
    }
    respawn(i) { this.snakes[i] = this.newSnake(this.snakes[i].isBot); this.bodyDirty = true; }

    // Same features as Arena.observe() in Python: 16 sectors x (food, danger, heads, wall) + 4 own values.
    observe(i) {
      if (this.wrap) return this.observeWrap(i);
      const c = this.cfg, K = c.sectors, V = c.view_radius;
      const s = this.snakes[i];
      const out = new Float32Array(4 * K + 4);
      if (!s.alive) return out;
      const hx = s.body[0][0], hy = s.body[0][1];
      const ch = Math.cos(-s.heading), sh = Math.sin(-s.heading);
      const sector = (rx, ry) => {
        const x = rx * ch - ry * sh, y = rx * sh + ry * ch;
        const ang = Math.atan2(y, x);
        return Math.floor(pmod(ang + Math.PI / K, TWO_PI) / TWO_PI * K) % K;
      };
      const food = new Float64Array(K);
      for (const f of this.food) {
        const rx = f[0] - hx, ry = f[1] - hy;
        const d = Math.hypot(rx, ry);
        if (d < V) food[sector(rx, ry)] += f[2] * (1 - d / V);
      }
      for (let k = 0; k < K; k++) out[k] = Math.tanh(food[k] / 4.0);
      for (let j = 0; j < this.snakes.length; j++) {
        const o = this.snakes[j];
        if (j === i || !o.alive) continue;
        const orad = radius(o);
        for (let p = 0; p < o.body.length; p++) {
          const rx = o.body[p][0] - hx, ry = o.body[p][1] - hy;
          const d = Math.hypot(rx, ry) - orad;
          if (d >= V) continue;
          const sec = sector(rx, ry);
          const close = 1 - Math.max(d, 0) / V;
          if (close > out[K + sec]) out[K + sec] = close;
          if (p === 0) {
            const size = Math.tanh((o.body.length - s.body.length) / 50.0);
            if (close > Math.abs(out[2 * K + sec])) out[2 * K + sec] = close * (size >= 0 ? 1 : -1) * (0.5 + 0.5 * Math.abs(size));
          }
        }
      }
      const cc = hx * hx + hy * hy - c.world_radius * c.world_radius;
      for (let k = 0; k < K; k++) {
        const a = s.heading + k * TWO_PI / K;
        const b = Math.cos(a) * hx + Math.sin(a) * hy;
        const t = -b + Math.sqrt(Math.max(b * b - cc, 0));
        out[3 * K + k] = 1 - Math.min(Math.max(t / V, 0), 1);
      }
      out[4 * K] = Math.tanh(s.body.length / 200.0);
      out[4 * K + 1] = s.body.length > c.min_boost_len ? 1 : 0;
      out[4 * K + 2] = Math.hypot(hx, hy) / c.world_radius;
      out[4 * K + 3] = Math.tanh(s.steps / 2000.0);
      return out;
    }

    // Same features on the wrapped map: no wall, so the wall sectors stay 0 and "distance from centre" reads 0.
    observeWrap(i) {
      this.ensureGrids();
      const s = this.snakes[i];
      const c = this.cfg, K = c.sectors, V = s.view || c.view_radius, S = this.S;
      const out = new Float32Array(4 * K + 4);
      if (!s.alive) return out;
      const hx = s.body[0][0], hy = s.body[0][1];
      const ch = Math.cos(-s.heading), sh = Math.sin(-s.heading);
      const sector = (rx, ry) => {
        const x = rx * ch - ry * sh, y = rx * sh + ry * ch;
        const ang = Math.atan2(y, x);
        return Math.floor(pmod(ang + Math.PI / K, TWO_PI) / TWO_PI * K) % K;
      };
      const food = new Float64Array(K), F = this.food, fg = this.fg, V2 = V * V, VB2 = (V + 20) * (V + 20);
      this.nearCells(hx, hy, V, (cell) => {
        for (let t = fg.start[cell], e = fg.start[cell + 1]; t < e; t++) {
          const f = F[fg.items[t]];
          let rx = f[0] - hx, ry = f[1] - hy;
          rx -= S * Math.round(rx / S); ry -= S * Math.round(ry / S);
          const d2 = rx * rx + ry * ry;
          if (d2 >= V2) continue;
          food[sector(rx, ry)] += f[2] * (1 - Math.sqrt(d2) / V);
        }
      });
      for (let k = 0; k < K; k++) out[k] = Math.tanh(food[k] / 4.0);
      const bg = this.bg, snakes = this.snakes, myLen = s.body.length;
      this.nearCells(hx, hy, V + 20, (cell) => {
        for (let t = bg.start[cell], e = bg.start[cell + 1]; t < e; t++) {
          const j = bg.j[t];
          if (j === i) continue;
          const o = snakes[j], p = bg.p[t];
          let rx = o.body[p][0] - hx, ry = o.body[p][1] - hy;
          rx -= S * Math.round(rx / S); ry -= S * Math.round(ry / S);
          const d2 = rx * rx + ry * ry;
          if (d2 >= VB2) continue;
          const d = Math.sqrt(d2) - radius(o);
          if (d >= V) continue;
          const sec = sector(rx, ry);
          const close = 1 - Math.max(d, 0) / V;
          if (close > out[K + sec]) out[K + sec] = close;
        }
      });
      // heads in snake order (the size feature depends on visiting order, as in the Python arena)
      for (let j = 0; j < snakes.length; j++) {
        const o = snakes[j];
        if (j === i || !o.alive) continue;
        let rx = o.body[0][0] - hx, ry = o.body[0][1] - hy;
        rx -= S * Math.round(rx / S); ry -= S * Math.round(ry / S);
        const d = Math.hypot(rx, ry) - radius(o);
        if (d >= V) continue;
        const sec = sector(rx, ry), close = 1 - Math.max(d, 0) / V;
        const size = Math.tanh((o.body.length - myLen) / 50.0);
        if (close > Math.abs(out[2 * K + sec])) out[2 * K + sec] = close * (size >= 0 ? 1 : -1) * (0.5 + 0.5 * Math.abs(size));
      }
      out[4 * K] = Math.tanh(s.body.length / 200.0);
      out[4 * K + 1] = s.body.length > c.min_boost_len ? 1 : 0;
      out[4 * K + 2] = 0;
      out[4 * K + 3] = Math.tanh(s.steps / 2000.0);
      return out;
    }

    botAction(o) {
      const K = this.cfg.sectors;
      const score = new Float64Array(K), threat = new Float64Array(K);
      for (let k = 0; k < K; k++) {
        threat[k] = Math.max(o[K + k], o[3 * K + k]);
        score[k] = o[k] - 2.5 * threat[k];
      }
      let best = 0, bestV = -Infinity;
      for (let k = 0; k < K; k++) {
        const v = 0.25 * score[(k - 1 + K) % K] + 0.5 * score[k] + 0.25 * score[(k + 1) % K];
        if (v > bestV) { bestV = v; best = k; }
      }
      const rel = best <= K / 2 ? best : best - K;
      let turn = rel === 0 ? 0 : (rel > 0 ? 1 : -1);
      if (threat[0] > 0.7) {
        let l = 0, r = 0;
        for (let k = 1; k < K / 2; k++) l += threat[k];
        for (let k = K / 2 + 1; k < K; k++) r += threat[k];
        turn = l < r ? 1 : -1;
      }
      return turn === -1 ? 0 : turn === 0 ? 1 : 2;
    }

    // actions: array (agent index -> action id). Returns {died:[...], killer:[...]}.
    step(actions) {
      const c = this.cfg, n = this.snakes.length;
      const botObs = this.snakes.map((s, i) => (s.isBot && s.alive ? this.observe(i) : null));
      for (let i = 0; i < n; i++) {
        const s = this.snakes[i];
        if (!s.alive) continue;
        const a = s.isBot ? this.botAction(botObs[i]) : (actions[i] ?? 1);
        const [turn, boost] = ACTIONS[a];
        const sizeFactor = 1 / (1 + s.body.length / 400);
        s.heading = pmod(s.heading + turn * c.max_turn * sizeFactor, TWO_PI);
        let moves = 1;
        if (boost && s.body.length > c.min_boost_len) {
          moves = 2;
          s.mass -= c.boost_cost;
          if (this.rand() < 0.5) this.dropFood([s.body[s.body.length - 1]], c.boost_cost * 0.8);
        }
        const dx = Math.cos(s.heading) * c.seg_spacing, dy = Math.sin(s.heading) * c.seg_spacing;
        const target = Math.max(c.start_len - 2, Math.floor(s.mass / c.mass_per_seg));
        for (let m = 0; m < moves; m++) {
          const nh = [this.wp(s.body[0][0] + dx), this.wp(s.body[0][1] + dy)];
          s.body.unshift(nh);
          if (target < s.body.length) s.body.pop();
        }
        while (s.body.length > target && s.body.length > 2) s.body.pop();
        s.steps++;
      }
      // eat
      const eaten = new Uint8Array(this.food.length);
      if (this.wrap) {
        this.ensureGrids();
        const S = this.S, F = this.food, fg = this.fg;
        for (const s of this.snakes) {
          if (!s.alive) continue;
          const rr = (radius(s) + 8) ** 2, hx = s.body[0][0], hy = s.body[0][1];
          this.nearCells(hx, hy, Math.sqrt(rr), (cell) => {
            for (let t = fg.start[cell], e = fg.start[cell + 1]; t < e; t++) {
              const f = fg.items[t];
              if (eaten[f]) continue;
              let rx = F[f][0] - hx, ry = F[f][1] - hy;
              rx -= S * Math.round(rx / S); ry -= S * Math.round(ry / S);
              if (rx * rx + ry * ry < rr) { s.mass += F[f][2]; eaten[f] = 1; }
            }
          });
        }
      } else for (const s of this.snakes) {
        if (!s.alive) continue;
        const rr = (radius(s) + 8) ** 2, hx = s.body[0][0], hy = s.body[0][1];
        for (let f = 0; f < this.food.length; f++) {
          if (eaten[f]) continue;
          const rx = this.food[f][0] - hx, ry = this.food[f][1] - hy;
          if (rx * rx + ry * ry < rr) { s.mass += this.food[f][2]; eaten[f] = 1; }
        }
      }
      this.food = this.food.filter((_, f) => !eaten[f]);
      this.foodDirty = true;
      // collisions
      const died = new Array(n).fill(false), killer = new Array(n).fill(-1);
      if (this.wrap) {
        this.buildBodyGrid();
        const S = this.S, bg = this.bg, snakes = this.snakes;
        for (let i = 0; i < n; i++) {
          const s = snakes[i];
          if (!s.alive) continue;
          const hx = s.body[0][0], hy = s.body[0][1], ri = radius(s);
          let best = -1;
          this.nearCells(hx, hy, 40, (cell) => {
            for (let t = bg.start[cell], e = bg.start[cell + 1]; t < e; t++) {
              const j = bg.j[t];
              if (j === i || (best >= 0 && j >= best)) continue;
              const o = snakes[j], p = o.body[bg.p[t]];
              let rx = p[0] - hx, ry = p[1] - hy;
              rx -= S * Math.round(rx / S); ry -= S * Math.round(ry / S);
              if (rx * rx + ry * ry < (radius(o) + ri * 0.6) ** 2) best = j;
            }
          });
          if (best >= 0) { died[i] = true; killer[i] = best; }
        }
      } else for (let i = 0; i < n; i++) {
        const s = this.snakes[i];
        if (!s.alive) continue;
        const hx = s.body[0][0], hy = s.body[0][1];
        if (hx * hx + hy * hy > c.world_radius ** 2) { died[i] = true; continue; }
        const ri = radius(s);
        outer: for (let j = 0; j < n; j++) {
          const o = this.snakes[j];
          if (j === i || !o.alive) continue;
          const rr = (radius(o) + ri * 0.6) ** 2;
          for (const p of o.body) {
            const rx = p[0] - hx, ry = p[1] - hy;
            if (rx * rx + ry * ry < rr) { died[i] = true; killer[i] = j; break outer; }
          }
        }
      }
      for (let i = 0; i < n; i++) {
        const s = this.snakes[i];
        if (!s.alive) continue;
        s.maxLen = Math.max(s.maxLen, s.body.length);
        if (s.life && s.steps >= s.life) died[i] = true; // NPC old age
        if (died[i]) {
          this.dropFood(s.body, s.mass * c.death_food_frac);
          s.alive = false;
          if (killer[i] >= 0) this.snakes[killer[i]].kills++;
        }
      }
      if (this.food.length < c.food_count) this.spawnFood(Math.min(20, c.food_count - this.food.length));
      if (this.food.length > c.food_count * 3) this.food = this.food.slice(-c.food_count * 3);
      for (let i = 0; i < n; i++) if (this.snakes[i].isBot && !this.snakes[i].alive) this.snakes[i] = this.newSnake(true);
      this.foodDirty = this.bodyDirty = true;
      return { died, killer };
    }
  }

  // The trained brain: 68 -> 256 tanh -> 256 tanh -> 6 action scores.
  class Brain {
    constructor(json) {
      const dec = (b64) => {
        const bin = typeof atob === "function" ? atob(b64) : Buffer.from(b64, "base64").toString("binary");
        const bytes = new Uint8Array(bin.length);
        for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
        return new Float32Array(bytes.buffer);
      };
      this.layers = json.layers.map((L) => ({ w: dec(L.w), b: dec(L.b), in: L.in, out: L.out, act: L.act }));
      this.gen = json.generation; this.info = json.info || {};
    }
    logits(obs) {
      let x = obs;
      for (const L of this.layers) {
        const y = new Float32Array(L.out);
        for (let o = 0; o < L.out; o++) {
          let acc = L.b[o];
          const row = o * L.in;
          for (let k = 0; k < L.in; k++) acc += L.w[row + k] * x[k];
          y[o] = L.act === "tanh" ? Math.tanh(acc) : acc;
        }
        x = y;
      }
      return x;
    }
    act(obs, rand, greedy) {
      const z = this.logits(obs);
      if (greedy) { let b = 0; for (let k = 1; k < z.length; k++) if (z[k] > z[b]) b = k; return b; }
      let m = -Infinity; for (const v of z) m = Math.max(m, v);
      const p = Array.from(z, (v) => Math.exp(v - m)); const s = p.reduce((a, b) => a + b, 0);
      let r = rand() * s;
      for (let k = 0; k < p.length; k++) { r -= p[k]; if (r <= 0) return k; }
      return p.length - 1;
    }
  }

  const SA = { Arena, Brain, ACTIONS, CFG, mulberry32, radius };
  if (typeof module !== "undefined" && module.exports) module.exports = SA; else root.SA = SA;
});
