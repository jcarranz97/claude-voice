#!/usr/bin/env python3
"""The HUD -- an arc-reactor style status window, drawn in a terminal.

    claude-voice hud --terminal

This is the surface for a machine with no desktop, an ssh session, or a spare
pane you already have open. `claude-voice hud` with no flag opens the other
one: a frameless window (hudweb.py) drawing the same state with curves instead
of ring glyphs, which is the better window wherever there is a screen to put
it on.

Both read hudcore.py, and every key below runs the shared action there, so the
two cannot disagree about what is on screen or about what a key did.

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
import math
import signal
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import presence as _presence                          # noqa: E402
# What the HUD knows lives in hudcore, so that this window and the web one
# cannot disagree about it. What is left in here is drawing.
import hudcore as core                                # noqa: E402
from hudcore import (L, agents_live, conversation_alive,   # noqa: E402,F401
                     daemon_alive, dictate_blocked, dictate_target,
                     focus_here, focus_state, history_rows, listen_stranded,
                     mic_held, mic_open, mic_speaking, panel_open, position,
                     read_state, set_panel_open, sweep_orphans)

BASE = core.BASE
ENABLED = core.ENABLED

FPS = 20.0
IDLE_AFTER = core.IDLE_AFTER

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
            core.act("sweep")
        # The rest of the keys are the shared actions, so that a key here and
        # a click in the web HUD run the same code rather than two copies of
        # it that drift. A refusal is shown, never swallowed: the answer to
        # "I pressed d and nothing happened" has to be on screen.
        action = {ord("c"): "conversation", ord("l"): "language",
                  ord("t"): "session", ord("f"): "focus",
                  ord("d"): "dictate", ord("m"): "voice",
                  ord(" "): "voice"}.get(ch)
        if action:
            ok, msg = core.act(action)
            if not ok:
                refused_at, refused_why = time.time(), msg
            elif msg:
                notice_at, notice = time.time(), f"· {msg} ·"

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

        centered(stdscr, 1, core.TITLE, w, curses.color_pair(color) | curses.A_BOLD)

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
        other, other_label = core.next_language()

        def legend(voice_label: str, focus_label: str, sep: str) -> str:
            # The two silencing keys sit together, in scope order: m takes the
            # machine, f takes everything except the session t points at.
            # Apart, f read as a view control and got pressed as one.
            row = [f"m: {voice_label}", f"f: {focus_label}",
                   "d: dictate", "c: conversation", "t: session"]
            if other and other != core.CFG.preset:
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
            core.conversation_stop()
    except Exception:
        pass
    try:
        core.run("voice.py", "silence")     # waited on: we are on the way out
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
