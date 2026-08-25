#!/usr/bin/env python3
"""The HUD -- an arc-reactor style status window.

Open it in a spare terminal and leave it running:

    claude-voice hud

It reads the state file the hooks write. It does not talk to Claude directly:
it only watches. Closing it breaks nothing.

  m (or space)  turn the voice off / ON
                turning it off silences whatever is playing, instantly
  h             history: what was said out loud, both sides
  d             dictate: record, transcribe, send to Claude
  t             switch which Claude session receives dictation
  c             conversation mode: continuous listening, sends when you stop
  x             close any orphaned capture (emergency)
  q             quit the HUD (does not affect the voice)

It works out on its own when you are recording with /voice, by reading the
capture state: there is no dictation hook to attach to.

The history pane reads the spoken log (spokenlog.py), which is written where
sound is produced rather than parsed out of the transcript: narration and
acknowledgements never reach the transcript, and a dictated line is
indistinguishable there from a typed one.
"""

import curses
import json
import subprocess
import sys
import math
import textwrap
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import config as _config                              # noqa: E402

try:
    import spokenlog as _spokenlog                     # noqa: E402
except Exception:                                      # an empty pane, not a crash
    _spokenlog = None

CFG = _config.load()
BASE = _config.BASE
STATE = BASE / "state.json"
ENABLED = BASE / "enabled"

FPS = 20.0
IDLE_AFTER = 900          # after 15 min of nothing, treat it as asleep

# Concentric rings of the reactor. Radii in character cells; terminals have
# cells about 2x taller than wide, so X is stretched when drawing.
RINGS = [(3.0, "·"), (5.0, "○"), (7.0, "◦")]

TITLE = (CFG.get("hud.title", "") or CFG.name).strip()
# Letterspaced, the way the status labels are.
TITLE = " ".join(TITLE.upper())


def L(key: str, fallback: str) -> str:
    return CFG.get(f"hud.{key}", fallback) or fallback


_open_cache = {"t": 0.0, "v": False}


def mic_open() -> bool:
    """Is ANYTHING capturing from the microphone? The truth, not what we think.

    Deliberately does NOT consult our pidfile or any state of ours: the
    dangerous case is exactly that one -- an orphaned pw-record after an
    unclean shutdown, with no parent and no pidfile.

    Counts PipeWire capture clients, not ALSA state: PipeWire keeps the PCM
    open for several seconds after the client dies, so ALSA raises false
    alarms. An active input stream really does mean someone is recording now.
    """
    if time.time() - _open_cache["t"] < 1.0:
        return _open_cache["v"]
    val = False
    try:
        out = subprocess.run(["pw-dump"], capture_output=True, text=True, timeout=3).stdout
        objs = json.loads(out)
        val = any(
            o.get("type") == "PipeWire:Interface:Node"
            and o.get("info", {}).get("props", {}).get("media.class") == "Stream/Input/Audio"
            for o in objs)
    except Exception:
        # Without pw-dump, ALSA is the fallback: it errs on the side of warning
        # too much, which is the correct side to err on for a privacy notice.
        try:
            val = any(f.read_text().startswith("state: RUNNING")
                      for f in Path("/proc/asound").glob("card*/pcm*c/sub*/status"))
        except Exception:
            val = False
    _open_cache.update(t=time.time(), v=val)
    return val


def mic_speaking() -> bool:
    """Are you talking RIGHT NOW? Only meaningful in conversation mode."""
    return (BASE / "mic-active").exists()


def daemon_alive() -> bool:
    try:
        import os as _os
        _os.kill(int((BASE / "listen.pid").read_text().strip()), 0)
        return True
    except Exception:
        return False


def sweep_orphans() -> int:
    """Kill captures of ours that were left without an owner."""
    import os as _os, signal
    killed = 0
    me = _os.getpid()
    node = CFG.get("stt.node", "") or ""
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit() or int(proc.name) == me:
            continue
        try:
            # comm is the EXECUTABLE. Matching "pw-record" in the command line
            # kills anything that merely mentions it -- including the shell
            # that launched this script.
            if (proc / "comm").read_text().strip() != "pw-record":
                continue
            cmd = (proc / "cmdline").read_bytes().decode("utf-8", "ignore")
        except Exception:
            continue
        # Ours by signature: either the configured node, or the exact raw
        # capture flags listen.py uses. Never a blanket pw-record kill.
        mine = (node and node in cmd) or ("--raw" in cmd and "--latency" in cmd)
        if not mine:
            continue
        try:
            _os.kill(int(proc.name), signal.SIGTERM)
            killed += 1
        except Exception:
            pass
    return killed


_target_cache = {"t": 0.0, "pane": {}}


def dictate_target_info() -> dict:
    """The target pane as-is: id, path and title. Re-read every 2 s because it
    queries tmux, and the HUD redraws far more often than that."""
    if time.time() - _target_cache["t"] < 2.0:
        return _target_cache["pane"]
    try:
        out = subprocess.run(
            [sys.executable, str(HERE / "dictate.py"), "--target"],
            capture_output=True, text=True, timeout=3).stdout
        _target_cache["pane"] = json.loads(out.strip() or "{}")
    except Exception:
        _target_cache["pane"] = {}
    _target_cache["t"] = time.time()
    return _target_cache["pane"]


def dictate_target() -> str:
    """How that session is shown on screen."""
    p = dictate_target_info()
    if not p:
        return ""
    return f'{p.get("dir", "")} · {p.get("title", "")}'.strip(" ·")


def _thinking():
    """The heartbeat module, which is where agent detection lives."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("thinking", HERE / "thinking.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


_session_cache = {"t": 0.0, "key": None, "sid": "", "cwd": ""}


def target_session() -> tuple:
    """(uuid, directory) of the session the HUD is watching -- the same one
    dictation goes to, the one `t` switches.

    Without this the HUD announced ANY window's agents, so switching to a quiet
    session still claimed there were agents running.
    """
    tgt = dictate_target_info()
    key = (tgt.get("path"), tgt.get("title"))
    if key == _session_cache["key"] and time.time() - _session_cache["t"] < 20:
        return _session_cache["sid"], _session_cache["cwd"]
    sid = ""
    if key[0]:
        try:
            sid = _thinking().session_for(key[0], key[1] or "")
        except Exception:
            sid = ""
    _session_cache.update({"t": time.time(), "key": key, "sid": sid,
                           "cwd": key[0] or ""})
    return sid, key[0] or ""


_agents_cache = {"t": 0.0, "list": []}


def agents_live() -> list:
    """Subagents working right now IN THE VISIBLE SESSION, with descriptions.

    Cached: the HUD redraws 20 times a second and sweeping the disk at that
    rate would be absurd.
    """
    if time.time() - _agents_cache["t"] < 1.5:
        return _agents_cache["list"]
    try:
        sid, cwd = target_session()
        _agents_cache["list"] = _thinking().agents_live(sid, cwd)
    except Exception:
        _agents_cache["list"] = []
    _agents_cache["t"] = time.time()
    return _agents_cache["list"]


_hist_cache = {"mtime": -1.0, "w": -1, "rows": []}


def history_rows(width: int) -> list:
    """The spoken log wrapped to the pane: a list of (text, side) rows.

    Cached on the log's mtime and the width. The HUD redraws 20 times a second
    and the file only changes when something is actually said, so re-reading
    per frame would be pure waste.
    """
    if _spokenlog is None or width < 20:
        return []
    try:
        mt = _spokenlog.mtime()
    except Exception:
        return []
    if mt == _hist_cache["mtime"] and width == _hist_cache["w"]:
        return _hist_cache["rows"]

    try:
        entries = _spokenlog.tail(int(CFG.get("history.show", 200) or 200))
    except Exception:
        entries = []

    you, said = L("history_you", "you"), L("history_said", "said")
    pad = max(len(you), len(said))
    rows = []
    for e in entries:
        mine = e["side"] == "in"
        when = time.strftime("%H:%M", time.localtime(e["t"])) if e["t"] else "     "
        # Who said it is carried by the label, the arrow and the colour: one
        # of the three surviving a narrow terminal or a mono theme is enough.
        head = f'{when}  {(you if mine else said):>{pad}} {"›" if mine else "‹"} '
        body = textwrap.wrap(e["text"], max(8, width - len(head))) or [""]
        rows.append((head + body[0], e["side"]))
        for cont in body[1:]:
            rows.append((" " * len(head) + cont, e["side"]))
    _hist_cache.update(mtime=mt, w=width, rows=rows)
    return rows


def draw_history(win, h, w, scroll, said_color, mine_color) -> int:
    """The history pane, newest at the bottom. Returns the clamped scroll."""
    top, bottom = 5, h - 3
    rows = history_rows(w - 6)
    if not rows:
        centered(win, h // 2, L("history_empty", "nothing spoken yet"), w,
                 curses.A_DIM)
        return 0

    page = max(1, bottom - top + 1)
    scroll = max(0, min(scroll, max(0, len(rows) - page)))
    end = len(rows) - scroll
    start = max(0, end - page)
    shown = rows[start:end]

    y = bottom - len(shown) + 1        # bottom-aligned: the newest line anchors
    for text, side in shown:
        mine = side == "in"
        try:
            win.addstr(y, 3, text[:w - 4],
                       curses.color_pair(mine_color if mine else said_color) |
                       (curses.A_BOLD if mine else curses.A_NORMAL))
        except curses.error:
            pass
        y += 1

    if start > 0:
        centered(win, top - 1, f"↑ {start} older", w, curses.A_DIM)
    if scroll > 0:
        centered(win, h - 2, f"↓ {scroll} newer", w, curses.A_DIM)
    return scroll


def read_state() -> dict:
    try:
        d = json.loads(STATE.read_text())
    except Exception:
        return {"state": "idle", "text": "", "until": 0, "ts": 0}
    # "speaking" expires on its own: the audio is over whether or not anyone
    # said so.
    if d.get("state") == "speaking" and d.get("until", 0) and time.time() > d["until"]:
        d["state"] = "ready"
    return d


def draw_reactor(win, cy, cx, t, state, color):
    """Rings that breathe, spin or pulse depending on state."""
    h, w = win.getmaxyx()
    for radius, glyph in RINGS:
        if state == "thinking":
            # arcs spinning at a different speed per ring
            speed = 2.2 + radius * 0.18
            head = (t * speed) % (2 * math.pi)
            arc = 1.5
            steps = int(radius * 9)
            for i in range(steps):
                a = 2 * math.pi * i / steps
                delta = (a - head) % (2 * math.pi)
                if delta > arc:
                    continue
                bright = 1.0 - delta / arc
                _plot(win, cy, cx, radius, a, glyph, color,
                      curses.A_BOLD if bright > 0.55 else curses.A_DIM, h, w)
        elif state == "listening":
            # Wave travelling INWARD: speaking sends energy out, listening
            # draws it in. Same shape inverted, and legible at a glance.
            phase = 1.0 - ((t * 2.6) % 1.0)
            near = abs((radius / 7.0) - phase) < 0.30
            steps = int(radius * 9)
            for i in range(steps):
                a = 2 * math.pi * i / steps
                _plot(win, cy, cx, radius, a, glyph, color,
                      curses.A_BOLD if near else curses.A_DIM, h, w)
        elif state == "speaking":
            # radial pulse outward, like a voice wave
            phase = (t * 3.4) % 1.0
            near = abs((radius / 7.0) - phase) < 0.28
            steps = int(radius * 9)
            for i in range(steps):
                a = 2 * math.pi * i / steps
                _plot(win, cy, cx, radius, a, glyph, color,
                      curses.A_BOLD if near else curses.A_DIM, h, w)
        else:
            # slow breathing
            breath = (math.sin(t * 0.9) + 1) / 2
            steps = int(radius * 7)
            for i in range(steps):
                a = 2 * math.pi * i / steps
                _plot(win, cy, cx, radius, a, glyph, color,
                      curses.A_BOLD if breath > 0.75 else curses.A_DIM, h, w)


def _plot(win, cy, cx, r, angle, glyph, color, attr, h, w):
    y = int(round(cy + math.sin(angle) * r))
    x = int(round(cx + math.cos(angle) * r * 2))   # x2: cells are not square
    if 0 <= y < h - 1 and 0 <= x < w - 1:
        try:
            win.addstr(y, x, glyph, curses.color_pair(color) | attr)
        except curses.error:
            pass


def draw_bars(win, y, cx, t, state, color, w):
    """VU-style bars. They thrash while speaking, nearly flat otherwise."""
    n = 21
    out = []
    for i in range(n):
        if state == "speaking":
            v = abs(math.sin(t * 7 + i * 0.7)) * abs(math.cos(t * 3.1 + i * 0.35))
        elif state == "listening":
            # slower and shallower than speaking: this is input, not output
            v = abs(math.sin(t * 4.5 + i * 0.9)) * 0.75
        elif state == "thinking":
            v = abs(math.sin(t * 2.2 + i * 0.55)) * 0.45
        else:
            v = 0.06
        out.append("▁▂▃▄▅▆▇█"[min(7, int(v * 8))])
    s = "".join(out)
    x = max(0, cx - len(s) // 2)
    if x + len(s) < w:
        try:
            win.addstr(y, x, s, curses.color_pair(color) |
                       (curses.A_BOLD if state == "speaking" else curses.A_DIM))
        except curses.error:
            pass


def centered(win, y, text, w, attr=0):
    x = max(0, (w - len(text)) // 2)
    try:
        win.addstr(y, x, text[:max(0, w - x - 1)], attr)
    except curses.error:
        pass


def _run(script: str, *args, detach: bool = False) -> None:
    cmd = [sys.executable, str(HERE / script), *args]
    if detach:
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    else:
        subprocess.run(cmd, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, check=False)


def main(stdscr):
    curses.curs_set(0)
    stdscr.nodelay(True)
    curses.use_default_colors()
    for i, fg in enumerate((curses.COLOR_CYAN, curses.COLOR_YELLOW,
                            curses.COLOR_GREEN, curses.COLOR_WHITE,
                            curses.COLOR_MAGENTA, curses.COLOR_BLUE), start=1):
        curses.init_pair(i, fg, -1)
    CYAN, AMBER, GREEN, WHITE, MAGENTA, BLUE = 1, 2, 3, 4, 5, 6

    t0 = time.time()
    silenced_at = 0.0
    history, hist_scroll = False, 0
    while True:
        ch = stdscr.getch()
        if ch == ord("h"):
            history = not history
            hist_scroll = 0
        if ch in (ord("q"), 27):
            # In the pane, q goes back. Quitting the HUD from there would be a
            # trap: you opened a view, you expect to close a view.
            if history:
                history, hist_scroll = False, 0
            else:
                break
        if history:
            if ch in (curses.KEY_UP, ord("k")):
                hist_scroll += 1
            elif ch in (curses.KEY_DOWN, ord("j")):
                hist_scroll -= 1
            elif ch == curses.KEY_PPAGE:
                hist_scroll += 10
            elif ch == curses.KEY_NPAGE:
                hist_scroll -= 10
            elif ch == ord("g"):
                hist_scroll = 10 ** 6      # clamped to the oldest line on draw
            elif ch == ord("G"):
                hist_scroll = 0
            hist_scroll = max(0, hist_scroll)
        if ch == ord("x"):
            sweep_orphans()
        if ch == ord("c"):
            # Conversation mode: toggle the continuous listening daemon.
            pf = BASE / "listen.pid"
            alive = False
            try:
                import os as _os
                _os.kill(int(pf.read_text().strip()), 0); alive = True
            except Exception:
                pf.unlink(missing_ok=True)
            if alive:
                try:
                    import os as _os, signal
                    # the whole group: pw-record is its child
                    _os.killpg(int(pf.read_text().strip()), signal.SIGTERM)
                except Exception:
                    pass
                pf.unlink(missing_ok=True)
                # Verify, do not assume: if the mic is still open after the
                # signal, an orphan was left and has to be swept.
                for _ in range(12):
                    time.sleep(0.15)
                    _open_cache["t"] = 0.0        # no cache while verifying
                    if not mic_open():
                        break
                else:
                    sweep_orphans()
            else:
                _run("listen.py", detach=True)
        if ch == ord("t"):
            _run("dictate.py", "--next")
            # Refresh now, not in 2 s: the first thing you look at after
            # switching session is whether it has agents, and a HUD that lags
            # reads as a HUD that lies.
            _target_cache["t"] = 0.0
            _agents_cache["t"] = 0.0
        if ch == ord("d"):
            # Dictation: record / stop and deliver. Runs detached because
            # transcription takes ~1 s and the HUD must keep animating.
            _run("dictate.py", "--toggle", detach=True)
        if ch in (ord("m"), ord(" ")):
            if ENABLED.exists():
                # Turning it off means SHUT UP NOW, not just "don't speak
                # again". If it is droning mid-answer, this is the key.
                ENABLED.unlink(missing_ok=True)
                _run("voice.py", "silence")
                silenced_at = time.time()
            else:
                ENABLED.parent.mkdir(parents=True, exist_ok=True)
                ENABLED.touch()

        d = read_state()
        st = d.get("state", "idle")
        age = time.time() - d.get("ts", 0) if d.get("ts") else 1e9
        if st in ("thinking", "speaking") and age > IDLE_AFTER:
            st = "idle"
        # You talking wins over whatever I am doing: it is the most immediate
        # thing that can happen on screen.
        if mic_speaking():
            st = "listening"

        label, color = {
            "thinking":  (L("thinking", "T H I N K I N G"), CYAN),
            "speaking":  (L("speaking", "S P E A K I N G"), AMBER),
            "listening": (L("listening", "L I S T E N I N G"), MAGENTA),
            "ready":     (L("ready", "R E A D Y"), GREEN),
        }.get(st, (L("idle", "S T A N D I N G   B Y"), WHITE))

        # Thinking and waiting on agents look the same from inside, but they
        # are not the same thing: if agents are out, the wait has an owner and
        # that gets said. It does not depend on the state file -- agents are
        # detected independently, so this shows even when the session that
        # launched them is not the one speaking.
        agents = agents_live()
        if agents and st not in ("speaking", "listening"):
            label = L("agents", "A G E N T S") + (f"   x{len(agents)}" if len(agents) > 1 else "")
            color = BLUE
            st = "thinking"          # spin the reactor, do not breathe

        t = time.time() - t0
        h, w = stdscr.getmaxyx()
        stdscr.erase()

        if h < 16 or w < 40:
            centered(stdscr, h // 2, "window too small", w, curses.A_DIM)
            stdscr.refresh()
            time.sleep(1 / FPS)
            continue

        if history:
            centered(stdscr, 1, L("history", "H I S T O R Y"), w,
                     curses.color_pair(WHITE) | curses.A_BOLD)
            centered(stdscr, 3,
                     "h/q: back   ·   ↑↓ scroll   ·   PgUp/PgDn: page"
                     "   ·   g/G: oldest/newest", w, curses.A_DIM)
            hist_scroll = draw_history(stdscr, h, w, hist_scroll, AMBER, MAGENTA)
            stdscr.refresh()
            time.sleep(1 / FPS)
            continue

        on = ENABLED.exists()
        if not on and st != "listening" and not agents:
            # With the voice off, saying so is more useful than animating a
            # state. But listening still shows: it does not depend on my voice.
            label, color = (L("voice_off", "V O I C E   O F F"), WHITE)
            st = "idle"

        centered(stdscr, 1, TITLE, w, curses.color_pair(color) | curses.A_BOLD)

        # The key's label says what it WILL DO, not what it is called.
        badge = "  VOICE ON  " if on else "  VOICE OFF  "
        centered(stdscr, 2, badge, w,
                 curses.color_pair(GREEN if on else WHITE) |
                 (curses.A_BOLD | curses.A_REVERSE if on else curses.A_DIM))
        keys = [f"m: {'turn OFF and silence' if on else 'turn the voice ON'}",
                "d: dictate", "c: conversation", "t: session",
                "h: history", "q: quit"]
        # Wide separators while they fit; tighter before letting the last key
        # fall off the edge, which is what an untruncated line is worth here.
        keys = ("   ·   ".join(keys) if len("   ·   ".join(keys)) <= w - 4
                else "  ·  ".join(keys))
        centered(stdscr, 3, keys, w,
                 curses.color_pair(GREEN) | curses.A_BOLD if not on else curses.A_DIM)
        if time.time() - silenced_at < 2.0:
            centered(stdscr, 4, "· voice off, silence ·", w,
                     curses.color_pair(AMBER) | curses.A_BOLD)

        cy = h // 2 - 1
        draw_reactor(stdscr, cy, w // 2, t, st, color)
        centered(stdscr, cy, label, w, curses.color_pair(color) | curses.A_BOLD)
        draw_bars(stdscr, min(h - 4, cy + 9), w // 2, t, st, color, w)

        if agents:
            # What they are doing, not just how many.
            top = max(cy + 11, h - 5 - min(3, len(agents)))
            for i, desc in enumerate(agents[:3]):
                if top + i >= h - 4:
                    break
                centered(stdscr, top + i, f"· {desc}"[:w - 4], w,
                         curses.color_pair(BLUE) | curses.A_DIM)

        # The capture warning is based on the kernel, not on our state, and is
        # shown even when the daemon is dead: an open microphone with no owner
        # is precisely the case worth shouting about.
        if mic_open():
            if daemon_alive():
                centered(stdscr, 5, "  ● CONVERSATION — microphone open  ", w,
                         curses.color_pair(MAGENTA) | curses.A_BOLD | curses.A_REVERSE)
            else:
                centered(stdscr, 5, "  ⚠ MICROPHONE OPEN, NO OWNER — press x  ", w,
                         curses.color_pair(AMBER) | curses.A_BOLD | curses.A_REVERSE)

        tgt = dictate_target()
        if tgt:
            centered(stdscr, h - 4, f"dictation → {tgt}"[:w - 4], w,
                     curses.color_pair(MAGENTA) | curses.A_DIM)

        said = d.get("text", "")
        if said:
            centered(stdscr, h - 2, f'«{said}»'[:w - 4], w,
                     curses.color_pair(GREEN if st != "speaking" else AMBER))

        stdscr.refresh()
        time.sleep(1 / FPS)


if __name__ == "__main__":
    try:
        curses.wrapper(main)
    except KeyboardInterrupt:
        pass
