/* The web HUD.
 *
 * It knows nothing except what /events sends it, and it can do nothing except
 * POST an action name back. Every decision -- what state we are in, whether a
 * key is refused, what the microphone is doing -- was already made in Python,
 * in the same code the terminal HUD calls. This file draws it.
 *
 * The connection is the window. While this page holds the event stream open,
 * the server counts a window as open and the voice runs; when the last stream
 * drops, the server shuts down and takes the microphone with it. That is the
 * same promise the curses HUD made by being a process you could close.
 */

const TOKEN = document.currentScript.dataset.token || "";
const $ = (id) => document.getElementById(id);

let snap = null;
let flashUntil = 0;
/* The microphone level, as last published. Speech arrives here about twenty
 * times a second and the page repaints sixty, so it is a target to move
 * towards rather than a value to draw. */
let heard = 0;

/* --- the window itself -------------------------------------------------
 *
 * A frameless window has no title bar, so the page has to be one. It cannot
 * move itself, though: only the process that owns the window can do that. So
 * it asks, over the WebKit bridge, and the shell does it.
 *
 * In the browser fallback there is no bridge and there is a real title bar
 * already, so these are no-ops there -- except closing, which is not a window
 * question at all. Closing the HUD means the application stops: the server
 * shuts down, and it takes the window, the microphone and the heartbeat with
 * it. That works the same in every shell. */

const BRIDGE = window.webkit?.messageHandlers?.hud;
const native = (msg) => BRIDGE && BRIDGE.postMessage(msg);

const EDGES = ["n", "s", "w", "e", "nw", "ne", "sw", "se"];

function windowControls() {
  const bar = $("bar");
  bar.addEventListener("mousedown", (e) => {
    if (e.button !== 0 || e.target.closest(".no-drag")) return;
    native("drag");
  });
  bar.addEventListener("dblclick", (e) => {
    if (e.target.closest(".no-drag")) return;
    native("maximize");
  });
  $("close").onclick = () => post("/quit");

  // Grips only where there is a shell to act on them. The browser fallback
  // has a real title bar and resizes itself; drawing dead resize cursors
  // over it would be a lie the pointer tells.
  if (!BRIDGE) return;
  const grips = $("grips");
  grips.hidden = false;
  for (const edge of EDGES) {
    const g = document.createElement("i");
    g.className = edge;
    g.addEventListener("mousedown", (e) => {
      if (e.button !== 0) return;
      e.preventDefault();
      native(`resize:${edge}`);
    });
    grips.appendChild(g);
  }
}

/* --- the stream ------------------------------------------------------- */

function connect() {
  const es = new EventSource(`/events?token=${TOKEN}`);
  // Its own event, because it arrives far more often than everything else
  // and none of the rest of the window has to be touched when it does.
  es.addEventListener("level", (e) => { heard = parseFloat(e.data) || 0; });
  es.onmessage = (e) => {
    snap = JSON.parse(e.data);
    if ($("boot")) $("boot").remove();
    document.querySelector(".grid").hidden = false;
    render(snap);
  };
  es.onerror = () => {
    // A dropped stream is either a reload or a server that has gone. Say so
    // rather than freezing on the last frame, which reads as a live HUD.
    document.body.dataset.state = "voice_off";
    $("mode").textContent = "disconnected";
    heard = 0;                     // nothing is being heard down a dead pipe
  };
}

/* The token goes in a header, not the body and not the query. That is what
 * makes the request unreachable from anywhere else: a form submission and a
 * top-level navigation -- the only things another page can aim at a loopback
 * port -- cannot set a header, and a script that tried would need a CORS
 * preflight the server answers with nothing. */
function post(path, body) {
  return fetch(path, {
    method: "POST",
    headers: { "content-type": "application/json", "X-CV-Token": TOKEN },
    body: JSON.stringify(body || {}),
  }).then((r) => r.json()).catch(() => ({ ok: false, msg: "no answer" }));
}

async function act(name) {
  const r = await post("/act", { action: name });
  if (r.msg) flash(r.msg, !r.ok);
}

function flash(msg, bad) {
  document.querySelector(".flash")?.remove();
  const el = document.createElement("div");
  el.className = "flash" + (bad ? " bad" : "");
  el.textContent = msg;
  document.body.appendChild(el);
  const mine = (flashUntil = Date.now() + (bad ? 3200 : 2000));
  setTimeout(() => { if (flashUntil === mine) el.remove(); }, bad ? 3200 : 2000);
}

/* --- drawing ---------------------------------------------------------- */

/* The state labels come from the language pack letterspaced -- "S T A N D I N G
 * B Y" -- because the terminal draws them as a title and has no other way to
 * space letters. In a browser the spacing is CSS's job, so the spaces have to
 * come out before anything but the big label uses one. */
const deSpace = (s) =>
  s.replace(/(?<=\S) (?=\S)/g, "").replace(/\s{2,}/g, " ").trim();

const UNITS = ["B", "KB", "MB", "GB", "TB"];

const scale = (n) => {
  let i = 0;
  while (n >= 1024 && i < UNITS.length - 1) { n /= 1024; i++; }
  return [n < 10 ? n.toFixed(1) : String(Math.round(n)), UNITS[i]];
};

const human = (n) => scale(n).join(" ");

/* "21 / 30 GB", not "21 GB / 30 GB" -- the repeated unit is what pushes a
   tile onto two lines, and it says nothing the second time. */
const pair = (a, b) => {
  const [av, au] = scale(a), [bv, bu] = scale(b);
  return au === bu ? `${av} / ${bv} ${bu}` : `${av} ${au} / ${bv} ${bu}`;
};

function meter(id, pct) {
  $(`${id}-bar`).style.width = `${Math.min(100, pct)}%`;
  $(`${id}-bar`).classList.toggle("hot", pct >= 85);
  $(`${id}-val`).textContent = `${Math.round(pct)}%`;
}

/* Which blocks this window draws. Everything is on unless the config says
 * otherwise: not everyone works in pull requests, and a panel about subagents
 * is noise to somebody who has never launched one. A column with nothing left
 * in it goes too -- an empty rail down the side of the window is worse than
 * the panel it used to hold. */
function blocks(s) {
  const p = s.panels || {};
  const on = (k) => p[k] !== false;
  $("system-block").hidden = !on("system");
  $("session-block").hidden = !on("session");
  $("agents-block").hidden = !on("agents");
  // The repo block hides itself when there is no repository to name, so this
  // only has to answer the other question: whether it is wanted at all.
  if (!on("repo")) $("repo-block").hidden = true;
  return on;
}

/* The right column, once everything that could fill it has been drawn.
 *
 * Alerts live there and are not a panel: an open microphone with no owner is
 * worth a rail of its own whatever the config says about blocks. So this is
 * asked after alerts() has run and answered from what is actually in the box
 * -- there is no alert list in the state to count, they are worked out here. */
function column(on) {
  document.querySelector(".panel.right").hidden =
    !on("session") && !on("agents") && !on("repo")
    && !$("alerts").childElementCount;
}

function render(s) {
  document.body.dataset.state = s.state;
  $("brand").textContent = s.title;
  $("mode").textContent = deSpace(s.label);
  // CSS does the letterspacing here, so the pack's own spacing comes out
  // first -- otherwise the gap between words disappears into the tracking.
  $("state").textContent = deSpace(s.label);
  $("said").textContent = s.said ? `«${s.said}»` : "";

  const badge = $("badge");
  badge.classList.toggle("on", s.voice_on);
  badge.textContent = !s.voice_on ? "VOICE OFF"
    : s.focus.state ? "VOICE ON · ONE SESSION" : "VOICE ON";

  const sys = s.system;
  meter("cpu", sys.cpu); meter("mem", sys.mem); meter("disk", sys.disk);
  $("mem-abs").textContent = pair(sys.mem_used, sys.mem_total);
  $("disk-abs").textContent = human(sys.disk_free);
  $("load-abs").textContent = sys.load.map((x) => x.toFixed(2)).join(" ");

  // A machine with no readable card shows no card rows at all. Zeros would
  // read as a measurement, and it is the one number here that is not one.
  const gpu = sys.gpu;
  for (const id of ["gpu-row", "vram-row", "gpu-name", "vram-tile"])
    $(id).hidden = !gpu;
  if (gpu) {
    meter("gpu", gpu.busy);
    meter("vram", gpu.vram);
    $("vram-abs").textContent = pair(gpu.vram_used, gpu.vram_total);
    $("gpu-name").textContent = gpu.name;
    $("gpu-name").title = gpu.name;
  }

  $("history-title").textContent = `// ${deSpace(s.labels.history)}`;
  history(s);

  const on = blocks(s);
  if (on("repo")) repo(s);
  session(s);
  $("k-lang").textContent = s.language.name || s.language.preset;
  $("k-mic").textContent = s.mic.speaking ? "recording you"
    : s.mic.conversation ? "open · conversation"
    : s.mic.open ? `open · ${(s.mic.held[0] || "no owner")}`
    : s.dictation.recording ? "recording" : "closed";

  const list = $("agents");
  list.innerHTML = "";
  if (!s.agents.length) list.innerHTML = '<li class="empty">none running</li>';
  for (const a of s.agents) {
    const li = document.createElement("li");
    li.textContent = a;
    list.appendChild(li);
  }

  ear(s);
  alerts(s);
  column(on);
  keys(s);
  ticker(s);
}

/* What the watched session is working on.
 *
 * The branch is nearly free and always right; the pull request and its checks
 * come from `gh` on a slow clock, so this row can be a second behind and,
 * when the network is down, several minutes behind. That is the correct
 * trade: the alternative is a window that stalls on somebody else's CI.
 *
 * Nothing here says "unknown" or "loading". A row that has no answer is not
 * drawn -- an empty pull request row on a branch that has never been pushed
 * reads as a failure to find one, and there is nothing to find. */
function repo(s) {
  const r = s.repo || {};
  $("repo-block").hidden = !r.branch;
  if (!r.branch) return;
  // Two rows, not one: a repository name and a branch name on the same line
  // wrap into three in a panel this narrow, and the wrap lands mid-word.
  $("k-repo").textContent = r.name || "—";
  $("k-branch").textContent = r.branch + (r.detached ? " (detached)" : "");

  const pr = r.pr;
  for (const id of ["k-pr-label", "k-pr"]) $(id).hidden = !pr;
  for (const id of ["k-checks-label", "k-checks"]) $(id).hidden = !pr;
  if (!pr) return;

  $("k-pr-num").textContent =
    `#${pr.number} · ${pr.draft && pr.state === "open" ? "draft" : pr.state}`;
  $("k-pr-title").textContent = pr.title || "";

  // Counts read better as a sentence than as a table, and the failing names
  // are the only thing here that saves a trip to the browser.
  const c = pr.checks || {};
  const box = $("k-checks");
  box.dataset.check = c.state || "none";
  const bits = [];
  if (c.running) bits.push(`${c.running} running`);
  if (c.fail) bits.push(`${c.fail} failing`);
  if (c.pass) bits.push(`${c.pass} passing`);
  box.textContent = { passing: "✓ ", failing: "✗ ", running: "● " }[c.state] || "";
  box.append(bits.join(" · ") || "none yet");
  if (c.failing?.length) {
    const names = document.createElement("span");
    names.className = "chk-names";
    names.textContent = c.failing.join(", ");
    box.appendChild(names);
  }
}

/* Where your voice goes in, and where sound comes out.
 *
 * They are two settings and `t` moves both, so they name the same pane nearly
 * always -- printing that pane's name twice says nothing the second time. One
 * row while they agree; two the moment they do not, which is the only time
 * the distinction has ever mattered:
 *
 *   no focus set      dictation names a pane, the voice belongs to everyone
 *   the pane is gone  the voice is held by a window that closed, so nothing
 *                     speaks anywhere until it is cleared
 *   aimed apart       you are typing into one window and another answers
 */
function session(s) {
  const target = s.dictation.target || "—";
  const f = s.focus;
  const split = (label, dictation, voice) => {
    $("k-io-label").textContent = label;
    $("k-io").textContent = dictation;
    for (const id of ["k-voice-label", "k-voice"]) $(id).hidden = !voice;
    if (voice) $("k-voice").textContent = voice;
  };

  if (f.state === "gone") split("dictation", target, `${f.label} — that pane is gone`);
  else if (f.state && !f.here) split("dictation", target, f.label);
  else if (f.state) split("voice + dictation", target, "");
  else split("dictation", target, "every session");
}

function history(s) {
  const box = $("history");
  const near = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
  if (!s.history.length) {
    box.innerHTML = `<p class="empty">${s.labels.history_empty}</p>`;
    return;
  }
  // Rebuilt from the tail rather than appended to: the log is trimmed at the
  // far end and a list that only ever grows would keep lines the file dropped.
  box.innerHTML = "";
  for (const e of s.history) {
    const row = document.createElement("div");
    row.className = `line ${e.side === "in" ? "in" : "out"}`;
    const when = e.t ? new Date(e.t * 1000).toTimeString().slice(0, 5) : "";
    row.innerHTML = `<time></time><span class="who"></span><span class="text"></span>`;
    row.querySelector("time").textContent = when;
    row.querySelector(".who").textContent =
      e.side === "in" ? s.labels.history_you : s.labels.history_said;
    row.querySelector(".text").textContent = e.text;
    box.appendChild(row);
  }
  if (near) box.scrollTop = box.scrollHeight;
}

/* The ear, as its own indicator.
 *
 * The `c` button lighting up says the mode is on, which is not the same as
 * being heard -- the microphone can be open with nothing on the other end,
 * and that is exactly the moment the difference matters. So this is three
 * states and not a toggle:
 *
 *   ready    armed and quiet, breathing -- say something
 *   hearing  the microphone is recording you
 *   deaf     the microphone is open and nothing is listening
 *
 * It follows the EAR, not the mode. Push-to-talk opens the microphone just
 * as conversation mode does, and a badge that went dark for one of them
 * would be the same silence this exists to end -- so `d` lights it too, for
 * as long as it is recording. Only `ready` is conversation-specific, because
 * push-to-talk has no resting armed state: it is recording or it is nothing.
 *
 * And nothing at all when the ear is shut. A dark indicator is a statement,
 * and there it would be the wrong one.
 */
function ear(s) {
  const state = s.mic.stranded ? "deaf"
    : s.mic.speaking || s.dictation.recording ? "hearing"
    : s.mic.conversation ? "ready" : "";
  if (state) document.body.dataset.mic = state;
  else delete document.body.dataset.mic;

  $("mic").hidden = !state;
  if (!state) return;
  $("mic-word").textContent = s.labels[
    { ready: "mic_ready", hearing: "mic_hearing", deaf: "mic_deaf" }[state]];
}

function alerts(s) {
  const box = $("alerts");
  const out = [];
  // Order is severity, and severity is what gets read first. The stranded
  // microphone comes before everything: it is the one case where the window
  // looks like it is listening and nothing is.
  if (s.mic.stranded) {
    out.push([s.mic.speaking
      ? `${s.mic.stranded} — you are talking to nothing`
      : `${s.mic.stranded} — conversation on hold`, false]);
  } else if (s.mic.open && !s.mic.daemon && !s.dictation.recording) {
    out.push(["microphone open, no owner — press x", false]);
  } else if (!s.mic.open && s.mic.held.length) {
    out.push([`mic held open by ${s.mic.held[0]} — not recording`, true]);
  }
  if (s.focus.state === "gone")
    out.push([`voice held by ${s.focus.label} — that pane is gone, press f`, false]);
  else if (s.focus.state && !s.focus.here)
    out.push([`voice → ${s.focus.label} · dictation → ${s.dictation.target || "—"}`, false]);
  if (s.dictation.blocked)
    out.push([`${s.dictation.blocked} — dictation disabled`, false]);

  box.innerHTML = "";
  for (const [text, calm] of out) {
    const el = document.createElement("div");
    el.className = "alert" + (calm ? " calm" : "");
    el.textContent = text;
    box.appendChild(el);
  }
}

/* Each button says what it WILL DO, the way the terminal legend does. */
function keys(s) {
  const row = [
    ["m", s.voice_on ? "turn off and silence" : "turn the voice on", "voice", s.voice_on],
    ["f", s.focus.state ? "unmute the rest" : "mute the rest", "focus", !!s.focus.state],
    ["d", s.dictation.recording ? "stop and send" : "dictate", "dictate", s.dictation.recording],
    ["c", s.mic.conversation ? "end conversation" : "conversation", "conversation", s.mic.conversation],
    ["t", "session", "session", false],
  ];
  if (s.language.next && s.language.next !== s.language.preset)
    row.push(["l", s.language.next_label, "language", false]);
  row.push(["x", "close orphan capture", "sweep", false]);
  // Last, and named for what it does to the application rather than to the
  // window: closing the HUD stops the voice, and the legend should say so
  // before you find out.
  row.push(["q", "quit — stops the voice", "quit", false]);

  const box = $("keys");
  const want = row.map((r) => r.join("|")).join(",");
  if (box.dataset.want === want) return;   // rebuilding steals a hover
  box.dataset.want = want;
  box.innerHTML = "";
  for (const [key, label, action, live] of row) {
    const b = document.createElement("button");
    b.className = live ? "live" : "";
    b.innerHTML = `<kbd></kbd><span></span>`;
    b.querySelector("kbd").textContent = key;
    b.querySelector("span").textContent = label;
    b.onclick = () => (action === "quit" ? post("/quit") : act(action));
    box.appendChild(b);
  }
}

/* The scrolling line of nothing much, from the reference. It is decoration,
   and it says so by only ever repeating what is already on screen. */
function ticker(s) {
  const bits = [
    `voice.${s.voice_on ? "on" : "off"}`, `state.${s.state}`,
    `lang.${s.language.preset}`, `cpu=${s.system.cpu}%`, `ram=${s.system.mem}%`,
    `agents=${s.agents.length}`, `mic.${s.mic.open ? "open" : "closed"}`,
  ];
  const line = bits.join("  ·  ") + "  ·  ";
  $("ticker").textContent = line.repeat(6);
}

/* --- the reactor ------------------------------------------------------ */

const blob = $("blob");
const fill = $("core-fill");
const sats = $("satellites");
const wave = $("wave").getContext("2d");

/* Lobes, wobble and spin per state -- the same distinctions the terminal
   HUD drew with rings that breathed, spun or pulsed. `live` is how much of
   the shape the voice is allowed to take over when there is a voice: the two
   states that HAVE one give away most of it, and the rest keep their clock,
   because nothing is coming in to move them. */
const SHAPE = {
  thinking:  { lobes: 11, wob: 7,  spin: 0.55, breathe: 2,  bars: 0.55, live: 0 },
  agents:    { lobes: 13, wob: 9,  spin: 1.10, breathe: 2,  bars: 0.75, live: 0 },
  speaking:  { lobes: 9,  wob: 6,  spin: 0.18, breathe: 3,  bars: 0.30, live: 1 },
  listening: { lobes: 10, wob: 7,  spin: -0.4, breathe: 2,  bars: 0.25, live: 1 },
  stranded:  { lobes: 9,  wob: 1,  spin: 0,    breathe: 0,  bars: 0.04, live: 0 },
  ready:     { lobes: 9,  wob: 5,  spin: 0.12, breathe: 4,  bars: 0.28, live: 0 },
  idle:      { lobes: 9,  wob: 4,  spin: 0.08, breathe: 5,  bars: 0.22, live: 0 },
  voice_off: { lobes: 9,  wob: 2,  spin: 0.03, breathe: 3,  bars: 0.08, live: 0 },
};

/* How much the voice moves the reactor, once it has one.
 *
 * The shape above is a state; this is a sentence. Loudness swells the whole
 * ring (SWELL), spikes the lobes (SPIKE) and lights the core, so a long word
 * and a short one no longer look the same -- which was the whole complaint:
 * the reactor changed colour when I started talking and then animated at
 * exactly the rate it always had. */
const SWELL = 26;
const SPIKE = 13;

/* Attack and release. A meter that rises and falls at the same speed reads as
 * a graph; an ear rises fast and lets go slowly, and that asymmetry is most
 * of what makes this look like it is following the voice rather than
 * plotting it. At 60 fps these are per-frame fractions of the distance. */
const ATTACK = 0.5;
const RELEASE = 0.11;

let amp = 0;

/* What the voice is doing at this exact instant, 0..1.
 *
 * While a line is being spoken the page has its whole envelope and the moment
 * playback started, so it reads it off the clock -- sixty times a second, all
 * of them correct, with nothing sent per frame. Otherwise it is the ear, and
 * the ear is whatever last arrived. */
function voiceNow() {
  const L = snap?.level;
  if (snap?.state === "speaking" && L?.env?.length && L.t0) {
    const n = L.env.length;
    let x = (Date.now() / 1000 - L.t0 - (L.lead || 0)) / (L.step || 0.04);
    // Outside the line, silence -- not the last value held. Same rule, and
    // the same slack at either end, as level.py: two windows drawing one
    // sentence must not disagree about when it is over.
    if (x <= -1 || x >= n) return 0;
    x = Math.max(0, Math.min(n - 1, x));
    const i = Math.floor(x);
    if (i >= n - 1) return L.env[n - 1] / 100;
    return (L.env[i] + (L.env[i + 1] - L.env[i]) * (x - i)) / 100;
  }
  return heard;
}

/* One closed periodic path. The radius is a base plus two sines that each
 * complete a whole number of turns around the circle, so the seam where the
 * path closes is invisible. `phase` offsets everything, which is what makes
 * two satellites side by side look like two things rather than one thing
 * drawn twice. */
function ring(cx, cy, base, s, t, phase, steps, shrink = 1) {
  const N = steps;
  // The wobble is in absolute units, so at a fifteenth of the radius it stops
  // being a wobble and becomes a starburst. It scales with the shape, and the
  // lobes thin out with it: thirteen of them around a small circle is noise.
  const wob = s.wob * shrink;
  const lobes = shrink < 1 ? Math.max(5, Math.round(s.lobes * 0.55)) : s.lobes;
  let d = "";
  for (let i = 0; i < N; i++) {
    const a = (i / N) * Math.PI * 2;
    const r = base
      + Math.sin(a * lobes + t * s.spin * 3 + phase) * wob
      + Math.sin(a * (lobes * 2) - t * s.spin * 2 + phase) * wob * 0.28;
    const x = (cx + Math.cos(a) * r).toFixed(1);
    const y = (cy + Math.sin(a) * r).toFixed(1);
    d += `${i ? "L" : "M"}${x},${y}`;
  }
  return d + "Z";
}

/* Subagents, in orbit.
 *
 * Waiting on agents and thinking look identical from the inside, and the word
 * under the reactor already says which it is. This says HOW MANY, at a glance
 * and from across the room -- three small reactors is a number you read
 * without counting a list. They orbit slowly and out of step with each other,
 * because they are separate pieces of work and should not move as one. */
const ORBIT = 133;
const SAT_R = 15;

function satellites(t, s) {
  const n = snap?.agents?.length || 0;
  while (sats.childElementCount > n) sats.lastElementChild.remove();
  while (sats.childElementCount < n) {
    const p = document.createElementNS("http://www.w3.org/2000/svg", "path");
    p.setAttribute("class", "sat");
    p.setAttribute("filter", "url(#glow)");
    sats.appendChild(p);
  }
  if (!n) return;
  for (let i = 0; i < n; i++) {
    // Spread evenly, turning as a set, each breathing on its own clock.
    const a = (i / n) * Math.PI * 2 + t * 0.16;
    const phase = (i * 2.4) % (Math.PI * 2);
    const r = SAT_R + Math.sin(t * 1.6 + phase) * 1.6;
    sats.children[i].setAttribute(
      "d", ring(Math.cos(a) * ORBIT, Math.sin(a) * ORBIT, r, s, t, phase, 60,
                SAT_R / 92));
  }
}

function frame(now) {
  const t = now / 1000;
  const s = SHAPE[snap?.state] || SHAPE.idle;

  const want = s.live ? voiceNow() : 0;
  amp += (want - amp) * (want > amp ? ATTACK : RELEASE);
  const v = amp * s.live;

  // The wobble grows with the voice, so the ring goes from round to jagged
  // on a stressed syllable instead of wobbling by the same amount all the
  // way through a sentence.
  const shape = { ...s, wob: s.wob + SPIKE * v };
  const base = 92 + Math.sin(t * 1.1) * s.breathe + SWELL * v;

  blob.setAttribute("d", ring(0, 0, base, shape, t, 0, 180));
  blob.setAttribute("stroke-width", (3 + 2.6 * v).toFixed(2));
  fill.setAttribute("r", (46 + Math.sin(t * 1.1) * s.breathe * 0.8 + 34 * v).toFixed(1));
  satellites(t, s);

  // The meter reads the same number as the ring, which is the point of
  // having one under the other. Its own texture stays: a bar graph of one
  // value is a block, and the eye reads a block as a broken meter.
  bars(t, s.live ? 0.12 + 0.95 * v : s.bars);
  requestAnimationFrame(frame);
}

/* The meter under the reactor. Loud while something is happening, flat while
   nothing is -- and flat is a statement, which is why stranded is 0.04 and
   not zero: a dead canvas reads as a broken one. */
function bars(t, amp) {
  const { width: w, height: h } = wave.canvas;
  wave.clearRect(0, 0, w, h);
  const hue = getComputedStyle(document.body).getPropertyValue("--hue").trim();
  wave.fillStyle = hue;
  const n = 64, bw = w / n;
  for (let i = 0; i < n; i++) {
    const x = i / n;
    const env = Math.sin(x * Math.PI);          // fade at both ends
    // Two sines that never share a zero, so no bar ever collapses and the
    // meter reads as a meter rather than as a dotted line.
    const v = Math.abs(Math.sin(x * 22 + t * 6)) * 0.6
            + Math.abs(Math.sin(x * 9 - t * 3.4)) * 0.4;
    const bh = Math.max(2, (0.18 + v * 0.82) * env * amp * h);
    wave.globalAlpha = 0.4 + v * 0.45;
    wave.fillRect(i * bw + 1, (h - bh) / 2, bw - 2, bh);
  }
  wave.globalAlpha = 1;
}

/* --- clock and keys --------------------------------------------------- */

setInterval(() => {
  const d = new Date();
  $("clock").textContent = d.toTimeString().slice(0, 5);
  $("date").textContent = d.toLocaleDateString(undefined,
    { weekday: "long", month: "long", day: "numeric" });
}, 1000);

const BIND = { m: "voice", " ": "voice", f: "focus", d: "dictate",
               c: "conversation", t: "session", l: "language", x: "sweep" };

addEventListener("keydown", (e) => {
  if (e.metaKey || e.ctrlKey || e.altKey || e.isComposing) return;
  // Holding m must not toggle the voice sixty times.
  if (e.repeat) return;
  const t = e.target;
  if (t && (t.isContentEditable || /^(input|textarea|select)$/i.test(t.tagName))) return;
  if (e.key === "q") { post("/quit"); return; }
  const a = BIND[e.key.toLowerCase()];
  // Space is an alias for m, and space also scrolls: both need the default
  // taken away, which is why this preventDefaults rather than only acting.
  if (a) { e.preventDefault(); act(a); }
});

// A window opened by a launcher focuses its document, but a click on
// something unfocusable can still leave keydown going nowhere.
document.body.tabIndex = -1;
document.body.focus();
addEventListener("click", () => document.body.focus());

$("clock").textContent = new Date().toTimeString().slice(0, 5);
windowControls();
requestAnimationFrame(frame);
connect();
