// SnekBrain: drawing the world, running games on screen, highlights and video recording.
// Everything here runs in the window. The real learning happens in the background trainer;
// this just plays the newest brain it hands over.
(function (root) {
  "use strict";
  const TICKS_PER_SEC = 30;          // game speed at 1x
  const FRAME_EVERY = 2;             // highlight frames: every 2nd tick (15 per second)
  const HL_BEFORE = 150, HL_AFTER = 45;
  const ACTION_NAMES = ["Left", "Straight", "Right", "Left + boost", "Boost", "Right + boost"];
  const AGENT_HUES = [150, 190, 120, 210, 170, 100, 230, 135, 200, 160, 110, 180, 220, 145, 195, 125];
  const radiusOf = (n) => 5 + Math.min(14, 0.03 * n);

  function colorFor(s) {
    if (s.k === "h") return { body: "#ffd84a", glow: "rgba(255,216,74,.55)" };
    if (s.k === "l") return { body: "#b98cff", glow: "rgba(185,140,255,.35)" };
    if (s.k === "b") return { body: "#c98a56", glow: "rgba(201,138,86,.2)" };
    if (s.k === "t") { const h = s.c * 57 % 360; return { body: `hsl(${h} 90% 62%)`, glow: `hsla(${h},90%,62%,.45)` }; }
    const h = AGENT_HUES[(s.c || 0) % AGENT_HUES.length];
    return { body: `hsl(${h} 95% 58%)`, glow: `hsla(${h},95%,58%,.45)` };
  }

  // ------------------------------------------------------------------ drawing
  class Renderer {
    constructor(canvas) { this.cv = canvas; this.ctx = canvas.getContext("2d"); this.t = 0; }
    resize() {
      const r = this.cv.getBoundingClientRect(), dpr = Math.min(2, window.devicePixelRatio || 1);
      const w = Math.max(50, Math.round(r.width * dpr)), h = Math.max(50, Math.round(r.height * dpr));
      if (this.cv.width !== w || this.cv.height !== h) { this.cv.width = w; this.cv.height = h; }
      this.dpr = dpr;
    }
    clear() { const c = this.ctx; c.setTransform(1, 0, 0, 1, 0, 0); c.fillStyle = "#04060a"; c.fillRect(0, 0, this.cv.width, this.cv.height); }
    // view: {wrap, L, R, snakes:[{k,c,n,b:[x,y,...],h,label}], food:[x,y,v,...]}; cam: {x,y,zoom}; rect: [x,y,w,h] in canvas pixels
    draw(view, cam, rect, opts) {
      opts = opts || {};
      const c = this.ctx, [rx, ry, rw, rh] = rect || [0, 0, this.cv.width, this.cv.height];
      const z = cam.zoom * (this.dpr || 1), cx = rx + rw / 2, cy = ry + rh / 2;
      const S = view.wrap ? 2 * view.L : 0;
      const wd = (d) => (S ? d - S * Math.round(d / S) : d);
      const sx = (x) => cx + wd(x - cam.x) * z, sy = (y) => cy + wd(y - cam.y) * z;
      c.save();
      c.beginPath(); c.rect(rx, ry, rw, rh); c.clip();
      c.fillStyle = "#05080e"; c.fillRect(rx, ry, rw, rh);
      // grid
      const g = 100, x0 = cam.x - rw / 2 / z, y0 = cam.y - rh / 2 / z;
      c.strokeStyle = "rgba(60,90,140,.13)"; c.lineWidth = 1; c.beginPath();
      for (let x = Math.floor(x0 / g) * g; x < x0 + rw / z + g; x += g) { const X = cx + (x - cam.x) * z; c.moveTo(X, ry); c.lineTo(X, ry + rh); }
      for (let y = Math.floor(y0 / g) * g; y < y0 + rh / z + g; y += g) { const Y = cy + (y - cam.y) * z; c.moveTo(rx, Y); c.lineTo(rx + rw, Y); }
      c.stroke();
      if (!view.wrap && view.R) { // the deadly edge of a round world
        c.strokeStyle = "rgba(255,92,122,.75)"; c.lineWidth = 3 * (this.dpr || 1);
        c.beginPath(); c.arc(cx - cam.x * z, cy - cam.y * z, view.R * z, 0, Math.PI * 2); c.stroke();
      }
      // food
      const F = view.food || [];
      const pad = 20;
      for (let i = 0; i + 2 < F.length; i += 3) {
        const X = sx(F[i]), Y = sy(F[i + 1]);
        if (X < rx - pad || X > rx + rw + pad || Y < ry - pad || Y > ry + rh + pad) continue;
        const r = Math.max(1.2, Math.min(6, 1.4 + 1.2 * Math.sqrt(F[i + 2]))) * z;
        c.fillStyle = `hsl(${(i * 37) % 360} 90% 65%)`;
        c.beginPath(); c.arc(X, Y, r, 0, Math.PI * 2); c.fill();
      }
      // snakes, NPCs first so the AI is drawn on top
      const order = (view.snakes || []).slice().sort((a, b) => (a.k === "b") - (b.k === "b")).reverse();
      for (const s of order) this.snake(s, sx, sy, z, rect);
      if (opts.senses) this.senses(opts.senses, sx, sy, z);
      if (opts.labels) for (const s of view.snakes) if (s.label && s.b.length) {
        c.font = `${Math.round(12 * (this.dpr || 1))}px Segoe UI, sans-serif`; c.fillStyle = "rgba(230,237,247,.9)"; c.textAlign = "center";
        c.fillText(s.label, sx(s.b[0]), sy(s.b[1]) - (radiusOf(s.n) * z + 10 * (this.dpr || 1)));
      }
      c.restore();
    }
    snake(s, sx, sy, z, rect) {
      const c = this.ctx, b = s.b;
      if (!b || b.length < 4) return;
      const r = radiusOf(s.n) * z, col = colorFor(s);
      c.lineCap = "round"; c.lineJoin = "round";
      // outline glow then body, drawn tail to head; break the line at the edge of a no-edge world
      for (let pass = 0; pass < 2; pass++) {
        c.strokeStyle = pass ? col.body : col.glow;
        c.lineWidth = pass ? 2 * r : 2 * r + 6 * (this.dpr || 1);
        c.beginPath();
        let px = null, py = null;
        for (let i = b.length - 2; i >= 0; i -= 2) {
          const X = sx(b[i]), Y = sy(b[i + 1]);
          if (px === null || Math.abs(X - px) > 300 * z || Math.abs(Y - py) > 300 * z) c.moveTo(X, Y); else c.lineTo(X, Y);
          px = X; py = Y;
        }
        c.stroke();
      }
      // eyes
      const hx = sx(b[0]), hy = sy(b[1]);
      let hd = s.h;
      if (hd === undefined && b.length >= 4) hd = Math.atan2(b[1] - b[3], b[0] - b[2]);
      const ex = Math.cos(hd), ey = Math.sin(hd), px = -ey, py = ex;
      for (const side of [-1, 1]) {
        const X = hx + ex * r * 0.35 + px * side * r * 0.5, Y = hy + ey * r * 0.35 + py * side * r * 0.5;
        c.fillStyle = "#fff"; c.beginPath(); c.arc(X, Y, r * 0.32, 0, Math.PI * 2); c.fill();
        c.fillStyle = "#111"; c.beginPath(); c.arc(X + ex * r * 0.12, Y + ey * r * 0.12, r * 0.16, 0, Math.PI * 2); c.fill();
      }
    }
    // the 16 directions the followed snake senses: green = food, red = danger
    senses(o, sx, sy, z) {
      const c = this.ctx, K = 16, R = o.view * z, hx = sx(o.x), hy = sy(o.y);
      for (let k = 0; k < K; k++) {
        const a = o.heading + k * Math.PI * 2 / K, w = Math.PI / K;
        const food = o.obs[k], danger = Math.max(o.obs[K + k], o.obs[3 * K + k]);
        if (danger > 0.02) {
          c.fillStyle = `rgba(255,70,100,${0.05 + 0.33 * danger})`;
          c.beginPath(); c.moveTo(hx, hy); c.arc(hx, hy, R * (0.25 + 0.75 * danger), a - w, a + w); c.closePath(); c.fill();
        }
        if (food > 0.02) {
          c.fillStyle = `rgba(57,255,158,${0.04 + 0.26 * food})`;
          c.beginPath(); c.moveTo(hx, hy); c.arc(hx, hy, R * (0.2 + 0.8 * food), a - w * 0.6, a + w * 0.6); c.closePath(); c.fill();
        }
      }
      c.strokeStyle = "rgba(42,216,255,.25)"; c.lineWidth = 1;
      c.beginPath(); c.arc(hx, hy, R, 0, Math.PI * 2); c.stroke();
    }
  }

  // ------------------------------------------------------------------ one game on screen
  function npcLife(v) {
    if (!v) return 0;
    if (Array.isArray(v)) return v;
    const p = String(v).split(",").map((x) => parseInt(x, 10));
    return p.length === 2 && p[0] > 0 && p[1] > p[0] ? p : 0;
  }

  class Match {
    // world: brain info (wrap, half_size, world_radius, view_radius, food_count, food_value, npc_life, bot_view)
    // slots: [{brain, k:'a'|'h'|'t', c, label}]; one snake per slot, plus nBots scripted NPCs
    constructor(world, slots, nBots, seed) {
      const cfg = {
        wrap: !!world.wrap, half_size: world.half_size || 1500, world_radius: world.world_radius || 1400,
        view_radius: world.bot_view || 420, agent_view: world.view_radius || 420,
        food_count: world.food_count || 900, food_value: world.food_value || 2, npc_life: npcLife(world.npc_life),
      };
      this.world = world; this.slots = slots; this.seed = seed || 1;
      this.arena = new SA.Arena(slots.length, nBots, this.seed, cfg);
      this.rand = SA.mulberry32(this.seed * 7 + 3);
      this.tick = 0; this.dead = new Array(slots.length).fill(0);
      this.stats = slots.map(() => ({ best: 0, kills: 0, deaths: 0, score: 0 }));
      this.follow = 0; this.lastObs = null; this.lastProbs = null;
      this.human = { turn: 0, boost: false, target: null };
      this.listeners = [];
      this.frames = []; this.pending = []; this.cool = { close: 0, kill: 0, record: 0 };
      this.recordBest = 0; this.recordGen = null; this.highlights = false; this.near = new Map();
    }
    setBrain(brain) { for (const s of this.slots) if (s.k === "a") s.brain = brain; }
    leader() {
      let best = -1, bl = -1;
      this.arena.snakes.forEach((s, i) => { if (i < this.slots.length && s.alive && this.slots[i].k !== "h" && s.body.length > bl) { bl = s.body.length; best = i; } });
      const cur = this.arena.snakes[this.follow]; // keep following the same snake unless another is clearly longer
      if (cur && cur.alive && this.slots[this.follow] && this.slots[this.follow].k !== "h" && cur.body.length >= bl * 0.85) return this.follow;
      return best;
    }
    humanAction(s) {
      const H = this.human;
      let turn = H.turn;
      if (H.target) {
        const want = Math.atan2(H.target[1], H.target[0]);
        let d = want - s.heading; d = Math.atan2(Math.sin(d), Math.cos(d));
        turn = Math.abs(d) < 0.08 ? 0 : d > 0 ? 1 : -1;
      }
      return [[-1, 0], [0, 0], [1, 0], [-1, 1], [0, 1], [1, 1]].findIndex((a) => a[0] === turn && a[1] === (H.boost ? 1 : 0));
    }
    step() {
      const A = this.arena, n = this.slots.length, actions = new Array(n).fill(1);
      for (let i = 0; i < n; i++) {
        const s = A.snakes[i];
        if (!s.alive) continue;
        const slot = this.slots[i];
        if (slot.k === "h") { actions[i] = this.humanAction(s); continue; }
        if (!slot.brain) continue;
        const obs = A.observe(i);
        const z = slot.brain.logits(obs);
        let m = -Infinity; for (const v of z) m = Math.max(m, v);
        const p = Array.from(z, (v) => Math.exp(v - m)); const sum = p.reduce((a, b) => a + b, 0);
        let r = this.rand() * sum, a = p.length - 1;
        for (let k = 0; k < p.length; k++) { r -= p[k]; if (r <= 0) { a = k; break; } }
        actions[i] = a;
        if (i === this.follow) { this.lastObs = obs; this.lastProbs = p.map((v) => v / sum); this.lastAction = a; }
        if (this.highlights) this.watchClose(i, obs);
      }
      const before = A.snakes.map((s) => s.kills);
      const lens = A.snakes.map((s) => s.body.length);
      const res = A.step(actions);
      this.tick++;
      for (let i = 0; i < n; i++) {
        const s = A.snakes[i], st = this.stats[i];
        if (s.alive) { st.best = Math.max(st.best, s.body.length); }
        if (res.died[i]) { st.deaths++; this.dead[i] = this.tick; this.emit({ kind: "death", i, len: lens[i] }); }
        if (s.kills > before[i]) {
          const victim = res.died.findIndex((d, j) => d && res.killer[j] === i);
          st.kills += s.kills - before[i];
          this.emit({ kind: "kill", i, victimLen: victim >= 0 ? lens[victim] : 0 });
          if (this.highlights && i === this.follow && victim >= 0 && lens[victim] >= 60 && this.tick > this.cool.kill) {
            this.cool.kill = this.tick + 900;
            this.mark("kill", `Took down a snake of length ${lens[victim]}`);
          }
        }
        if (!s.alive && this.tick - this.dead[i] > 45) A.respawn(i); // back in the game after 1.5 s
      }
      if (this.highlights) this.watchRecord();
      if (this.highlights && this.tick % FRAME_EVERY === 0) this.captureFrame();
    }
    emit(e) { for (const f of this.listeners) f(e); }
    // ---------------- highlights: noticed while the newest brain plays
    watchClose(i, obs) {
      if (i !== this.follow) return;
      const K = 16, ahead = Math.max(obs[K], obs[K + 1], obs[2 * K - 1]);
      if (ahead > 0.965 && this.tick > this.cool.close && !this.near.has(i)) this.near.set(i, this.tick);
      for (const [j, t0] of this.near) {
        if (!this.arena.snakes[j].alive) { this.near.delete(j); continue; }
        if (this.tick - t0 >= 45) { // it got that close and lived
          this.near.delete(j);
          this.cool.close = this.tick + 1200;
          this.mark("close", "Close call! It dodged at the last moment");
        }
      }
    }
    watchRecord() {
      const i = this.follow;
      if (i < 0) return;
      const s = this.arena.snakes[i];
      if (!s || !s.alive || this.slots[i].k !== "a") return;
      if (this.recordBest > 0 && s.body.length > this.recordBest && this.tick > this.cool.record) {
        this.recordBest = s.body.length;
        this.cool.record = this.tick + 1800;
        this.mark("record", `New record: length ${s.body.length}`);
      } else if (s.body.length > this.recordBest && this.recordBest === 0 && this.tick > 300) {
        this.recordBest = s.body.length; // first look: remember where we are, no banner
      }
    }
    mark(kind, label) {
      this.pending.push({ kind, label, at: this.frames.length, due: this.tick + HL_AFTER, gen: this.recordGen });
    }
    captureFrame() {
      const f = this.frame(this.follow, 1300);
      this.frames.push(f);
      if (this.frames.length > HL_BEFORE + HL_AFTER + 10 && !this.pending.length) this.frames.splice(0, this.frames.length - HL_BEFORE);
      while (this.pending.length && this.tick >= this.pending[0].due) {
        const p = this.pending.shift();
        const start = Math.max(0, p.at - HL_BEFORE / FRAME_EVERY * FRAME_EVERY);
        const clip = { kind: p.kind, label: p.label, generation: p.gen, fps: TICKS_PER_SEC / FRAME_EVERY,
                       wrap: this.arena.wrap, L: this.arena.L || 0, R: this.arena.cfg.world_radius, frames: this.frames.slice(start) };
        this.emit({ kind: "highlight", clip });
      }
    }
    // A picture of the world (same format as the trainer's live picture). Cropped to `crop` around snake i if given.
    frame(i, crop) {
      const A = this.arena, out = [];
      const S = A.wrap ? A.S : 0, wd = (d) => (S ? d - S * Math.round(d / S) : d);
      const cs = i >= 0 && A.snakes[i] && A.snakes[i].alive ? A.snakes[i].body[0] : null;
      const near = (x, y) => !crop || !cs || (Math.abs(wd(x - cs[0])) < crop && Math.abs(wd(y - cs[1])) < crop * 0.7);
      A.snakes.forEach((s, j) => {
        if (!s.alive) return;
        if (crop && cs && !s.body.some((p, q) => q % 6 === 0 && near(p[0], p[1]))) return;
        const b = [];
        const step = crop ? 2 : 1;
        for (let q = 0; q < s.body.length; q += step) b.push(Math.round(s.body[q][0]), Math.round(s.body[q][1]));
        const slot = this.slots[j];
        out.push({ k: slot ? slot.k : "b", c: slot ? (slot.c ?? j) : j, n: s.body.length, h: +s.heading.toFixed(2), b,
                   label: slot && slot.label ? slot.label : undefined });
      });
      const food = [];
      for (const f of A.food) if (near(f[0], f[1])) food.push(Math.round(f[0]), Math.round(f[1]), +f[2].toFixed(1));
      return { wrap: A.wrap, L: A.L || 0, R: A.cfg.world_radius, snakes: out, food };
    }
    senses() {
      const i = this.follow, s = this.arena.snakes[i];
      if (i < 0 || !s || !s.alive || !this.lastObs) return null;
      return { x: s.body[0][0], y: s.body[0][1], heading: s.heading, view: s.view || this.arena.cfg.view_radius, obs: this.lastObs };
    }
  }

  // ------------------------------------------------------------------ camera
  class Camera {
    constructor() { this.x = 0; this.y = 0; this.zoom = 0.6; this.tx = 0; this.ty = 0; this.tz = 0.6; }
    aim(x, y, n, widthPx, wrapS) {
      if (wrapS) { // move the short way round a no-edge world
        const dx = x - this.x - wrapS * Math.round((x - this.x) / wrapS), dy = y - this.y - wrapS * Math.round((y - this.y) / wrapS);
        this.tx = this.x + dx; this.ty = this.y + dy;
      } else { this.tx = x; this.ty = y; }
      const span = 1100 + Math.min(1400, 3 * (n || 12));
      this.tz = Math.max(0.15, widthPx / span);
    }
    update(dt, wrapS) {
      const k = 1 - Math.exp(-dt * 4);
      this.x += (this.tx - this.x) * k; this.y += (this.ty - this.y) * k; this.zoom += (this.tz - this.zoom) * k * 0.5;
      if (wrapS) { const L = wrapS / 2; this.x = this.x - wrapS * Math.floor((this.x + L) / wrapS); this.y = this.y - wrapS * Math.floor((this.y + L) / wrapS); }
    }
    snap() { this.x = this.tx; this.y = this.ty; this.zoom = this.tz; }
  }

  // ------------------------------------------------------------------ recording
  class Recorder {
    constructor(canvas) { this.cv = canvas; this.rec = null; }
    static pickType() {
      const types = ["video/mp4;codecs=avc1.42E01E", "video/mp4;codecs=avc1", "video/mp4", "video/webm;codecs=vp9", "video/webm;codecs=vp8", "video/webm"];
      for (const t of types) if (window.MediaRecorder && MediaRecorder.isTypeSupported(t)) return t;
      return "";
    }
    get on() { return !!this.rec; }
    start() {
      const type = Recorder.pickType();
      if (!type) throw new Error("This window can't record video.");
      const stream = this.cv.captureStream(30);
      this.chunks = []; this.type = type;
      this.rec = new MediaRecorder(stream, { mimeType: type, videoBitsPerSecond: 8_000_000 });
      this.rec.ondataavailable = (e) => { if (e.data && e.data.size) this.chunks.push(e.data); };
      this.rec.start(1000);
    }
    stop() {
      return new Promise((res) => {
        if (!this.rec) return res(null);
        const r = this.rec; this.rec = null;
        r.onstop = () => res({ blob: new Blob(this.chunks, { type: this.type.split(";")[0] }), ext: this.type.startsWith("video/mp4") ? "mp4" : "webm" });
        r.stop();
      });
    }
  }

  // live "inside training" picture from the trainer -> drawable view
  function viewFromSnapshot(snap) {
    if (!snap || !snap.snakes) return null;
    let c = 0;
    return { wrap: !!snap.wrap, L: snap.L || 0, R: snap.R || 0, food: snap.food || [],
             snakes: snap.snakes.map((s) => ({ k: s.k === "a" ? "a" : s.k === "l" ? "l" : "b", c: c++, n: s.n * 1 || 12, h: s.h, b: s.b || [] })) };
  }

  root.SB = { Renderer, Match, Camera, Recorder, viewFromSnapshot, colorFor, ACTION_NAMES, TICKS_PER_SEC, radiusOf };
})(window);
