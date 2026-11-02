// SnekBrain window: talks to the local engine, runs the on-screen games, and all the panels.
(function () {
  "use strict";
  const $ = (s) => document.querySelector(s);
  const $$ = (s) => Array.from(document.querySelectorAll(s));
  // tiny safe element builder (text is always set as text, never as HTML)
  function h(tag, attrs, ...kids) {
    const e = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === undefined || v === null || v === false) continue;
      if (k === "class") e.className = v;
      else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
      else if (k === "style") e.style.cssText = v;
      else if (v === true) e.setAttribute(k, "");
      else e.setAttribute(k, String(v));
    }
    for (const c of kids.flat()) if (c !== null && c !== undefined && c !== false) e.append(c instanceof Node ? c : document.createTextNode(String(c)));
    return e;
  }
  const fmt = (n) => (n === null || n === undefined || isNaN(n) ? "–" : n >= 1e9 ? (n / 1e9).toFixed(2) + " billion" : n >= 1e6 ? (n / 1e6).toFixed(1) + " million" : n >= 1e4 ? Math.round(n / 1e3) + "k" : Math.round(n).toLocaleString());
  const ago = (t) => { const s = Date.now() / 1000 - t; return s < 60 ? "just now" : s < 3600 ? Math.round(s / 60) + " min ago" : s < 86400 ? Math.round(s / 3600) + " h ago" : Math.round(s / 86400) + " days ago"; };

  // ------------------------------------------------------------------ engine API
  async function api(method, path, body, raw) {
    const opt = { method, headers: { "X-SL-Token": window.SL_TOKEN } };
    if (body !== undefined) {
      if (raw) { opt.body = body; } else { opt.body = JSON.stringify(body); opt.headers["Content-Type"] = "application/json"; }
    }
    const r = await fetch("/api/" + path, opt);
    const ct = r.headers.get("Content-Type") || "";
    const data = ct.includes("json") ? await r.json() : await r.blob();
    if (!r.ok) throw new Error((data && data.error) || r.statusText);
    return data;
  }
  const GET = (p) => api("GET", p), POST = (p, b) => api("POST", p, b || {});

  function toast(msg, bad) {
    const t = $("#toast"); t.textContent = msg; t.className = "toast show" + (bad ? " bad" : "");
    clearTimeout(toast.t); toast.t = setTimeout(() => (t.className = "toast"), bad ? 6000 : 3000);
  }
  async function guard(fn) { try { return await fn(); } catch (e) { toast(e.message || String(e), true); } }

  // ------------------------------------------------------------------ state
  const st = {
    status: null, brains: [], active: null, brain: null, world: null, liveBrain: null, liveV: null, gen0: null,
    view: "live", speed: 1, rows: [], hall: [], events: [], settings: null, schema: null,
    match: null, matchB: null, cam: new SB.Camera(), camB: new SB.Camera(), insideView: null,
    tour: null, replay: null, recorder: null, busy: false, saver: false, stream: false,
  };
  const R = new SB.Renderer($("#cv"));

  // ------------------------------------------------------------------ games on screen
  function showcaseCounts() {
    const w = st.world || {};
    return { agents: Math.max(1, Math.min(8, w.agents || 6)), bots: Math.max(0, Math.min(12, w.bots ?? 6)) };
  }
  function newLiveMatch() {
    if (!st.world) return;
    const { agents, bots } = showcaseCounts();
    const slots = Array.from({ length: agents }, (_, i) => ({ k: "a", c: i, brain: st.liveBrain }));
    st.match = new SB.Match(st.world, slots, bots, (Date.now() & 0xffff) + 1);
    st.match.highlights = true;
    st.match.recordGen = st.liveBrain ? st.liveBrain.gen : null;
    st.match.recordBest = st.hlBest || 0;
    st.match.listeners.push(onMatchEvent);
    st.cam.snap();
  }
  function newCompare() {
    if (!st.world || !st.gen0 || !st.liveBrain) return;
    const { agents, bots } = showcaseCounts(), seed = (Date.now() & 0xffff) + 1;
    const mk = (b) => new SB.Match(st.world, Array.from({ length: agents }, (_, i) => ({ k: "a", c: i, brain: b })), bots, seed);
    st.match = mk(st.gen0); st.matchB = mk(st.liveBrain);
  }
  function newPlay() {
    if (!st.world) return;
    const { agents, bots } = showcaseCounts();
    const slots = [{ k: "h", c: 0, label: "You" }].concat(Array.from({ length: Math.max(1, agents - 1) }, (_, i) => ({ k: "a", c: i + 1, brain: st.liveBrain })));
    st.match = new SB.Match(st.world, slots, bots, (Date.now() & 0xffff) + 1);
    st.match.follow = 0;
    st.playDeaths = 0;
    st.match.listeners.push((e) => { if (e.kind === "death" && e.i === 0) { st.playDeaths++; flash(`You were eaten at length ${e.len}! Back in 1.5 s`); } });
    st.cam.snap();
  }
  function onMatchEvent(e) {
    if (e.kind === "highlight" && st.active) {
      const clip = e.clip;
      if (clip.kind === "record") st.hlBest = Math.max(st.hlBest || 0, parseInt((clip.label.match(/\d+/) || [0])[0], 10));
      banner(`${clip.generation !== null && clip.generation !== undefined ? "Gen " + Math.floor(clip.generation) + " · " : ""}${clip.label}`);
      POST(`brain/${st.active}/highlights`, clip).then(() => { if (st.view === "highlights") renderHighlights(); }).catch(() => {});
    }
  }
  function flash(msg) { const f = $("#flash"); f.textContent = msg; f.classList.add("show"); clearTimeout(flash.t); flash.t = setTimeout(() => f.classList.remove("show"), 2600); }
  function banner(msg, ms) { const b = $("#banner"); b.textContent = msg; b.classList.add("show"); clearTimeout(banner.t); banner.t = setTimeout(() => b.classList.remove("show"), ms || 4500); }

  // ------------------------------------------------------------------ main loop
  let lastT = performance.now(), acc = 0;
  function frame(now) {
    requestAnimationFrame(frame);
    const dt = Math.min(0.1, (now - lastT) / 1000); lastT = now;
    R.resize(); R.clear();
    const W = R.cv.width, H = R.cv.height;
    try {
      if (st.replay) return drawReplay(dt, W, H);
      if (st.view === "inside") return drawInside(dt, W, H);
      const m = st.match; if (!m) return drawEmpty(W, H);
      acc += dt * SB.TICKS_PER_SEC * (st.view === "play" ? 1 : st.speed);
      const t0 = performance.now();
      let n = 0;
      while (acc >= 1 && performance.now() - t0 < 13) { // never freeze the window; at Max it just runs as fast as it can
        m.step(); if (st.matchB) st.matchB.step(); acc -= 1; n++;
        if (st.tour) tournamentTick();
      }
      if (acc > 8) acc = 8;
      if (st.view === "compare" && st.matchB) {
        drawMatch(st.match, st.cam, [0, 0, W / 2 - 2, H], dt, "Generation 0 (never trained)");
        drawMatch(st.matchB, st.camB, [W / 2 + 2, 0, W / 2 - 2, H], dt, `Generation ${genText(st.liveBrain)} (newest)`);
        R.ctx.fillStyle = "#233049"; R.ctx.fillRect(W / 2 - 2, 0, 4, H);
      } else drawMatch(m, st.cam, [0, 0, W, H], dt, null);
      if (n && (frame.k = (frame.k || 0) + 1) % 6 === 0) drawBrainPanel(m);
    } catch (e) { console.error(e); }
  }
  function drawEmpty(W, H) {
    const c = R.ctx; c.fillStyle = "#8b99b3"; c.font = `${16 * R.dpr}px Segoe UI`; c.textAlign = "center";
    c.fillText(st.view === "compare" ? "Before/after shows up once it has trained one generation." : "Getting the brain ready…", W / 2, H / 2);
  }
  function drawMatch(m, cam, rect, dt, label) {
    const A = m.arena;
    if ($("#chkFollow").checked || st.view === "play") {
      const lead = st.view === "play" ? 0 : m.leader();
      const changed = lead !== m.follow;
      if (st.view !== "play") m.follow = lead;
      const s = A.snakes[lead];
      if (changed && s && s.alive) { cam.aim(s.body[0][0], s.body[0][1], s.body.length, rect[2] / R.dpr, A.wrap ? A.S : 0); cam.snap(); }
      if (s && s.alive) cam.aim(s.body[0][0], s.body[0][1], s.body.length, rect[2] / R.dpr, A.wrap ? A.S : 0);
    } else cam.aim(0, 0, 900, rect[2] / R.dpr, 0);
    cam.update(dt, A.wrap ? A.S : 0);
    const view = m.frame(-1, 0);
    R.draw(view, cam, rect, { senses: $("#chkSenses").checked && st.view !== "play" ? m.senses() : null, labels: st.view === "tournament" || st.view === "play" });
    if (label) {
      const c = R.ctx; c.font = `600 ${14 * R.dpr}px Segoe UI`; c.fillStyle = "rgba(230,237,247,.9)"; c.textAlign = "center";
      c.fillText(label, rect[0] + rect[2] / 2, rect[1] + 26 * R.dpr);
    }
  }
  function drawInside(dt, W, H) {
    const v = st.insideView;
    if (!v) { const c = R.ctx; c.fillStyle = "#8b99b3"; c.font = `${16 * R.dpr}px Segoe UI`; c.textAlign = "center";
      c.fillText(st.status && st.status.running ? "Waiting for a picture from training…" : "Start training to look inside.", W / 2, H / 2); return; }
    let lead = null;
    for (const s of v.snakes) if (s.k === "a" && (!lead || s.n > lead.n)) lead = s;
    if (lead && lead.b.length) st.cam.aim(lead.b[0], lead.b[1], lead.n, W / R.dpr, v.wrap ? 2 * v.L : 0);
    st.cam.update(dt, v.wrap ? 2 * v.L : 0);
    R.draw(v, st.cam, [0, 0, W, H], {});
  }
  function genText(b) { if (!b || b.gen === null || b.gen === undefined) return "?"; return String(Math.floor(Number(b.gen))); } // the live brain sits between saved generations (110.9): show the generation it grew from

  // ------------------------------------------------------------------ brain panel (what it senses + its choice)
  function drawBrainPanel(m) {
    const cv = $("#senses"), c = cv.getContext("2d"), W = cv.width, H = cv.height;
    c.clearRect(0, 0, W, H);
    const o = m.lastObs, p = m.lastProbs;
    if (!o) return;
    const K = 16, cx = W / 2, cy = H / 2 + 4, R0 = 14, R1 = H / 2 - 8;
    c.strokeStyle = "#233049"; c.beginPath(); c.arc(cx, cy, R1, 0, Math.PI * 2); c.stroke();
    for (let k = 0; k < K; k++) {
      const a = -Math.PI / 2 + k * Math.PI * 2 / K, w = Math.PI / K * 0.9; // up = straight ahead
      const food = o[k], danger = Math.max(o[K + k], o[3 * K + k]), heads = o[2 * K + k];
      if (danger > 0.01) { c.fillStyle = `rgba(255,70,100,${0.25 + 0.6 * danger})`; c.beginPath(); c.arc(cx, cy, R0 + (R1 - R0) * danger, a - w, a + w); c.arc(cx, cy, R0, a + w, a - w, true); c.fill(); }
      if (food > 0.01) { c.fillStyle = `rgba(57,255,158,${0.3 + 0.6 * food})`; c.beginPath(); c.arc(cx, cy, R0 + (R1 - R0) * food * 0.9, a - w * 0.5, a + w * 0.5); c.arc(cx, cy, R0, a + w * 0.5, a - w * 0.5, true); c.fill(); }
      if (Math.abs(heads) > 0.05) { c.fillStyle = heads > 0 ? "#ffb547" : "#2ad8ff"; c.beginPath(); c.arc(cx + Math.cos(a) * (R0 + (R1 - R0) * Math.abs(heads)), cy + Math.sin(a) * (R0 + (R1 - R0) * Math.abs(heads)), 3, 0, Math.PI * 2); c.fill(); }
    }
    c.fillStyle = "#39ff9e"; c.beginPath(); c.moveTo(cx, cy - 9); c.lineTo(cx - 6, cy + 6); c.lineTo(cx + 6, cy + 6); c.closePath(); c.fill();
    c.fillStyle = "#8b99b3"; c.font = "11px Segoe UI"; c.textAlign = "left";
    c.fillText("green: food", 4, 12); c.fillText("red: danger", 4, 25); c.fillText("orange dot: bigger snake", 4, 38);
    c.textAlign = "right"; c.fillText("up = where it's heading", W - 4, 12);
    const box = $("#actions"); box.replaceChildren();
    if (!p) return;
    const best = p.indexOf(Math.max(...p));
    SB.ACTION_NAMES.forEach((name, i) => {
      box.append(h("div", { class: i === m.lastAction ? "pick" : "" }, name),
        h("div", { class: "bar" }, h("i", { style: `width:${(p[i] * 100).toFixed(1)}%` })),
        h("div", { class: i === best ? "pick" : "" }, Math.round(p[i] * 100) + "%"));
    });
  }

  // ------------------------------------------------------------------ polling the engine
  async function pollStatus() {
    try {
      const s = await GET("status");
      st.status = s;
      if (s.active !== st.active) await loadBrain(s.active);
      renderStatus();
    } catch (e) { $("#statePill").textContent = "Engine not answering…"; $("#statePill").className = "pill error"; }
  }
  async function pollLive() {
    if (!st.active) return;
    try {
      const { v } = await GET(`brain/${st.active}/live-version`);
      if (!v || v === st.liveV) return;
      const json = await GET(`brain/${st.active}/live`);
      const first = !st.liveBrain;
      st.liveBrain = new SA.Brain(json); st.liveV = v;
      if (st.match && st.view === "live") { st.match.setBrain(st.liveBrain); st.match.recordGen = st.liveBrain.gen; }
      if (st.matchB && st.view === "compare") st.matchB.setBrain(st.liveBrain);
      if (st.match && st.view === "play") st.match.setBrain(st.liveBrain);
      if (!first) flash(`Brain upgraded · Gen ${genText(st.liveBrain)}`);
      if (!st.match && st.view === "live") newLiveMatch();
      $("#genBadge").textContent = `Gen ${genText(st.liveBrain)}`;
    } catch (e) { /* not trained yet */ }
  }
  async function pollSlow() {
    if (!st.active) return;
    try {
      const [p, hl, ev] = await Promise.all([GET(`brain/${st.active}/progress`), GET(`brain/${st.active}/hall`), GET(`brain/${st.active}/events?n=40`)]);
      st.rows = p.rows; st.hall = hl.records; st.events = ev.events;
      drawChart(); renderEvents(); renderHappening();
    } catch (e) { /* keep the old picture */ }
  }
  async function pollInside() {
    if (st.view !== "inside" || !st.active) return;
    try { st.insideView = SB.viewFromSnapshot(await GET("live-game")); } catch (e) { st.insideView = null; }
  }

  async function loadBrain(id) {
    st.active = id; st.liveBrain = null; st.liveV = null; st.gen0 = null; st.match = st.matchB = null; st.hlBest = 0;
    if (!id) return;
    const b = await GET(`brain/${id}`);
    st.brain = b; st.world = b.world;
    try { const hls = (await GET(`brain/${id}/highlights`)).highlights; for (const x of hls) if (x.kind === "record") st.hlBest = Math.max(st.hlBest, parseInt(((x.label || "").match(/\d+/) || [0])[0], 10)); } catch (e) { /* none */ }
    await refreshBrains();
    await pollLive();
    if (!st.liveBrain) { // never trained: let generation 0 (random) play once it exists, else a random brain
      st.liveBrain = randomBrain(); st.liveV = null;
    }
    setView(st.view, true);
    pollSlow();
  }
  function randomBrain() {
    // an untrained brain, same shape, so there's always something on screen
    const rnd = SA.mulberry32(42), mk = (i, o, act) => {
      const w = new Float32Array(i * o), b = new Float32Array(o), sc = Math.sqrt(2 / i);
      for (let k = 0; k < w.length; k++) w[k] = (rnd() * 2 - 1) * sc;
      const enc = (a) => { let s = ""; const u = new Uint8Array(a.buffer); for (let k = 0; k < u.length; k++) s += String.fromCharCode(u[k]); return btoa(s); };
      return { in: i, out: o, act, w: enc(w), b: enc(b) };
    };
    return new SA.Brain({ generation: 0, layers: [mk(68, 64, "tanh"), mk(64, 64, "tanh"), mk(64, 6, "linear")] });
  }
  async function refreshBrains() {
    const r = await GET("brains"); st.brains = r.brains;
    const sel = $("#brainSelect"); sel.replaceChildren();
    for (const b of st.brains) sel.append(h("option", { value: b.id, selected: b.id === st.active }, `${b.name} · gen ${b.generation}`));
  }

  // ------------------------------------------------------------------ status, happening, chart, events
  function renderStatus() {
    const s = st.status, pill = $("#statePill");
    const words = { training: "Training", starting: "Starting…", paused: "Paused", waiting_video: "Waiting for your video", paused_closed: "Paused (window closed)", error: "Stopped by a problem", stopped: "Stopped" };
    pill.textContent = (words[s.state] || s.state) + (s.state === "training" && s.mode === "turbo" ? " · Turbo" : "");
    pill.className = "pill " + (s.state === "training" ? "training" : s.state === "waiting_video" ? "waiting" : s.state === "error" ? "error" : "");
    pill.title = s.video ? `Paused while ${s.video} plays. The game on screen keeps going.` : s.error || "";
    $("#btnTrain").textContent = s.running ? "Pause training" : "Start training";
    $$("#modeSeg button").forEach((b) => b.classList.toggle("on", b.dataset.mode === s.mode));
    renderHappening();
    renderStream();
  }
  function renderHappening() {
    const s = st.status, box = $("#happening");
    if (!s) return;
    const last = st.rows[st.rows.length - 1], first = st.rows[0];
    const gen = s.generation ?? (last ? last.generation : 0);
    const lines = [h("div", { class: "big" }, `Generation ${gen ?? 0}`)];
    const L = (label, value) => h("div", { class: "line" }, label + " ", h("b", {}, value));
    lines.push(L("Games played:", fmt(s.steps ?? (last ? last.timesteps : 0)) + " moves"));
    if (s.running) lines.push(L("Learning speed:", s.sps ? fmt(s.sps) + " moves/second" : "warming up"));
    if (last) lines.push(L("Average length now:", Math.round(last.avg_max_len)));
    const prog = (st.brain && st.brain.progress) || {};
    lines.push(L("World level:", String((s.stage ?? prog.stage ?? 0) + 1)));
    let say = "";
    if (!s.running) say = st.rows.length ? "Paused. Press Start training and it carries on exactly where it stopped." : "Brand new brain: it knows nothing yet. Press Start training and watch it figure the game out by itself.";
    else if (s.state === "waiting_video") say = "Your video comes first, so learning waits. The snake on screen keeps playing.";
    else if (first && last && st.rows.length > 3) {
      const g = last.avg_max_len - first.avg_max_len;
      say = g > 5 ? `Since generation ${first.generation} its snakes grew from ${Math.round(first.avg_max_len)} to ${Math.round(last.avg_max_len)} long on average. Nobody told it how.` : "Still figuring things out. Early on it mostly learns not to die.";
    } else say = "It's playing thousands of games in the background. Every few seconds the snake on screen gets the newer brain.";
    if (s.exploring) say += " Right now it's trying more new moves, because it stopped improving for a while.";
    if (s.device === "cuda") say += " Training on the graphics card.";
    lines.push(h("div", { class: "say" }, say));
    box.replaceChildren(...lines);
    $("#chartHint").textContent = st.rows.length > 1 ? `(${st.rows.length} generations)` : "";
  }
  function drawChart(cv, rows) {
    cv = cv || $("#chart"); rows = rows || st.rows;
    const dpr = Math.min(2, window.devicePixelRatio || 1), W = cv.clientWidth * dpr || 300, H = (cv.clientHeight || 150) * dpr;
    cv.width = W; cv.height = H;
    const c = cv.getContext("2d"); c.clearRect(0, 0, W, H);
    if (!rows || rows.length < 2) { c.fillStyle = "#8b99b3"; c.font = `${12 * dpr}px Segoe UI`; c.fillText("The chart fills in as generations finish.", 8 * dpr, H / 2); return; }
    const pad = 6 * dpr, xs = rows.map((r) => r.generation), x0 = Math.min(...xs), x1 = Math.max(...xs);
    const line = (key, color) => {
      const vs = rows.map((r) => +r[key]), lo = Math.min(...vs), hi = Math.max(...vs) || 1;
      c.strokeStyle = color; c.lineWidth = 2 * dpr; c.beginPath();
      rows.forEach((r, i) => { const X = pad + (r.generation - x0) / Math.max(1, x1 - x0) * (W - 2 * pad), Y = H - pad - (vs[i] - lo) / Math.max(1e-9, hi - lo) * (H - 2 * pad); i ? c.lineTo(X, Y) : c.moveTo(X, Y); });
      c.stroke();
      return { lo, hi, vs };
    };
    line("avg_return", "rgba(42,216,255,.8)");
    const L = line("avg_max_len", "#39ff9e");
    c.fillStyle = "#ffd84a"; c.font = `${11 * dpr}px Segoe UI`;
    const hallGens = new Set((st.hall || []).filter((r) => r.kind === "length").map((r) => r.generation));
    rows.forEach((r, i) => { if (hallGens.has(r.generation)) { const X = pad + (r.generation - x0) / Math.max(1, x1 - x0) * (W - 2 * pad), Y = H - pad - (L.vs[i] - L.lo) / Math.max(1e-9, L.hi - L.lo) * (H - 2 * pad); c.fillText("★", X - 4 * dpr, Y - 4 * dpr); } });
    c.fillStyle = "#8b99b3"; c.fillText(`gen ${x0}`, pad, H - 2); c.textAlign = "right"; c.fillText(`gen ${x1}`, W - pad, H - 2); c.textAlign = "left";
  }
  function renderEvents() {
    const ul = $("#events"); ul.replaceChildren();
    const ev = (st.events || []).slice().reverse();
    if (!ev.length) ul.append(h("li", {}, "Records, level ups and other news show up here."));
    for (const e of ev.slice(0, 30)) ul.append(h("li", { class: e.kind }, h("b", {}, e.text), ` · ${e.generation !== null && e.generation !== undefined ? "gen " + e.generation + ", " : ""}${ago(e.t)}`));
  }

  // ------------------------------------------------------------------ views
  function setView(v, force) {
    if (st.view === v && !force) return;
    st.view = v; st.replay = null;
    if (st.tour && v !== "tournament") st.tour = null;
    $$("#views button").forEach((b) => b.classList.toggle("on", b.dataset.view === v));
    $("#playHelp").classList.toggle("hidden", v !== "play");
    $("#tourPanel").classList.toggle("hidden", v !== "tournament");
    $("#hlPanel").classList.toggle("hidden", v !== "highlights");
    st.match = st.matchB = null;
    const note = { live: "The newest brain, playing live", compare: "Same game, same start: untrained vs trained", inside: "A real training game, right now", play: "You are the gold snake", tournament: "Saved brains against each other", highlights: "Moments it saved by itself" }[v];
    $("#viewNote").textContent = note || "";
    if (v === "live") newLiveMatch();
    else if (v === "compare") loadGen0().then(newCompare);
    else if (v === "play") newPlay();
    else if (v === "tournament") renderTournament();
    else if (v === "highlights") { newLiveMatch(); renderHighlights(); }
  }
  async function loadGen0() {
    if (st.gen0 || !st.active) return;
    try { st.gen0 = new SA.Brain(await GET(`brain/${st.active}/gen/0`)); } catch (e) { st.gen0 = null; }
  }

  // ------------------------------------------------------------------ play against it
  const keys = {};
  $("#cv").addEventListener("mousemove", (e) => {
    if (st.view !== "play" || !st.match) return;
    const r = $("#cv").getBoundingClientRect();
    st.match.human.target = [e.clientX - r.left - r.width / 2, e.clientY - r.top - r.height / 2];
  });
  $("#cv").addEventListener("mousedown", () => { if (st.match) st.match.human.boost = st.view === "play"; });
  window.addEventListener("mouseup", () => { if (st.match) st.match.human.boost = false; });
  window.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && (st.saver || st.stream)) { exitShow(); return; }
    if (st.view !== "play" || !st.match || e.target.tagName === "INPUT") return;
    keys[e.key] = true;
    if (["ArrowLeft", "ArrowRight", "ArrowUp", " ", "a", "d", "w"].includes(e.key)) { e.preventDefault(); st.match.human.target = null; }
    applyKeys();
  });
  window.addEventListener("keyup", (e) => { keys[e.key] = false; applyKeys(); });
  function applyKeys() {
    if (!st.match) return;
    const H = st.match.human, l = keys.ArrowLeft || keys.a, r = keys.ArrowRight || keys.d;
    if (l || r) H.target = null;
    H.turn = l && !r ? -1 : r && !l ? 1 : 0;
    if (keys.ArrowUp || keys[" "] || keys.w) H.boost = true; else if (!H.target) H.boost = false;
  }

  // ------------------------------------------------------------------ tournament
  async function renderTournament() {
    const panel = $("#tourPanel"); panel.replaceChildren(h("h4", {}, "Tournament"), h("div", { class: "small" }, "Pick 2 to 6 saved brains. They play the same games; best length plus kills wins."));
    const options = [];
    for (const b of st.brains) {
      const hall = b.id === st.active ? st.hall : [];
      const gens = new Set([0, ...b.saved_gens.slice(-3), ...hall.map((r) => r.generation)]);
      for (const g of [...gens].filter((g) => b.saved_gens.includes(g)).sort((a, z) => a - z)) options.push({ id: b.id, gen: g, label: `${b.name} · gen ${g}` });
    }
    const chosen = new Set(options.slice(-4).map((o) => o.id + ":" + o.gen));
    const list = h("div", {});
    for (const o of options.slice(-24)) {
      const key = o.id + ":" + o.gen;
      list.append(h("div", { class: "row" }, h("input", { type: "checkbox", checked: chosen.has(key), onchange: (e) => (e.target.checked ? chosen.add(key) : chosen.delete(key)) }), h("label", {}, o.label)));
    }
    const board = h("div", { id: "tourBoard" });
    panel.append(list, h("div", { class: "btnrow" }, h("button", { class: "primary", onclick: () => guard(() => startTournament([...chosen].slice(0, 6), options, board)) }, "Start tournament")), board);
    if (!options.length) panel.append(h("p", { class: "small" }, "Train a brain for a few generations first."));
  }
  async function startTournament(keysChosen, options, board) {
    if (keysChosen.length < 2) throw new Error("Pick at least 2");
    const picks = keysChosen.map((k) => options.find((o) => o.id + ":" + o.gen === k));
    const brainsLoaded = await Promise.all(picks.map((o) => GET(`brain/${o.id}/gen/${o.gen}`).then((j) => new SA.Brain(j))));
    const world = st.world;
    const slots = picks.map((o, i) => ({ k: "t", c: i, brain: brainsLoaded[i], label: o.label }));
    st.tour = { picks, round: 1, rounds: 3, ticks: 0, perRound: 2400, score: picks.map(() => 0), board, seed: 99 };
    st.match = new SB.Match(world, slots, Math.min(8, (world && world.bots) || 6), st.tour.seed);
    renderBoard();
  }
  function tournamentTick() {
    const T = st.tour; T.ticks++;
    if (T.ticks % 60 === 0) renderBoard();
    if (T.ticks >= T.perRound) {
      st.match.stats.forEach((s, i) => (T.score[i] += s.best + 20 * s.kills));
      if (T.round >= T.rounds) { renderBoard(true); const best = T.score.indexOf(Math.max(...T.score)); banner(`Winner: ${T.picks[best].label}`, 8000); st.tour = null; return; }
      T.round++; T.ticks = 0; T.seed++;
      const slots = st.match.slots; st.match = new SB.Match(st.world, slots, st.match.arena.nBots, T.seed);
    }
  }
  function renderBoard(final) {
    const T = st.tour; if (!T) return;
    const live = st.match.stats.map((s, i) => T.score[i] + (final ? 0 : s.best + 20 * s.kills));
    const order = live.map((v, i) => i).sort((a, b) => live[b] - live[a]);
    T.board.replaceChildren(h("h4", {}, final ? "Final scores" : `Round ${T.round} of ${T.rounds}`),
      ...order.map((i, r) => h("div", { class: "row" }, h("span", { style: `color:${SB.colorFor({ k: "t", c: i }).body}` }, "●"), h("label", {}, `${r + 1}. ${T.picks[i].label}`), h("b", {}, String(Math.round(live[i]))))));
  }

  // ------------------------------------------------------------------ highlights
  async function renderHighlights() {
    const panel = $("#hlPanel"); panel.replaceChildren(h("h4", {}, "Highlights"), h("div", { class: "small" }, "While it plays, it saves its best moments by itself: new records, close calls and big takedowns."));
    if (!st.active) return;
    const list = (await GET(`brain/${st.active}/highlights`)).highlights;
    if (!list.length) panel.append(h("p", { class: "small" }, "None yet. Leave it playing and they'll appear here."));
    for (const x of list.slice(0, 40)) {
      panel.append(h("div", { class: "row" },
        h("label", {}, `${x.generation !== null && x.generation !== undefined ? "Gen " + Math.floor(x.generation) + " · " : ""}${x.label || x.kind}`, h("div", { class: "small" }, x.t ? ago(x.t) : "")),
        h("button", { onclick: () => guard(() => playReplay(x.name)) }, "▶"),
        h("button", { title: "Delete", onclick: () => guard(async () => { await POST(`brain/${st.active}/highlight/${x.name}/delete`); renderHighlights(); }) }, "×")));
    }
  }
  async function playReplay(name, opts) {
    const clip = await GET(`brain/${st.active}/highlight/${name}`);
    st.replay = { clip, i: 0, acc: 0, opts: opts || {} };
    banner(`${clip.generation !== null && clip.generation !== undefined ? "Gen " + Math.floor(clip.generation) + " · " : ""}${clip.label}`, (clip.frames.length / clip.fps) * 1000 + 500);
  }
  function drawReplay(dt, W, H) {
    const r = st.replay, c = r.clip;
    r.acc += dt * c.fps * (r.opts.speed || 1);
    while (r.acc >= 1) { r.i++; r.acc -= 1; }
    if (r.i >= c.frames.length) { st.replay = null; if (r.opts.done) r.opts.done(); return; }
    const f = c.frames[r.i], view = { wrap: c.wrap, L: c.L, R: c.R, snakes: f.snakes, food: f.food };
    let lead = null; for (const s of f.snakes) if (s.k === "a" && (!lead || s.n > lead.n)) lead = s;
    if (lead && lead.b.length) st.cam.aim(lead.b[0], lead.b[1], lead.n, W / R.dpr, c.wrap ? 2 * c.L : 0);
    if (r.i === 1) st.cam.snap();
    st.cam.update(dt, c.wrap ? 2 * c.L : 0);
    R.draw(view, st.cam, [0, 0, W, H], {});
    const cx = R.ctx; cx.fillStyle = "rgba(255,216,74,.9)"; cx.font = `700 ${13 * R.dpr}px Segoe UI`; cx.textAlign = "right";
    cx.fillText("REPLAY", W - 14 * R.dpr, 24 * R.dpr);
  }

  // ------------------------------------------------------------------ recording, timelapse
  function download(blob, name) {
    const a = $("#dl"), url = URL.createObjectURL(blob); a.href = url; a.download = name; a.click();
    setTimeout(() => URL.revokeObjectURL(url), 30000);
  }
  async function toggleClip() {
    const btn = $("#btnClip");
    if (!st.recorder) st.recorder = new SB.Recorder($("#cv"));
    if (st.recorder.on) {
      const out = await st.recorder.stop(); btn.classList.remove("rec"); btn.textContent = "● Record clip";
      if (out) { download(out.blob, `SnekBrain-${new Date().toISOString().slice(0, 19).replace(/[:T]/g, "-")}.${out.ext}`); toast(`Saved the clip to your Downloads folder (.${out.ext})`); }
      return;
    }
    st.recorder.start(); btn.classList.add("rec"); btn.textContent = "■ Stop recording";
  }
  async function timelapse() {
    if (!st.active || st.busy) return;
    const info = st.brains.find((b) => b.id === st.active);
    const gens = (info && info.saved_gens) || [];
    if (gens.length < 2) throw new Error("Train a few generations first, then the timelapse can show it improving.");
    const pick = gens.length <= 8 ? gens : Array.from({ length: 8 }, (_, i) => gens[Math.round(i * (gens.length - 1) / 7)]);
    st.busy = true; setView("live"); $("#btnTimelapse").disabled = true;
    const rec = new SB.Recorder($("#cv"));
    try {
      const brainsL = [];
      for (const g of pick) brainsL.push(new SA.Brain(await GET(`brain/${st.active}/gen/${g}`)));
      rec.start();
      const keep = st.liveBrain;
      for (let k = 0; k < pick.length; k++) {
        const { agents, bots } = showcaseCounts();
        st.match = new SB.Match(st.world, Array.from({ length: agents }, (_, i) => ({ k: "a", c: i, brain: brainsL[k] })), bots, 7);
        banner(`Generation ${pick[k]}${k === 0 ? " · never trained" : k === pick.length - 1 ? " · newest" : ""}`, 7600);
        $("#genBadge").textContent = `Gen ${pick[k]}`;
        await new Promise((res) => setTimeout(res, 8000));
      }
      const out = await rec.stop();
      st.liveBrain = keep; newLiveMatch(); $("#genBadge").textContent = `Gen ${genText(st.liveBrain)}`;
      if (out) { download(out.blob, `SnekBrain-timelapse-${info.name.replace(/[^\w-]+/g, "_")}.${out.ext}`); toast("Timelapse saved to your Downloads folder"); }
    } finally { st.busy = false; $("#btnTimelapse").disabled = false; if (rec.on) await rec.stop(); }
  }

  // ------------------------------------------------------------------ screensaver + stream layout
  function enterShow(kind) {
    document.body.classList.toggle("saver", kind === "saver"); document.body.classList.toggle("stream", kind === "stream");
    st.saver = kind === "saver"; st.stream = kind === "stream";
    if (st.view !== "live") setView("live");
    if (document.documentElement.requestFullscreen) document.documentElement.requestFullscreen().catch(() => {});
    toast("Press Esc to leave");
    renderStream();
  }
  function exitShow() {
    document.body.classList.remove("saver", "stream"); st.saver = st.stream = false;
    if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
  }
  document.addEventListener("fullscreenchange", () => { if (!document.fullscreenElement && (st.saver || st.stream)) exitShow(); });
  function renderStream() {
    if (!st.stream) return;
    const s = st.status || {}, last = st.rows[st.rows.length - 1], box = $("#streamStats");
    const ev = (st.events || []).slice(-1)[0];
    const cv = h("canvas", { height: "120" });
    box.replaceChildren(h("div", { class: "t" }, "SnekBrain"), h("div", { class: "s" }, "An AI teaching itself a snake game · by Terrelbytes"),
      h("div", { class: "n" }, String(s.generation ?? 0)), h("div", { class: "l" }, "generation"),
      h("div", { class: "n" }, fmt(s.steps || 0)), h("div", { class: "l" }, "moves played"),
      h("div", { class: "n" }, last ? String(Math.round(last.avg_max_len)) : "–"), h("div", { class: "l" }, "average length"),
      h("div", { class: "n" }, String((s.stage ?? 0) + 1)), h("div", { class: "l" }, "world level"),
      cv, ev ? h("div", { class: "ev" }, ev.text) : null);
    requestAnimationFrame(() => drawChart(cv, st.rows));
  }

  // ------------------------------------------------------------------ modals
  function openModal(...kids) { $("#modalBody").replaceChildren(...kids); $("#modal").classList.remove("hidden"); }
  function closeModal() { $("#modal").classList.add("hidden"); $("#modalBody").replaceChildren(); }
  $("#modalClose").onclick = closeModal;
  $("#modal").addEventListener("mousedown", (e) => { if (e.target.id === "modal") closeModal(); });

  async function openBrains() {
    await refreshBrains();
    const rows = st.brains.map((b) => h("tr", { class: b.id === st.active ? "active" : "" },
      h("td", {}, h("b", {}, b.name), b.parent ? h("div", { class: "small" }, `branched from ${b.parent.name || "another brain"} gen ${b.parent.generation ?? "?"}`) : null),
      h("td", {}, String(b.generation)), h("td", {}, b.best_avg_len ? String(Math.round(b.best_avg_len)) : "–"), h("td", {}, fmt(b.steps)),
      h("td", {}, h("div", { class: "acts" },
        b.id === st.active ? h("span", { class: "small" }, "in use") : h("button", { onclick: () => guard(async () => { await POST(`brain/${b.id}/select`); closeModal(); await pollStatus(); }) }, "Use"),
        h("button", { onclick: () => branchDialog(b) }, "Branch"),
        h("button", { onclick: () => guard(async () => { const n = prompt("New name", b.name); if (n) { await POST(`brain/${b.id}/rename`, { name: n }); openBrains(); } }) }, "Rename"),
        h("button", { onclick: () => guard(() => exportBrain(b)) }, "Export"),
        h("button", { onclick: () => brainSettings(b) }, "Settings"),
        h("button", { class: "danger", onclick: () => guard(async () => { if (confirm(`Delete "${b.name}"? It goes to the Recycle Bin.`)) { await POST(`brain/${b.id}/delete`); await pollStatus(); openBrains(); } }) }, "Delete")))));
    openModal(h("h2", {}, "Brains"),
      h("p", {}, "Each brain is its own AI. Make as many as you like: start fresh, branch off a good generation, or swap brains with friends as one file."),
      h("table", { class: "tbl" }, h("tr", {}, h("th", {}, "Name"), h("th", {}, "Generation"), h("th", {}, "Best avg length"), h("th", {}, "Moves trained"), h("th", {}, "")), rows),
      h("div", { class: "btnrow" },
        h("button", { class: "primary", onclick: newBrainDialog }, "New brain"),
        h("button", { onclick: () => $("#fileIn").click() }, "Import a brain file"),
        h("button", { onclick: () => guard(() => POST("open-folder")) }, "Open brains folder")));
  }
  async function exportBrain(b) {
    toast("Packing the brain…");
    const blob = await api("GET", `brain/${b.id}/export`);
    download(blob, `${b.name.replace(/[^\w-]+/g, "_")}.snekbrain`);
    toast("Saved to your Downloads folder. Send that one file to anyone with SnekBrain.");
  }
  $("#fileIn").addEventListener("change", () => guard(async () => {
    const f = $("#fileIn").files[0]; if (!f) return;
    if (f.size > 2e9) throw new Error("That file is too big");
    toast("Importing…");
    const r = await api("POST", "brains/import", await f.arrayBuffer(), true);
    $("#fileIn").value = "";
    toast("Imported. It's in your Brains list.");
    await refreshBrains(); if (!$("#modal").classList.contains("hidden")) openBrains();
    return r;
  }));
  function branchDialog(b) {
    const sel = h("select", {}, ...b.saved_gens.slice().reverse().map((g) => h("option", { value: g }, `generation ${g}`)));
    const name = h("input", { type: "text", value: `${b.name} branch` });
    openModal(h("h2", {}, `Branch "${b.name}"`),
      h("p", {}, "A branch is a copy that carries on from the generation you pick. The original stays exactly as it is, so you can try different settings on each."),
      h("div", { class: "form" }, h("label", {}, "Start from"), sel, h("label", {}, "Name"), name),
      h("div", { class: "btnrow" }, h("button", { class: "primary", onclick: () => guard(async () => { await POST(`brain/${b.id}/branch`, { generation: sel.value, name: name.value }); toast("Branch made"); openBrains(); }) }, "Make branch"), h("button", { onclick: openBrains }, "Back")));
  }

  // settings form built from the schema (both app settings and brain settings)
  function field(row, value, onchange, locked) {
    const t = row.type;
    if (t === "bool") return h("input", { type: "checkbox", checked: !!value, disabled: locked, onchange: (e) => onchange(e.target.checked) });
    if (t.startsWith("choice:")) return h("select", { disabled: locked, onchange: (e) => onchange(e.target.value) }, ...t.slice(7).split(",").map((o) => h("option", { value: o, selected: o === value }, o)));
    if (t === "int" || t === "float") return h("input", { type: "number", value, min: row.min ?? undefined, max: row.max ?? undefined, step: t === "int" ? 1 : "any", disabled: locked, onchange: (e) => onchange(t === "int" ? parseInt(e.target.value, 10) : parseFloat(e.target.value)) });
    return h("input", { type: "text", value: value ?? "", disabled: locked, onchange: (e) => onchange(e.target.value) });
  }
  const REWARD = [["r_food", "Growing", 0, 1, 0.01], ["r_kill", "Taking down other snakes", 0, 10, 0.1], ["r_death", "Penalty for dying", -20, 0, 0.5], ["r_alive", "Just staying alive", 0, 0.01, 0.0001]];
  function rewardSliders(values, set) {
    const out = [h("div", { class: "sec" }, "What it gets rewarded for"), h("div", { class: "help" }, "This is the only thing you tell it. It never gets told how to play, only what counts as doing well.")];
    for (const [k, label, lo, hi, step] of REWARD) {
      const o = h("output", {}, String(values[k]));
      out.push(h("label", {}, label), h("div", { class: "slider" }, h("input", { type: "range", min: lo, max: hi, step, value: values[k], oninput: (e) => { o.textContent = e.target.value; set(k, parseFloat(e.target.value)); } }), o));
    }
    return out;
  }
  const PRESETS_KEY = "snekbrain.rewardPresets";
  function rewardPresetRow(values, apply) {
    let saved = {}; try { saved = JSON.parse(localStorage.getItem(PRESETS_KEY) || "{}"); } catch (e) { saved = {}; }
    const builtIn = { "Balanced (default)": { r_food: 0.1, r_kill: 2, r_death: -5, r_alive: 0.0005 }, "Hunter": { r_food: 0.05, r_kill: 6, r_death: -5, r_alive: 0 }, "Survivor": { r_food: 0.05, r_kill: 0.5, r_death: -15, r_alive: 0.004 }, "Glutton": { r_food: 0.3, r_kill: 0.5, r_death: -3, r_alive: 0 } };
    const all = Object.assign({}, builtIn, saved);
    const sel = h("select", { onchange: (e) => { const p = all[e.target.value]; if (p) apply(p); } }, h("option", { value: "" }, "Reward presets…"), ...Object.keys(all).map((n) => h("option", { value: n }, n)));
    const save = h("button", { onclick: () => { const n = prompt("Name this reward preset"); if (!n) return; saved[n.slice(0, 40)] = Object.fromEntries(REWARD.map(([k]) => [k, values[k]])); try { localStorage.setItem(PRESETS_KEY, JSON.stringify(saved)); } catch (e) { /* private mode */ } toast("Preset saved"); } }, "Save as preset");
    return h("div", { class: "btnrow" }, sel, save);
  }
  async function newBrainDialog() {
    const sch = (await GET("settings")).schema;
    let preset = "quick"; const values = {};
    const defaults = Object.fromEntries(sch.brain.map((r) => [r.key, r.default]));
    Object.assign(values, defaults);
    const name = h("input", { type: "text", value: "Newborn" });
    const presetBtns = Object.entries(sch.presets).map(([k, p]) => h("button", { class: k === preset ? "on" : "", onclick: (e) => { preset = k; $$(".presetGrid button").forEach((b) => b.classList.remove("on")); e.currentTarget.classList.add("on"); } }, h("b", {}, p.label), h("small", {}, p.help)));
    const form = h("div", { class: "form" });
    const draw = () => form.replaceChildren(...rewardSliders(values, (k, v) => (values[k] = v)));
    draw();
    const adv = h("div", { class: "form" });
    for (const r of sch.brain) if (!REWARD.some(([k]) => k === r.key)) adv.append(h("label", { title: r.help }, r.label + (r.new_only ? " *" : "")), field(r, values[r.key], (v) => (values[r.key] = v)), r.help ? h("div", { class: "help" }, r.help) : null);
    openModal(h("h2", {}, "New brain"), h("p", {}, "A new brain starts knowing nothing. It moves at random until it learns what works."),
      h("div", { class: "form" }, h("label", {}, "Name"), name),
      h("div", { class: "presetGrid" }, presetBtns),
      form, rewardPresetRow(values, (p) => { Object.assign(values, p); draw(); }),
      h("details", {}, h("summary", {}, "All settings (for tinkerers)"), h("p", { class: "small" }, "* can only be picked now, because it shapes the brain itself. Leave a setting alone to use the preset's value."), adv),
      h("div", { class: "btnrow" }, h("button", { class: "primary", onclick: () => guard(async () => {
        const changed = {}; for (const [k, v] of Object.entries(values)) if (v !== defaults[k]) changed[k] = v;
        const r = await POST("brains/new", { name: name.value, preset, settings: changed, start: true });
        closeModal(); toast("New brain made and training"); await refreshBrains(); await pollStatus(); return r;
      }) }, "Make it and start training"), h("button", { onclick: openBrains }, "Back")));
  }
  async function brainSettings(b) {
    const [sch, info] = await Promise.all([GET("settings"), GET(`brain/${b.id}`)]);
    const values = Object.assign({}, info.settings);
    const trained = info.trained;
    const form = h("div", { class: "form" });
    const draw = () => {
      form.replaceChildren(...rewardSliders(values, (k, v) => (values[k] = v)), h("div", { class: "sec" }, "Everything else"));
      for (const r of sch.schema.brain) if (!REWARD.some(([k]) => k === r.key)) form.append(h("label", { title: r.help }, r.label), field(r, values[r.key], (v) => (values[r.key] = v), r.new_only && trained), r.help ? h("div", { class: "help" }, r.help) : null);
    };
    draw();
    openModal(h("h2", {}, `Settings for "${b.name}"`), h("p", {}, trained ? "Greyed-out settings shape the brain itself, so they're fixed now. Make a new brain to change them. Saving restarts training with the new settings." : "Nothing trained yet, so everything can still change."),
      rewardPresetRow(values, (p) => { Object.assign(values, p); draw(); }), form,
      h("div", { class: "btnrow" }, h("button", { class: "primary", onclick: () => guard(async () => {
        const changed = {}; for (const [k, v] of Object.entries(values)) if (v !== info.settings[k]) changed[k] = v;
        await POST(`brain/${b.id}/settings`, { settings: changed }); toast("Saved"); openBrains();
        if (b.id === st.active) loadBrain(b.id);
      }) }, "Save"), h("button", { onclick: openBrains }, "Back")));
  }

  async function openHall() {
    if (!st.active) return;
    const recs = st.hall.slice().reverse();
    const info = st.brains.find((x) => x.id === st.active) || { saved_gens: [] };
    const rows = recs.map((r) => h("tr", {}, h("td", {}, h("b", {}, r.label || r.kind)), h("td", {}, `gen ${r.generation}`), h("td", {}, r.t ? ago(r.t) : ""),
      h("td", {}, info.saved_gens.includes(r.generation) ? h("div", { class: "acts" },
        h("button", { onclick: () => guard(() => watchGen(r.generation)) }, "Watch it"),
        h("button", { onclick: () => branchDialog(info) }, "Branch from it")) : h("span", { class: "small" }, "not kept"))));
    openModal(h("h2", {}, "Hall of Fame"), h("p", {}, "Every time it sets a record, that generation is kept forever. Watch any of them, branch from them, or pit them against each other in a Tournament."),
      recs.length ? h("table", { class: "tbl" }, h("tr", {}, h("th", {}, "Record"), h("th", {}, "Generation"), h("th", {}, "When"), h("th", {}, "")), rows) : h("p", {}, "No records yet. They come quickly once training starts."),
      h("div", { class: "btnrow" }, h("button", { onclick: () => { closeModal(); setView("tournament"); } }, "Open tournament")));
  }
  async function watchGen(g) {
    const b = new SA.Brain(await GET(`brain/${st.active}/gen/${g}`));
    closeModal(); setView("live");
    st.match.setBrain(b); st.match.highlights = false; $("#genBadge").textContent = `Gen ${g} (from the Hall of Fame)`;
    st.liveV = null; // the next upgrade swaps the newest brain back in
  }

  async function openSettings() {
    const s = await GET("settings"); st.settings = s.app; st.schema = s.schema;
    const values = Object.assign({}, s.app);
    const form = h("div", { class: "form" });
    const skip = new Set(["intro_seen", "mode"]);
    for (const r of s.schema.app) if (!skip.has(r.key)) form.append(h("label", { title: r.help }, r.label), field(r, values[r.key], (v) => (values[r.key] = v)), r.help ? h("div", { class: "help" }, r.help) : null);
    const presets = {
      "Demo": { mode: "turbo", swap_seconds: 10, live_fps: 10 },
      "Overnight": { mode: "normal", swap_seconds: 60, when_video: "pause", train_when_closed: true },
      "Turbo": { mode: "turbo", swap_seconds: 20 },
    };
    const presetRow = h("div", { class: "btnrow" }, h("span", { class: "small" }, "Quick setups:"), ...Object.entries(presets).map(([n, p]) => h("button", { title: JSON.stringify(p), onclick: () => guard(async () => { await POST("settings", { app: p }); toast(`${n} setup on`); openSettings(); pollStatus(); }) }, n)));
    const gpuBox = h("div", {}), aiBox = h("div", {}), upBox = h("div", {});
    openModal(h("h2", {}, "Settings"), presetRow, form,
      h("div", { class: "btnrow" }, h("button", { class: "primary", onclick: () => guard(async () => { const changed = {}; for (const [k, v] of Object.entries(values)) if (v !== s.app[k]) changed[k] = v; await POST("settings", { app: changed }); toast("Saved"); openSettings(); pollStatus(); }) }, "Save settings")),
      h("div", { class: "sec", style: "margin-top:16px" }, ""), h("h3", {}, "GPU boost"), gpuBox, h("h3", {}, "AI assistant hook"), aiBox, h("h3", {}, "Updates"), upBox,
      h("p", { class: "small" }, `Your brains and settings live in ${s.data_dir}`));
    guard(async () => renderGpu(gpuBox, await GET("gpu")));
    guard(async () => renderAi(aiBox, await GET("ai-hook")));
    guard(async () => renderUpdate(upBox, await GET("update")));
  }
  function renderGpu(box, g) {
    const kids = [];
    if (!g.card) kids.push(h("p", {}, "No NVIDIA graphics card found, so training uses the CPU (which works great for this size of brain)."));
    else {
      kids.push(h("p", {}, `Found: ${g.card.name} (${g.card.memory}). Matching add-on: ${g.build || "none for this card"}.`));
      if (g.job && g.job.state === "downloading") kids.push(h("p", {}, `Downloading… ${g.job.total ? Math.round(100 * g.job.done / g.job.total) + "%" : fmt(g.job.done) + " bytes"}`));
      else if (g.job && g.job.state && !["idle", "done", "error"].includes(g.job.state)) kids.push(h("p", {}, g.job.state + "…"));
      if (g.job && g.job.state === "error") kids.push(h("p", { class: "small", style: "color:var(--bad)" }, g.job.error));
      if (g.installed) kids.push(h("p", {}, `Add-on installed (${g.installed}). Turn on "GPU boost" above and save.`), h("button", { class: "danger", onclick: () => guard(async () => { await POST("gpu/remove"); renderGpu(box, await GET("gpu")); }) }, "Remove the add-on"));
      else if (g.supported) kids.push(h("p", { class: "small" }, "The add-on is PyTorch with CUDA from pytorch.org, about 2.5 GB, checked against its published checksum. Nothing else changes."),
        h("button", { onclick: () => guard(async () => { if (!confirm("Download the GPU add-on (about 2.5 GB) from pytorch.org?")) return; await POST("gpu/install"); poll(); }) }, "Download the GPU add-on"));
    }
    box.replaceChildren(...kids);
    const poll = () => setTimeout(async () => { const n = await GET("gpu").catch(() => null); if (n && !$("#modal").classList.contains("hidden")) { renderGpu(box, n); if (n.job && !["idle", "done", "error"].includes(n.job.state)) poll(); } }, 1500);
    if (g.job && !["idle", "done", "error"].includes(g.job.state)) poll();
  }
  function renderAi(box, a) {
    if (!a.enabled) { box.replaceChildren(h("p", { class: "small" }, "Off. Turn on \"Let my own AI assistant connect\" above to let an assistant like Claude Desktop read the stats and start or pause training. It never needs this.")); return; }
    const cfg = { mcpServers: { snekbrain: { command: a.mcp_command[0], args: a.mcp_command.slice(1) } } };
    const pre = h("textarea", { readonly: true, style: "width:100%;height:120px;font-family:Consolas,monospace;font-size:12px" }); pre.value = JSON.stringify(cfg, null, 2);
    box.replaceChildren(h("p", { class: "small" }, "On. Add this to your assistant's MCP settings (for Claude Desktop: Settings > Developer > Edit Config). It can read stats and start, pause or switch training, nothing more. Turn the hook off to cut it off."), pre,
      h("button", { onclick: () => { navigator.clipboard.writeText(pre.value).then(() => toast("Copied")); } }, "Copy"));
  }
  function renderUpdate(box, u) {
    const kids = [h("p", { class: "small" }, `You have version ${u.current || (st.status && st.status.version)}.`)];
    if (u.error) kids.push(h("p", { class: "small" }, `Last check: ${u.error}.`));
    if (u.newer) {
      kids.push(h("p", {}, `Version ${u.latest} is out.`));
      if (u.notes) kids.push(h("details", {}, h("summary", {}, "What's new"), h("pre", { style: "white-space:pre-wrap" }, u.notes)));
      kids.push(h("button", { class: "primary", onclick: () => guard(async () => { if (!confirm(`Download and install SnekBrain ${u.latest}? SnekBrain closes while it installs. Your brains are kept.`)) return; await POST("update/install"); toast("Downloading the update… SnekBrain will close and the installer opens."); }) }, `Install ${u.latest}`));
    } else if (u.latest) kids.push(h("p", { class: "small" }, "You're up to date."));
    if (u.job && u.job.state === "error") kids.push(h("p", { class: "small", style: "color:var(--bad)" }, u.job.error));
    kids.push(h("button", { onclick: () => guard(async () => renderUpdate(box, await GET("update?force=1"))) }, "Check now"));
    box.replaceChildren(...kids);
  }

  // ------------------------------------------------------------------ about + guided tour
  const TOUR = [
    ["🐍", "Watch an AI play", "Every snake with eyes like this is controlled by one artificial brain. When it starts, it knows nothing at all: not what food is, not that crashing is bad."],
    ["🎯", "Nobody programs it", "There are no rules telling it how to play. It only gets points for growing and taking down snakes, and loses points when it dies. That's it."],
    ["🔁", "It trains itself", "In the background it plays thousands of games at once and keeps whatever made it score better. That's reinforcement learning, the same idea behind game-playing AIs."],
    ["⬆️", "You see it improve", "Every few seconds the snake on screen gets the newest brain. Check the chart, the Before / after tab and the Hall of Fame to see how far it has come. Want to see a trained one right now? Pick \"Starter\" in the brain list at the top."],
    ["⚡", "Turbo and more", "Turbo uses more of your PC to learn faster. Save brains, branch them, play against them, record clips, and send a brain to a friend as one file."],
  ];
  function openTour(i) {
    i = i || 0; const [pic, title, text] = TOUR[i];
    openModal(h("div", { class: "tour" }, h("div", { class: "pic" }, pic), h("div", {}, h("h2", {}, title), h("p", { class: "lead" }, text))),
      h("div", { class: "dots" }, ...TOUR.map((_, k) => h("i", { class: k === i ? "on" : "" }))),
      h("div", { class: "btnrow", style: "justify-content:flex-end" },
        i > 0 ? h("button", { onclick: () => openTour(i - 1) }, "Back") : null,
        i < TOUR.length - 1 ? h("button", { class: "primary", onclick: () => openTour(i + 1) }, "Next") : h("button", { class: "primary", onclick: () => guard(async () => { closeModal(); await POST("settings", { app: { intro_seen: true } }); if (!st.status.running) { await POST("start"); pollStatus(); } }) }, "Start training")));
  }
  function openAbout() {
    openModal(h("h2", {}, "Terrelbytes SnekBrain"), h("p", { class: "lead" }, "AI Reinforcement Learning Demonstration Program"),
      h("p", {}, "Watch an AI teach itself a snake game. Nobody tells it how to play: it learns by playing, and you watch it get better."),
      h("p", {}, "Its brain is a small neural network (about 85,000 numbers) trained with PPO, a reinforcement learning method, right here on your PC. No internet needed, no account, nothing sent anywhere."),
      h("div", { class: "btnrow" }, h("button", { onclick: () => openTour(0) }, "Show the intro again"),
        h("button", { onclick: () => guard(async () => { if (!confirm("Quit SnekBrain completely? Training stops (everything is saved) until you open it again.")) return; await POST("quit"); document.body.replaceChildren(h("p", { style: "padding:40px;font-size:18px" }, "SnekBrain has stopped. You can close this window.")); }) }, "Quit SnekBrain")),
      h("p", { class: "small" }, `Version ${st.status ? st.status.version : ""} · made by Terrelbytes AI Solutions · open source (MIT). Built with Python, PyTorch, Stable-Baselines3 and Gymnasium; see the licenses file in the install folder. An original snake game, not affiliated with any other snake game.`));
  }

  // ------------------------------------------------------------------ wiring
  $$("#views button").forEach((b) => (b.onclick = () => setView(b.dataset.view)));
  $$("#speedSeg button").forEach((b) => (b.onclick = () => { st.speed = +b.dataset.speed; $$("#speedSeg button").forEach((x) => x.classList.toggle("on", x === b)); }));
  $$("#modeSeg button").forEach((b) => (b.onclick = () => guard(async () => { st.status = await POST("mode", { mode: b.dataset.mode }); renderStatus(); toast(b.dataset.mode === "turbo" ? "Turbo: learning as fast as your PC allows" : "Normal: learning quietly in the background"); })));
  $("#btnTrain").onclick = () => guard(async () => { st.status = await POST(st.status && st.status.running ? "pause" : "start"); renderStatus(); });
  $("#brainSelect").onchange = (e) => guard(async () => { await POST(`brain/${e.target.value}/select`); await pollStatus(); });
  $$("nav button").forEach((b) => (b.onclick = () => guard(() => ({ brains: openBrains, hall: openHall, settings: openSettings, about: openAbout })[b.dataset.open]())));
  $("#btnClip").onclick = () => guard(toggleClip);
  $("#btnTimelapse").onclick = () => guard(timelapse);
  $("#btnScreensaver").onclick = () => enterShow("saver");
  $("#btnStream").onclick = () => enterShow("stream");

  async function boot() {
    await pollStatus();
    const hv = decodeURIComponent((location.hash || "").slice(1)); // e.g. #compare opens that tab (stream overlays, screenshots)
    if (["live", "compare", "inside", "play", "tournament", "highlights"].includes(hv)) setView(hv);
    if (hv === "stream" || hv === "saver") enterShow(hv);
    requestAnimationFrame(frame);
    setInterval(pollStatus, 2000);
    setInterval(pollLive, 3000);
    setInterval(pollSlow, 10000);
    setInterval(pollInside, 125);
    const s = await GET("settings").catch(() => null);
    if (s && !s.app.intro_seen) openTour(0);
    if (s && s.app.check_updates) GET("update").then((u) => { if (u.newer) toast(`SnekBrain ${u.latest} is out. See Settings > Updates.`); }).catch(() => {});
  }
  boot();
  window.SNEK = st; // handy for the tests
})();
