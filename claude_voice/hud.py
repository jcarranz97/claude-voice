#!/usr/bin/env python3
"""The HUD -- an arc-reactor style status window.

Open it in a spare terminal and leave it running:

    claude-voice hud

It reads the state file the hooks write. It does not talk to Claude directly:
it only watches. Closing it breaks nothing.

  m (or space)  turn the voice off / ON
                turning it off silences whatever is playing, instantly
  l             language: cycle to the next preset, and relabel in place
  h             history: show/hide what was said out loud, both sides
  d             dictate: record, transcribe, send to Claude
  t             switch which Claude session receives dictation
  c             conversation mode: continuous listening, sends when you stop
  x             close any orphaned capture (emergency)
  q             quit the HUD (does not affect the voice)

It works out on its own when you are recording with /voice, by reading the
capture state: there is no dictation hook to attach to.

d and c are refused, with the reason on screen, when no Claude Code session can
receive the text: recording into a void looks exactly like not being heard. l
is refused the same way when the other language's voice was never downloaded:
switching into a preset that cannot speak is the same kind of silent failure.

The labels on this window come from the preset, so l has to reload the config
and recompute them in place -- quitting and reopening the HUD is not an
acceptable answer to a keystroke.

The history panel reads the spoken log (spokenlog.py), which is written where
sound is produced rather than parsed out of the transcript: narration and
acknowledgements never reach the transcript, and a dictated line is
indistinguishable there from a typed one. It shows the session being watched,
the same one `t` switches, so the panel is one conversation and not every
window on the machine interleaved by the clock.
"""

import curses
import json
import os
import signal
import subprocess
import sys
import math
import textwrap
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import config as _config                              # noqa: E402
import focus as _focus                                # noqa: E402
import presence as _presence                          # noqa: E402
import lang as _lang                                  # noqa: E402
# Every microphone question -- who has it, whether anyone is actually
# being recorded, and how to close a capture of ours that was left
# behind -- is answered in one place, because the watchdog on the
# systemd timer has to give the same answers this window does.
from mic import (our_captures, mic_open, mic_speaking,      # noqa: E402,F401
                 mic_held, daemon_alive, sweep_orphans, listen_stranded)

try:
    import spokenlog as _spokenlog                     # noqa: E402
except Exception:                                      # an empty pane, not a crash
    _spokenlog = None

CFG = _config.load()
BASE = _config.BASE
# The SPEAKER's state: global, because there is one pair of speakers. What
# each session is doing lives in turn.py, one file per session.
STATE = BASE / "state.json"
ENABLED = BASE / "enabled"
# Whether the history panel was open when you last had a HUD up. A marker
# file, the same way the voice switch is one: the HUD keeps no other state.
PANEL_OPEN = BASE / "hud-history"

FPS = 20.0
IDLE_AFTER = 900          # after 15 min of nothing, treat it as asleep

# The history panel shares the window with the reactor. When there is no
# honest way to show both, it takes the window instead.
SPLIT_MIN_W = 74          # narrower than this, no room beside the reactor
PANEL_MIN_W = 34
PANEL_MAX_W = 60
PANEL_MIN_H = 6           # shorter than this, a bottom strip is not worth it
BODY_TOP = 4              # the title and key legend span the window above it
REACTOR_ROWS = 19         # rings (15), the bars under them, the target line

CYAN, AMBER, GREEN, WHITE, MAGENTA, BLUE = 1, 2, 3, 4, 5, 6

# Concentric rings of the reactor. Radii in character cells; terminals have
# cells about 2x taller than wide, so X is stretched when drawing.
RINGS = [(3.0, "·"), (5.0, "○"), (7.0, "◦")]

TITLE = (CFG.get("hud.title", "") or CFG.name).strip()
# Letterspaced, the way the status labels are.
TITLE = " ".join(TITLE.upper())


def L(key: str, fallback: str) -> str:
    return CFG.get(f"hud.{key}", fallback) or fallback


def reload_cfg() -> None:
    """Pick up a language switch without being reopened.

    CFG and TITLE are resolved once at import, which is right for every other
    module here -- they are short-lived processes -- and wrong for the one
    window that stays open across a switch. The modules imported above keep
    their own copies, but nothing they answer (which microphone, where the
    panel sits) is a language question.
    """
    global CFG, TITLE
    CFG = _config.load(reload=True)
    TITLE = " ".join(((CFG.get("hud.title", "") or CFG.name).strip()).upper())


_lang_cache = {"preset": None, "name": "", "label": ""}


def next_language() -> tuple:
    """(preset, what that language calls itself) for the one `l` switches to.

    Cached against the active preset: the legend is redrawn twenty times a
    second and the answer is two TOML files off disk.
    """
    if _lang_cache["preset"] != CFG.preset:
        nxt = _lang.following(CFG.preset)
        _lang_cache.update(preset=CFG.preset, name=nxt,
                           label=_lang.label(nxt) if nxt else "")
    return _lang_cache["name"], _lang_cache["label"]


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
    if not p.get("ok"):
        return ""
    return f'{p.get("dir", "")} · {p.get("title", "")}'.strip(" ·")


_focus_cache = {"t": 0.0, "val": ("", ""), "pane": ""}


def focus_state(fresh: bool = False) -> tuple:
    """(state, label) for the focused pane, cached like every tmux question.

    state is "" when nothing is focused, "live" when the focused pane still has
    a claude in it, and "gone" when it does not. The last one is why this asks
    tmux at all rather than reading the focus file: a focus pointing at a
    window that has been closed means the voice is silent EVERYWHERE, and a HUD
    that shows that as ordinary quiet is a HUD that lies.
    """
    if fresh:
        _focus_cache["t"] = 0.0
    if time.time() - _focus_cache["t"] < 2.0:
        return _focus_cache["val"]
    pane = _focus.pane()
    _focus_cache["pane"] = pane
    if not pane:
        _focus_cache.update(t=time.time(), val=("", ""))
        return _focus_cache["val"]
    try:
        panes = _mod("dictate").claude_panes()
    except Exception:
        panes = []
    p = next((q for q in panes if q.get("pane_id") == pane), None)
    live = f'{p["dir"]} · {p["title"]}'.strip(" ·") if p else ""
    _focus_cache.update(t=time.time(),
                        val=("live", live) if p else ("gone", _focus.label() or pane))
    return _focus_cache["val"]


def focus_here() -> bool:
    """Is the focused pane the one dictation is aimed at? They are moved
    together, so a no means somebody pointed one of them somewhere else.

    Through the cache, not the file: this is asked on every frame, and the HUD
    draws twenty of those a second.
    """
    focus_state()
    pane = _focus_cache["pane"]
    return bool(pane) and dictate_target_info().get("pane_id") == pane


def dictate_blocked(fresh: bool = False) -> str:
    """Why nothing can be dictated, or "" when something can.

    Drawn as a WARNING rather than as a missing line: an absent footer reads
    as ordinary chrome, and "nobody is listening" is the one thing the HUD
    exists to make obvious before you start talking.
    """
    if fresh:
        # A key press is worth one tmux query: refusing because of a
        # two-second-old cache, right after the session was opened, would be
        # the same lie in the other direction.
        _target_cache["t"] = 0.0
    p = dictate_target_info()
    if p.get("ok"):
        return ""
    return p.get("why") or "no Claude Code session"


_mods = {}


def _mod(name: str):
    """Load a sibling module once and keep it. read_state() runs on every
    frame, and re-executing a module twenty times a second is pure waste."""
    if name not in _mods:
        import importlib.util
        spec = importlib.util.spec_from_file_location(name, HERE / f"{name}.py")
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        _mods[name] = m
    return _mods[name]


def _thinking():
    """The heartbeat module, which is where agent detection lives."""
    return _mod("thinking")


_session_cache = {"t": 0.0, "key": None, "sid": "", "cwd": ""}


def target_session() -> tuple:
    """(uuid, directory) of the session the HUD is watching -- the same one
    dictation goes to, the one `t` switches.

    Without this the HUD announced ANY window's agents, so switching to a quiet
    session still claimed there were agents running.
    """
    tgt = dictate_target_info()
    key = (tgt.get("path"), tgt.get("title"), tgt.get("pane_id"))
    if key == _session_cache["key"] and time.time() - _session_cache["t"] < 20:
        return _session_cache["sid"], _session_cache["cwd"]
    sid = ""
    if key[0]:
        try:
            sid = _thinking().session_for(key[0], key[1] or "", key[2] or "")
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


_hist_sid = {"t": 0.0, "key": None, "sid": ""}


def history_session() -> str:
    """Which conversation the panel is showing.

    The pane's session when its title names one, and spokenlog.follow() for
    when it does not -- the same policy `claude-voice history` follows, so the
    two never disagree about whose history you are reading.
    """
    sid, cwd = target_session()
    if sid:
        return sid
    if time.time() - _hist_sid["t"] < 2.0 and cwd == _hist_sid["key"]:
        return _hist_sid["sid"]              # the panel asks 20 times a second
    try:
        sid = _spokenlog.follow("", cwd)
    except Exception:
        sid = ""
    _hist_sid.update(t=time.time(), key=cwd, sid=sid)
    return sid


_hist_cache = {"mtime": -1.0, "w": -1, "sid": None, "rows": []}


def history_rows(width: int) -> list:
    """The spoken log wrapped to the panel: (text, side, continuation) rows.

    The log of the session being watched -- the one `t` switches, the one
    dictation reaches -- as resolved by history_session(). Empty when that
    session has said nothing yet, which is the honest answer: a panel that
    borrows another conversation to look busy is worse than a blank one.

    Cached on the log's mtime, the width and the session. The HUD redraws 20
    times a second and the file only changes when something is actually said,
    so re-reading per frame would be pure waste.
    """
    if _spokenlog is None or width < 20:
        return []
    try:
        sid = history_session()
        mt = _spokenlog.mtime(sid)
    except Exception:
        return []
    if (mt == _hist_cache["mtime"] and width == _hist_cache["w"]
            and sid == _hist_cache["sid"]):
        return _hist_cache["rows"]

    try:
        entries = _spokenlog.tail(int(CFG.get("history.show", 200) or 200), sid)
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
        rows.append((head + body[0], e["side"], False))
        for cont in body[1:]:
            rows.append((" " * len(head) + cont, e["side"], True))
    _hist_cache.update(mtime=mt, w=width, sid=sid, rows=rows)
    return rows


def panel_open() -> bool:
    try:
        return PANEL_OPEN.exists()
    except Exception:
        return False


def set_panel_open(on: bool) -> None:
    """Remember the panel across restarts. Never worth an exception."""
    try:
        if on:
            PANEL_OPEN.parent.mkdir(parents=True, exist_ok=True)
            PANEL_OPEN.touch()
        else:
            PANEL_OPEN.unlink(missing_ok=True)
    except Exception:
        pass


def position() -> str:
    """Where the panel sits: left, right or bottom. Anything else reads left."""
    pos = str(CFG.get("history.position", "left") or "left").strip().lower()
    return pos if pos in ("left", "right", "bottom") else "left"


def layout(h: int, w: int, history: bool) -> tuple:
    """Where the panel and the reactor go, as (top, bottom, x0, width) bands.

    Returns (panel, reactor, divider). The panel is a panel, not a mode: the
    reactor keeps animating beside or above it. Only when the window cannot
    hold both is `reactor` None, and the caller falls back to the panel alone.
    The divider is ("v", column) or ("h", row), or None.
    """
    body = (BODY_TOP, h - 1, 0, w)
    if not history:
        return None, body, None

    pos = position()
    if pos == "bottom":
        # The strip takes what is left once the reactor has its rows and the
        # divider has its one.
        ph = min(max(PANEL_MIN_H, h // 3), h - BODY_TOP - REACTOR_ROWS - 1)
        if ph >= PANEL_MIN_H:
            top = h - ph
            return (top, h - 1, 0, w), (BODY_TOP, top - 2, 0, w), ("h", top - 1)
    elif w >= SPLIT_MIN_W:
        pw = max(PANEL_MIN_W, min(PANEL_MAX_W, w * 2 // 5))
        if pos == "right":
            return ((BODY_TOP, h - 1, w - pw, pw),
                    (BODY_TOP, h - 1, 0, w - pw - 1), ("v", w - pw - 1))
        return ((BODY_TOP, h - 1, 0, pw),
                (BODY_TOP, h - 1, pw + 1, w - pw - 1), ("v", pw))

    return (0, h - 1, 0, w), None, None      # no room to share: take the window


def draw_divider(win, divider, h: int, w: int) -> None:
    kind, at = divider
    if kind == "v":
        for y in range(BODY_TOP, h - 1):
            try:
                win.addstr(y, at, "│", curses.A_DIM)
            except curses.error:
                pass
    else:
        try:
            win.addstr(at, 1, "─" * max(0, w - 2), curses.A_DIM)
        except curses.error:
            pass


def draw_panel(win, band, strip: bool, scroll: int) -> int:
    """Title, hint and log inside the panel band. Returns the clamped scroll.

    A bottom strip is short, so it spends no row on the hint: the key legend
    above it already says what h does, and the arrows are the obvious guess.
    """
    top, bottom, x0, w = band
    centered(win, top if strip else top + 1, L("history", "H I S T O R Y"), w,
             curses.color_pair(WHITE) | curses.A_BOLD, x0)
    if not strip:
        centered(win, top + 2, "↑↓ scroll  ·  g/G: ends", w, curses.A_DIM, x0)
    first = top + 2 if strip else top + 4
    return draw_history(win, first, bottom - 1, x0, w, scroll, AMBER, MAGENTA)


def draw_history(win, top, bottom, x0, w, scroll, said_color, mine_color) -> int:
    """The spoken log in the band [x0, x0 + w) between rows top and bottom.

    Newest at the bottom, and bottom-aligned, so a short log sits where the
    next line will appear rather than floating at the top. Returns the clamped
    scroll. The rows just outside the band carry the "more above/below" marks,
    so the caller leaves one spare row at each end.
    """
    rows = history_rows(w - 4)
    if not rows:
        centered(win, (top + bottom) // 2, L("history_empty", "nothing spoken yet"),
                 w, curses.A_DIM, x0)
        return 0

    page = max(1, bottom - top + 1)
    scroll = max(0, min(scroll, max(0, len(rows) - page)))
    end = len(rows) - scroll
    start = max(0, end - page)
    # Never open on a dangling continuation line: a wrapped sentence with its
    # head scrolled off reads as someone else's line.
    while start < end - 1 and rows[start][2]:
        start += 1
    shown = rows[start:end]

    y = bottom - len(shown) + 1        # bottom-aligned: the newest line anchors
    for text, side, _ in shown:
        mine = side == "in"
        try:
            win.addstr(y, x0 + 2, text[:w - 3],
                       curses.color_pair(mine_color if mine else said_color) |
                       (curses.A_BOLD if mine else curses.A_NORMAL))
        except curses.error:
            pass
        y += 1

    if start > 0:
        centered(win, top - 1, f"↑ {start} older", w, curses.A_DIM, x0)
    if scroll > 0:
        centered(win, bottom + 1, f"↓ {scroll} newer", w, curses.A_DIM, x0)
    return scroll


def read_state() -> dict:
    """What the session on screen is doing, with the speaker laid over it.

    Two questions, two files. What a SESSION is doing is per session (turn.py),
    because three windows and a bot can all be busy at once and only one of
    them is the one being watched. What the SPEAKER is doing is global -- there
    is one pair of them -- so it wins while it is playing, but only when the
    line belongs to the session on screen.

    Before this they shared one state.json, and whichever session finished
    first wrote "ready" over everyone: the reactor went calm while the window
    in front of you was still working.
    """
    turn = _mod("turn")
    sid, _ = target_session()
    try:
        # Unknown session (no tmux, no pane title yet): fall back to the
        # liveliest one, which is the guess the HUD made before any of this.
        d = turn.read(sid) if sid else turn.newest()
    except Exception:
        d = {"state": "idle", "text": "", "until": 0, "ts": 0, "session": ""}

    try:
        sp = json.loads(STATE.read_text())
    except Exception:
        return d
    # "speaking" expires on its own: the audio is over whether or not anyone
    # said so. Past that, the session's own state is the honest answer.
    if sp.get("state") != "speaking":
        return d
    if sp.get("until", 0) and time.time() > sp["until"]:
        return d
    owner = sp.get("session", "")
    # No owner recorded (the CLI, or a queue item from before this change):
    # show it rather than swallow it. Silence would be the worse error.
    if owner and sid and owner != sid:
        return d
    return sp


def draw_reactor(win, cy, cx, t, state, color, band=None):
    """Rings that breathe, spin or pulse depending on state.

    Clipped to the band (top, bottom, x0, width) so a ring never bleeds into
    the history panel, whichever edge it is on.
    """
    h, w = win.getmaxyx()
    y0, y1, x0, x1 = (0, h - 1, 0, w) if band is None else (
        band[0], band[1] + 1, band[2], min(band[2] + band[3], w))
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
                      curses.A_BOLD if bright > 0.55 else curses.A_DIM, y0, y1, x0, x1)
        elif state == "listening":
            # Wave travelling INWARD: speaking sends energy out, listening
            # draws it in. Same shape inverted, and legible at a glance.
            phase = 1.0 - ((t * 2.6) % 1.0)
            near = abs((radius / 7.0) - phase) < 0.30
            steps = int(radius * 9)
            for i in range(steps):
                a = 2 * math.pi * i / steps
                _plot(win, cy, cx, radius, a, glyph, color,
                      curses.A_BOLD if near else curses.A_DIM, y0, y1, x0, x1)
        elif state == "speaking":
            # radial pulse outward, like a voice wave
            phase = (t * 3.4) % 1.0
            near = abs((radius / 7.0) - phase) < 0.28
            steps = int(radius * 9)
            for i in range(steps):
                a = 2 * math.pi * i / steps
                _plot(win, cy, cx, radius, a, glyph, color,
                      curses.A_BOLD if near else curses.A_DIM, y0, y1, x0, x1)
        else:
            # slow breathing
            breath = (math.sin(t * 0.9) + 1) / 2
            steps = int(radius * 7)
            for i in range(steps):
                a = 2 * math.pi * i / steps
                _plot(win, cy, cx, radius, a, glyph, color,
                      curses.A_BOLD if breath > 0.75 else curses.A_DIM, y0, y1, x0, x1)


def _plot(win, cy, cx, r, angle, glyph, color, attr, y0, y1, x0, x1):
    y = int(round(cy + math.sin(angle) * r))
    x = int(round(cx + math.cos(angle) * r * 2))   # x2: cells are not square
    if y0 <= y < y1 and x0 <= x < x1 - 1:
        try:
            win.addstr(y, x, glyph, curses.color_pair(color) | attr)
        except curses.error:
            pass


def draw_bars(win, y, cx, t, state, color, w, x0=0):
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
    x = max(x0, cx - len(s) // 2)
    if x + len(s) < w:
        try:
            win.addstr(y, x, s, curses.color_pair(color) |
                       (curses.A_BOLD if state == "speaking" else curses.A_DIM))
        except curses.error:
            pass


def centered(win, y, text, w, attr=0, x0=0):
    """Centre text in the column band [x0, x0 + w). w is the band, not the screen."""
    x = max(0, (w - len(text)) // 2)
    try:
        win.addstr(y, x0 + x, text[:max(0, w - x - 1)], attr)
    except curses.error:
        pass


LISTEN_PID = BASE / "listen.pid"


def conversation_alive() -> bool:
    """Is the continuous listening daemon running? A pidfile is not an answer:
    a session that died leaves one behind, and nothing would ever start again."""
    try:
        os.kill(int(LISTEN_PID.read_text().strip()), 0)
        return True
    except Exception:
        LISTEN_PID.unlink(missing_ok=True)
        return False


def conversation_stop() -> None:
    """Stop it, and make sure the microphone actually closed."""
    try:
        # The whole group: pw-record is its child.
        os.killpg(int(LISTEN_PID.read_text().strip()), signal.SIGTERM)
    except Exception:
        pass
    LISTEN_PID.unlink(missing_ok=True)
    # Verify, do not assume: if the mic is still open after the signal, an
    # orphan was left and has to be swept.
    for _ in range(12):
        time.sleep(0.15)
        # fresh=True: the one-second cache is what makes the HUD cheap, and
        # exactly what must not be trusted here.
        if not mic_open(fresh=True):
            break
    else:
        sweep_orphans()


def conversation_start() -> None:
    _run("listen.py", detach=True)


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

    t0 = time.time()
    # One transient line under the legend: the voice was silenced, the
    # language changed. Whatever it says, it says it for two seconds.
    notice_at, notice = 0.0, ""
    refused_at, refused_why = 0.0, ""
    # Reopen the way you left it: h is a preference, not a per-run decision.
    history, hist_scroll = panel_open(), 0
    while True:
        ch = stdscr.getch()
        if ch == ord("h"):
            history = not history
            hist_scroll = 0
            set_panel_open(history)
        if ch in (ord("q"), 27):
            # h opens the panel and h closes it, so q keeps meaning quit. The
            # exception is a window too narrow to split, where the panel really
            # is a view you are inside: there, q goes back.
            if history and stdscr.getmaxyx()[1] < SPLIT_MIN_W:
                history, hist_scroll = False, 0
                set_panel_open(False)
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
            alive = conversation_alive()
            # Starting is refused; stopping never is, or a daemon left over
            # from a session that has since closed could not be killed here.
            blocked_now = "" if alive else dictate_blocked(fresh=True)
            if blocked_now:
                refused_at = time.time()
                refused_why = f"{blocked_now} — nothing to dictate to"
            elif alive:
                conversation_stop()
            else:
                conversation_start()
        if ch == ord("l"):
            # Language: the next preset in the cycle, relabelled in place.
            listening = conversation_alive()
            ok, msg = _lang.switch_next()
            if not ok:
                refused_at, refused_why = time.time(), msg
            else:
                reload_cfg()
                notice_at, notice = time.time(), f"· {msg} ·"
                # Everything else reads the config per invocation and follows
                # on its own. This daemon read its Whisper language when it
                # started, so it is the one thing a switch has to restart.
                if listening:
                    conversation_stop()
                    conversation_start()
        if ch == ord("t"):
            _run("dictate.py", "--next")
            # Refresh now, not in 2 s: the first thing you look at after
            # switching session is whether it has agents, and a HUD that lags
            # reads as a HUD that lies.
            _target_cache["t"] = 0.0
            _agents_cache["t"] = 0.0
            if _focus.pane():
                # The voice follows the session you switched to. Leaving it
                # behind would mean typing into one window while another one
                # answers out loud, which is two settings pretending to be one.
                tgt = dictate_target_info()
                if tgt.get("pane_id"):
                    _focus.set_pane(tgt["pane_id"], dictate_target())
                    _run("voice.py", "silence")
                    focus_state(fresh=True)
        if ch == ord("f"):
            # Focus: only the session dictation points at gets to speak.
            if _focus.pane():
                _focus.clear()
                focus_state(fresh=True)
                notice_at, notice = time.time(), "· every session speaks ·"
            else:
                # One tmux query is worth it: focusing the session named by a
                # two-second-old cache is how the voice ends up in the window
                # you just switched away from.
                _target_cache["t"] = 0.0
                tgt = dictate_target_info()
                if not tgt.get("pane_id"):
                    refused_at = time.time()
                    refused_why = (dictate_blocked(fresh=True)
                                   or "no session to focus")
                else:
                    _focus.set_pane(tgt["pane_id"], dictate_target())
                    # The other windows may be mid-sentence right now, and
                    # "only this one" should not have to wait out a paragraph.
                    _run("voice.py", "silence")
                    focus_state(fresh=True)
                    notice_at, notice = time.time(), "· this session only ·"
        if ch == ord("d"):
            # Dictation: record / stop and deliver. Runs detached because
            # transcription takes ~1 s and the HUD must keep animating.
            # A recording already under way is always allowed to stop: the
            # refusal is about opening the microphone, not closing it.
            blocked_now = ("" if (BASE / "dictate.pid").exists()
                           else dictate_blocked(fresh=True))
            if blocked_now:
                refused_at = time.time()
                refused_why = f"{blocked_now} — nothing to dictate to"
            else:
                _run("dictate.py", "--toggle", detach=True)
        if ch in (ord("m"), ord(" ")):
            if ENABLED.exists():
                # Turning it off means SHUT UP NOW, not just "don't speak
                # again". If it is droning mid-answer, this is the key.
                ENABLED.unlink(missing_ok=True)
                _run("voice.py", "silence")
                notice_at, notice = time.time(), "· voice off, silence ·"
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
        # ...but not when there is nowhere to deliver what is being said. The
        # microphone being on is not the same as being listened to, and while
        # conversation mode is holding, "LISTENING" is the one word on this
        # screen that would be a lie. The rings stop drawing energy inward and
        # the meter goes flat with it: nothing IS coming in.
        stranded = listen_stranded() if daemon_alive() else ""
        if stranded:
            st = "stranded"

        label, color = {
            "thinking":  (L("thinking", "T H I N K I N G"), CYAN),
            "speaking":  (L("speaking", "S P E A K I N G"), AMBER),
            "listening": (L("listening", "L I S T E N I N G"), MAGENTA),
            "stranded":  (L("stranded", "N O T   L I S T E N I N G"), AMBER),
            "ready":     (L("ready", "R E A D Y"), GREEN),
        }.get(st, (L("idle", "S T A N D I N G   B Y"), WHITE))

        # Thinking and waiting on agents look the same from inside, but they
        # are not the same thing: if agents are out, the wait has an owner and
        # that gets said. It does not depend on the state file -- agents are
        # detected independently, so this shows even when the session that
        # launched them is not the one speaking.
        agents = agents_live()
        if agents and st not in ("speaking", "listening", "stranded"):
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

        panel, reactor, divider = layout(h, w, history)
        if reactor is None:
            # No room to share: the panel takes the window, as a view.
            centered(stdscr, 1, L("history", "H I S T O R Y"), w,
                     curses.color_pair(WHITE) | curses.A_BOLD)
            centered(stdscr, 3, "h/q: back  ·  ↑↓ scroll  ·  g/G: ends", w,
                     curses.A_DIM)
            hist_scroll = draw_history(stdscr, 5, h - 3, 0, w,
                                       hist_scroll, AMBER, MAGENTA)
            stdscr.refresh()
            time.sleep(1 / FPS)
            continue

        on = ENABLED.exists()
        if not on and st not in ("listening", "stranded") and not agents:
            # With the voice off, saying so is more useful than animating a
            # state. But listening still shows: it does not depend on my voice.
            label, color = (L("voice_off", "V O I C E   O F F"), WHITE)
            st = "idle"

        centered(stdscr, 1, TITLE, w, curses.color_pair(color) | curses.A_BOLD)

        # The key's label says what it WILL DO, not what it is called.
        fstate, flabel = focus_state()
        badge = "  VOICE ON  " if on else "  VOICE OFF  "
        if on and fstate:
            # The switch alone stops being the whole truth once a pane owns
            # the voice: ON everywhere and ON in one window look the same
            # from every other window, and that is the confusing case.
            badge = "  VOICE ON · ONE SESSION  "
        centered(stdscr, 2, badge, w,
                 curses.color_pair(GREEN if on else WHITE) |
                 (curses.A_BOLD | curses.A_REVERSE if on else curses.A_DIM))
        # Named after what it WILL DO, like the others: the language you get
        # by pressing it, written in that language. Hidden when there is no
        # other language with a voice on disk -- a key that can only refuse.
        other, other_label = next_language()

        def legend(voice_label: str, focus_label: str, sep: str) -> str:
            # The two silencing keys sit together, in scope order: m takes the
            # machine, f takes everything except the session t points at.
            # Apart, f read as a view control and got pressed as one.
            row = [f"m: {voice_label}", f"f: {focus_label}",
                   "d: dictate", "c: conversation", "t: session"]
            if other and other != CFG.preset:
                row.append(f"l: {other_label}")
            row += [f"h: {'hide history' if history else 'history'}", "q: quit"]
            return sep.join(row)

        # Wide separators while they fit, then tighter, then the one label
        # long enough to be worth shortening -- in that order, because losing
        # the space between keys costs less than losing a key off the edge.
        full = "turn OFF and silence" if on else "turn the voice ON"
        short = "OFF, silence" if on else "voice ON"
        f_full = "unmute the rest" if fstate else "mute the rest"
        f_short = "unmute rest" if fstate else "mute rest"
        for voice_label, focus_label, sep in (
                (full, f_full, "   ·   "), (full, f_full, "  ·  "),
                (full, f_full, " · "), (short, f_full, " · "),
                (short, f_short, " · ")):
            keys = legend(voice_label, focus_label, sep)
            if len(keys) <= w - 4:
                break
        centered(stdscr, 3, keys, w,
                 curses.color_pair(GREEN) | curses.A_BOLD if not on else curses.A_DIM)
        if time.time() - refused_at < 2.5:
            # Louder than the footer, and only right after the key was
            # pressed: the answer to "I pressed d and nothing happened".
            centered(stdscr, 4, f"  ⚠ {refused_why}  ", w,
                     curses.color_pair(AMBER) | curses.A_BOLD | curses.A_REVERSE)
        elif time.time() - notice_at < 2.0:
            centered(stdscr, 4, notice, w,
                     curses.color_pair(AMBER) | curses.A_BOLD)

        if panel:
            # Below the legend, which spans the window: the panel is beside or
            # under the reactor, never instead of it.
            hist_scroll = draw_panel(stdscr, panel, divider[0] == "h",
                                     hist_scroll)
            draw_divider(stdscr, divider, h, w)

        rtop, rbot, x0, cw = reactor
        strip = bool(panel) and divider[0] == "h"
        # Under a bottom strip the rows are tight, so the notice moves onto the
        # divider -- a labelled rule reads better than a plain one -- and the
        # last spoken line goes, because the strip below ends with that line.
        notice_y = divider[1] if strip else rtop + 1
        foot_y = rbot if strip else rbot - 3
        cy = (rtop + rbot) // 2 - 1
        cx = x0 + cw // 2
        draw_reactor(stdscr, cy, cx, t, st, color, reactor)
        # Centred on the reactor's own axis, not on the band: rounding the two
        # separately leaves the label half a cell off and a ring glyph peeking
        # out of its last letter.
        try:
            stdscr.addstr(cy, max(x0, cx - len(label) // 2), label[:cw - 1],
                          curses.color_pair(color) | curses.A_BOLD)
        except curses.error:
            pass
        draw_bars(stdscr, min(foot_y - 1, cy + 9), cx, t, st, color,
                  x0 + cw, x0)

        if agents:
            # What they are doing, not just how many.
            top = max(cy + 11, foot_y - 1 - min(3, len(agents)))
            for i, desc in enumerate(agents[:3]):
                if top + i >= foot_y:
                    break
                centered(stdscr, top + i, f"· {desc}"[:cw - 4], cw,
                         curses.color_pair(BLUE) | curses.A_DIM, x0)

        # The capture warning is based on the kernel, not on our state, and is
        # shown even when the daemon is dead: an open microphone with no owner
        # is precisely the case worth shouting about.
        if mic_open():
            if stranded:
                # The microphone IS open and nothing is on the other end.
                # Said in the loudest slot the HUD has, and said differently
                # while you are mid-sentence, because that is the moment the
                # silence would otherwise be mistaken for being listened to.
                msg = (f"  ⚠ {stranded} — you are talking to nothing  "
                       if mic_speaking() else
                       f"  ⚠ {stranded} — conversation on hold  ")
                centered(stdscr, notice_y, msg[:cw - 2], cw,
                         curses.color_pair(AMBER) | curses.A_BOLD | curses.A_REVERSE,
                         x0)
            elif daemon_alive():
                centered(stdscr, notice_y, "  ● CONVERSATION — microphone open  ", cw,
                         curses.color_pair(MAGENTA) | curses.A_BOLD | curses.A_REVERSE,
                         x0)
            else:
                centered(stdscr, notice_y, "  ⚠ MICROPHONE OPEN, NO OWNER — press x  ", cw,
                         curses.color_pair(AMBER) | curses.A_BOLD | curses.A_REVERSE,
                         x0)
        else:
            # Nothing is recording, but somebody may still be holding the
            # microphone open. Said plainly, and without the alarm: there is
            # nothing wrong here that a key of ours could fix, and the only
            # reason to show it at all is that the desktop's indicator is lit
            # and this is the sentence that explains it.
            held = mic_held()
            if held:
                centered(stdscr, notice_y,
                         f"  ◦ mic held open by {held[0]} — not recording  ", cw,
                         curses.color_pair(WHITE) | curses.A_DIM, x0)

        tgt = dictate_target()
        blocked = dictate_blocked()
        if fstate == "gone":
            # Nothing speaks anywhere until this is cleared or moved, and no
            # other line on screen would say why.
            centered(stdscr, foot_y,
                     f"⚠ voice held by {flabel} — that pane is gone, press f"[:cw - 4],
                     cw, curses.color_pair(AMBER) | curses.A_BOLD, x0)
        elif fstate and not focus_here():
            # Talking into one window while another answers. Only reachable by
            # aiming dictation from somewhere else, but worth naming when it is.
            centered(stdscr, foot_y,
                     f"⚠ voice → {flabel} · dictation → {tgt or '—'}"[:cw - 4],
                     cw, curses.color_pair(AMBER) | curses.A_BOLD, x0)
        elif tgt:
            centered(stdscr, foot_y,
                     f"{'voice + dictation' if fstate else 'dictation'} → {tgt}"[:cw - 4],
                     cw, curses.color_pair(MAGENTA) | curses.A_DIM, x0)
        elif blocked:
            centered(stdscr, foot_y,
                     f"⚠ {blocked} — dictation disabled"[:cw - 4], cw,
                     curses.color_pair(AMBER) | curses.A_BOLD, x0)

        said = d.get("text", "")
        if said and not strip:
            centered(stdscr, rbot - 1, f'«{said}»'[:cw - 4], cw,
                     curses.color_pair(GREEN if st != "speaking" else AMBER), x0)

        stdscr.refresh()
        time.sleep(1 / FPS)


def shutdown() -> None:
    """Leave nothing of ours running.

    The window IS the application: what it started, it takes with it. The
    microphone first, because a capture with no window on screen is the one
    that frightens people, then anything queued or playing, then the tick
    loops and acknowledgements that live in other processes -- silence_all()
    is the same sweep the panic button does, including the walk through /proc
    for players whose pidfile was lost.

    Skipped while another HUD is still up: two terminals are two windows, and
    closing one of them is not closing the application.
    """
    _presence.leave()
    if not _presence.last_one_out():
        return
    try:
        if conversation_alive():
            conversation_stop()
    except Exception:
        pass
    try:
        _run("voice.py", "silence")     # waited on: we are on the way out
    except Exception:
        pass
    try:
        sweep_orphans()
    except Exception:
        pass


def _bye(signum, frame):
    """A closed terminal sends SIGHUP and a killed one SIGTERM, and neither
    runs a `finally` on its own -- which is how the microphone was left open
    by the exact exit that most needed it closed."""
    raise SystemExit(0)


if __name__ == "__main__":
    for _sig in (signal.SIGTERM, signal.SIGHUP):
        try:
            signal.signal(_sig, _bye)
        except Exception:
            pass
    _presence.enter()
    try:
        curses.wrapper(main)
    except KeyboardInterrupt:
        pass
    finally:
        shutdown()
