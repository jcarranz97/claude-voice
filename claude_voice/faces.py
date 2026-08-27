#!/usr/bin/env python3
"""The figure in the middle of the HUD, and where it comes from.

  faces.py                  what is installed, and which one is in use
  faces.py <name>           watch it animate, without opening a HUD
  faces.py <name> --dump    print its frames as text

The reactor is a taste, and someone who leaves a HUD open all day should not
have to patch hud.py to have their own. A face is a directory of plain text
frames:

    ~/.config/claude-voice/faces/cat/
      face.toml
      idle/       frame-01.txt  frame-02.txt
      thinking/   frame-01.txt … frame-06.txt
      speaking/   …
      listening/  …

and the config picks one:

    [hud]
    face = "cat"

Text files, because the point is that anyone can make one in a text editor.
Frames rather than one picture per state, because the reactor is not a picture
-- it is a behaviour, and the behaviour carries the meaning. Thinking spins,
speaking pushes energy out, listening draws it in, idle breathes. A face that
holds still in four different colours is not a face, it is a colour scheme, so
the format has motion in it from the start: a directory of frames per state,
played in order at the rate the face declares.

The reactor stays the default and stays parametric -- it is drawn from radii,
not from files, which is why it fits any window. Everything else declares the
smallest window it can be drawn in, and below that the HUD falls back to the
reactor: a face clipped in half is worse than a face that is not this one.

What a face does NOT get to do is replace the VU bars. They are a meter, not
decoration -- they thrash while speaking and lie nearly flat otherwise -- and
a face supplying its own would be supplying a second, disagreeing answer to
the same question. `bars = false` suppresses them, and that is the whole knob.

Frames are read and measured once, at load. The HUD redraws twenty times a
second and must never touch the disk to do it.
"""

import curses
import math
import sys
import unicodedata
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import config as _config                              # noqa: E402

BASE = _config.BASE
# Bundled faces ship inside the package; yours live in the config directory
# and shadow a bundled face of the same name. Same rule as the language packs:
# what ships is an example, and reinstalling must not take your own with it.
BUNDLED_FACES = HERE / "faces"
USER_FACES = BASE / "faces"

DEFAULT = "reactor"

# Every state the HUD can ask a face to draw. `agents` is thinking with an
# owner: it spins the same way, so a face that does not draw it separately
# gets thinking rather than idle.
STATES = ("thinking", "speaking", "listening", "idle", "ready",
          "agents", "stranded")
# Missing state -> what to try instead, in order. Everything ends at idle,
# so a two-frame face is a valid face.
FALLBACK = {
    "thinking":  ("idle",),
    "speaking":  ("idle",),
    "listening": ("idle",),
    "ready":     ("idle",),
    "agents":    ("thinking", "idle"),
    "stranded":  ("idle",),
    "idle":      (),
}

# Names only, mapped to the eight terminal colours, so a face stays portable
# across themes. No hex: a face that names #1b2b34 is a face that only works
# on the terminal it was written on.
COLORS = ("cyan", "yellow", "green", "white", "magenta", "blue", "red", "black")
ALIASES = {"amber": "yellow", "orange": "yellow", "grey": "white",
           "gray": "white", "purple": "magenta"}

# What the reactor has always used, and therefore what a face inherits for any
# state it does not colour itself.
DEFAULT_COLORS = {
    "thinking": "cyan", "speaking": "yellow", "listening": "magenta",
    "idle": "white", "ready": "green", "agents": "blue", "stranded": "yellow",
}

MAX_FRAME_BYTES = 256 * 1024      # a frame is a picture, not a payload


# --- measuring -----------------------------------------------------------
# Box drawing and emoji are not all one column wide, and a face using
# double-width characters would sit half a cell off centre for the whole
# session if this were len().

def _cells(ch: str) -> int:
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def dwidth(s: str) -> int:
    """How many columns a string occupies on screen."""
    return sum(_cells(c) for c in s)


def dclip(s: str, cols: int) -> str:
    """The longest prefix of s that fits in `cols` columns.

    Never splits a double-width character across the edge: half a glyph is
    what a terminal renders as garbage.
    """
    if cols <= 0:
        return ""
    out, used = [], 0
    for ch in s:
        w = _cells(ch)
        if used + w > cols:
            break
        out.append(ch)
        used += w
    return "".join(out)


# --- the faces themselves ------------------------------------------------

class Reactor:
    """The default: concentric rings, drawn from radii rather than read.

    Parametric on purpose. It is the fallback for every window too small for
    a fixed-art face, so it is the one face that must fit anything.
    """

    name = DEFAULT
    origin = "built in"
    bars = True
    min_width, min_height = 0, 0
    fps = 0.0

    # Radii in character cells; terminals have cells about 2x taller than
    # wide, so X is stretched when drawing.
    RINGS = [(3.0, "·"), (5.0, "○"), (7.0, "◦")]

    def rows(self) -> int:
        return 19             # rings (15), the bars under them, the target line

    def fits(self, band) -> bool:
        return True           # drawn from radii, so it fits whatever is there

    def color(self, state: str) -> str:
        return DEFAULT_COLORS.get(state, "white")

    def states(self) -> dict:
        return {s: 0 for s in ("thinking", "speaking", "listening", "idle")}

    def draw(self, win, cy, cx, t, state, color, band) -> tuple:
        """Rings that breathe, spin or pulse. Returns (label row, bars row).

        Clipped to the band (top, bottom, x0, width) so a ring never bleeds
        into the history panel, whichever edge it is on.
        """
        h, w = win.getmaxyx()
        y0, y1, x0, x1 = (0, h - 1, 0, w) if band is None else (
            band[0], band[1] + 1, band[2], min(band[2] + band[3], w))
        for radius, glyph in self.RINGS:
            if state in ("thinking", "agents"):
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
                          curses.A_BOLD if bright > 0.55 else curses.A_DIM,
                          y0, y1, x0, x1)
            elif state == "listening":
                # Wave travelling INWARD: speaking sends energy out, listening
                # draws it in. Same shape inverted, and legible at a glance.
                phase = 1.0 - ((t * 2.6) % 1.0)
                near = abs((radius / 7.0) - phase) < 0.30
                steps = int(radius * 9)
                for i in range(steps):
                    a = 2 * math.pi * i / steps
                    _plot(win, cy, cx, radius, a, glyph, color,
                          curses.A_BOLD if near else curses.A_DIM,
                          y0, y1, x0, x1)
            elif state == "speaking":
                # radial pulse outward, like a voice wave
                phase = (t * 3.4) % 1.0
                near = abs((radius / 7.0) - phase) < 0.28
                steps = int(radius * 9)
                for i in range(steps):
                    a = 2 * math.pi * i / steps
                    _plot(win, cy, cx, radius, a, glyph, color,
                          curses.A_BOLD if near else curses.A_DIM,
                          y0, y1, x0, x1)
            else:
                # slow breathing
                breath = (math.sin(t * 0.9) + 1) / 2
                steps = int(radius * 7)
                for i in range(steps):
                    a = 2 * math.pi * i / steps
                    _plot(win, cy, cx, radius, a, glyph, color,
                          curses.A_BOLD if breath > 0.75 else curses.A_DIM,
                          y0, y1, x0, x1)
        return cy, cy + 9


def _plot(win, cy, cx, r, angle, glyph, color, attr, y0, y1, x0, x1):
    y = int(round(cy + math.sin(angle) * r))
    x = int(round(cx + math.cos(angle) * r * 2))   # x2: cells are not square
    if y0 <= y < y1 and x0 <= x < x1 - 1:
        try:
            win.addstr(y, x, glyph, curses.color_pair(color) | attr)
        except curses.error:
            pass


class ArtFace:
    """A directory of text frames, parsed once and played back on a clock."""

    def __init__(self, name: str, path: Path, meta: dict, origin: str):
        self.name = str(meta.get("name", name) or name)
        self.dir = path
        self.origin = origin
        self.fps = max(0.5, min(30.0, float(meta.get("fps", 8) or 8)))
        self.bars = bool(meta.get("bars", True))
        self._frames = {}          # state -> [[str, ...], ...]
        self.width = 0
        self.height = 0
        for state in STATES:
            frames = _read_frames(path / state)
            if frames:
                self._frames[state] = frames
                for art in frames:
                    self.height = max(self.height, len(art))
                    self.width = max(self.width, max(dwidth(r) for r in art))

        colors = dict(DEFAULT_COLORS)
        for state, want in (meta.get("colors") or {}).items():
            want = ALIASES.get(str(want).strip().lower(), str(want).strip().lower())
            if want in COLORS:
                colors[state] = want
        self._colors = colors

        # The row of the art the state label is drawn over. The reactor's
        # middle is empty by construction; a cat's is a nose, so a face with
        # something in the middle has to say which row is safe.
        # Allowed past the last row on purpose: a face whose middle is a
        # nose can put the label under itself, which is the honest answer for
        # most art. The reactor's middle is empty by construction and is the
        # only shape where drawing the label through the figure works.
        row = meta.get("label_row", None)
        self.label_row = (max(0, min(self.height + 3, int(row)))
                          if row is not None and self.height else
                          max(0, self.height // 2))
        # A face knows how small it can be drawn better than the HUD does; the
        # declared minimum wins over the measured one only when it is larger,
        # because art clipped at its own declared size is still clipped.
        self.min_width = max(self.width, int(meta.get("min_width", 0) or 0))
        self.min_height = max(self.height, int(meta.get("min_height", 0) or 0))

    def ok(self) -> bool:
        """A face with no idle frames has no fallback and cannot be drawn."""
        return bool(self._frames.get("idle"))

    def states(self) -> dict:
        """state -> how many frames it has of its own. For `faces` to print."""
        return {s: len(self._frames[s]) for s in STATES if s in self._frames}

    def rows(self) -> int:
        """Rows this face needs of the band: the art, the state word under it,
        the meter, and the footer the HUD keeps for the target line.

        One number, used both to decide whether the face fits and to size the
        bottom history strip. Two answers to that question is how art ends up
        drawn over the line naming the session."""
        return max(self.min_height, self.height, self.label_row + 1) + 3

    def color(self, state: str) -> str:
        return self._colors.get(state, "white")

    def frames(self, state: str):
        art = self._frames.get(state)
        if art:
            return art
        for alt in FALLBACK.get(state, ("idle",)):
            art = self._frames.get(alt)
            if art:
                return art
        return self._frames.get("idle") or [[""]]

    def frame(self, state: str, t: float):
        frames = self.frames(state)
        return frames[int(t * self.fps) % len(frames)]

    def fits(self, band) -> bool:
        return band[3] >= self.min_width and (band[1] - band[0] + 1) >= self.rows()

    def draw(self, win, cy, cx, t, state, color, band) -> tuple:
        """Paint the current frame. Returns (label row, bars row).

        Placed so the face's own label_row lands on cy, which is the row the
        HUD centres everything else on -- the label stays put when you switch
        faces, and a face whose middle is a nose can move the label off it.
        """
        h, w = win.getmaxyx()
        top, bottom, x0, x1 = (0, h - 1, 0, w) if band is None else (
            band[0], band[1], band[2], min(band[2] + band[3], w))
        art = self.frame(state, t)
        y = cy - self.label_row
        # Nudge, do not clip: sliding a frame that would hang off an edge back
        # into the band costs a row of centring and saves the whole picture.
        y = max(top, min(y, bottom - len(art) + 1))
        # The BLOCK is centred, once, on the face's own width -- never each
        # row on its own. Rows are different lengths by nature, and centring
        # them individually turns a drawing into a ragged column that
        # reshuffles itself on every frame.
        x = x0 + max(0, ((x1 - x0) - self.width) // 2)
        attr = curses.color_pair(color) | (
            curses.A_BOLD if state in ("speaking", "listening") else curses.A_NORMAL)
        for i, row in enumerate(art):
            if not (top <= y + i <= bottom) or not row.strip():
                continue
            text = dclip(row, max(0, x1 - x - 1))
            try:
                win.addstr(y + i, x, text, attr)
            except curses.error:
                pass
        label_y = min(bottom, y + self.label_row)
        return label_y, max(y + len(art), label_y + 1) + 1


def _read_frames(d: Path) -> list:
    """Every frame-*.txt in a state directory, in name order, parsed once.

    Sorted by filename, which is why the convention is zero-padded: frame-10
    before frame-2 is an animation that stutters for a reason nobody would
    guess from looking at the art.
    """
    try:
        files = sorted(p for p in d.iterdir()
                       if p.is_file() and p.suffix == ".txt")
    except OSError:
        return []
    out = []
    for p in files:
        try:
            if p.stat().st_size > MAX_FRAME_BYTES:
                continue
            text = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        lines = [ln.rstrip("\n").rstrip() for ln in text.split("\n")]
        while lines and not lines[-1].strip():
            lines.pop()
        while lines and not lines[0].strip():
            lines.pop(0)
        if lines:
            out.append(lines)
    return out


# --- finding them --------------------------------------------------------

def face_dir(name: str) -> Path:
    """Where a face lives. Yours shadows the one that ships."""
    mine = USER_FACES / name
    return mine if mine.is_dir() else BUNDLED_FACES / name


def options() -> list:
    """Every face on disk, in a stable order, the reactor first.

    The reactor leads rather than sorting into place because it is the default
    and the fallback: a list that buries it under `cat` reads as if it were
    just another option, and it is the one you get back to.
    """
    names = set()
    for d in (BUNDLED_FACES, USER_FACES):
        try:
            names.update(p.name for p in d.iterdir()
                         if p.is_dir() and not p.name.startswith("."))
        except OSError:
            pass
    names.discard(DEFAULT)
    return [DEFAULT] + sorted(names)


def origin_of(name: str) -> str:
    if name == DEFAULT:
        return "built in"
    if (USER_FACES / name).is_dir():
        return "yours"
    return "bundled"


def load(name: str = "") -> object:
    """The face in effect, or the one asked for. Never raises.

    A face that is missing, unreadable or empty falls back to the reactor and
    says so on stderr, for the same reason a broken config file does: the HUD
    losing its figure is a cosmetic problem, and it taking the voice down with
    it would not be.
    """
    if not name:
        name = str(_config.load().get("hud.face", DEFAULT) or DEFAULT).strip()
    if name == DEFAULT:
        return Reactor()
    d = face_dir(name)
    if not d.is_dir():
        print(f"claude-voice: no face called {name}; using the reactor",
              file=sys.stderr)
        return Reactor()
    # config's reader, not tomllib's, for its one useful behaviour: a typo in
    # a face.toml is loud on stderr and never fatal.
    meta = _config._read(d / "face.toml")
    face = ArtFace(name, d, meta, origin_of(name))
    if not face.ok():
        print(f"claude-voice: face {name} has no idle frames; using the reactor",
              file=sys.stderr)
        return Reactor()
    return face


# --- `claude-voice faces` ------------------------------------------------

def show() -> None:
    active = str(_config.load().get("hud.face", DEFAULT) or DEFAULT).strip()
    for name in options():
        face = load(name)
        mark = "→" if name == active else " "
        if isinstance(face, Reactor):
            note = "any size · scales to the window"
        else:
            counts = face.states()
            note = (f"{sum(counts.values())} frames / {len(counts)} states · "
                    f"{face.min_width}x{face.min_height} min · {face.fps:g} fps"
                    + ("" if face.bars else " · no bars"))
        print(f"  {mark} {name:<12} {origin_of(name):<8}  {note}")
    print("\n  claude-voice faces <name>   watch it, without opening a HUD")
    print("  Set it with  [hud] face = \"<name>\"  in the config file.")
    print(f"  Your own faces go in {USER_FACES}/<name>/")


def dump(name: str) -> int:
    face = load(name)
    if isinstance(face, Reactor):
        print("  the reactor is drawn, not stored: there are no frames to print")
        return 0
    for state, n in face.states().items():
        for i, art in enumerate(face.frames(state), start=1):
            print(f"\n── {state} {i}/{n} " + "─" * 20)
            print("\n".join(art))
    return 0


def preview(name: str) -> int:
    """Watch a face cycle its states. The point is to see it move: a still of
    a frame says nothing about whether the animation reads."""
    face = load(name)
    cycle = ["idle", "thinking", "speaking", "listening", "agents"]

    def run(stdscr):
        import time
        curses.curs_set(0)
        stdscr.nodelay(True)
        curses.use_default_colors()
        for i, fg in enumerate(_curses_colors(), start=1):
            curses.init_pair(i, fg, -1)
        t0 = time.time()
        while True:
            ch = stdscr.getch()
            if ch in (ord("q"), 27):
                break
            if ch == ord(" "):
                t0 -= 3.0
            t = time.time() - t0
            state = cycle[int(t / 3.0) % len(cycle)]
            h, w = stdscr.getmaxyx()
            stdscr.erase()
            band = (3, h - 2, 0, w)
            pair = 1 + COLORS.index(face.color(state))
            fits = face.fits(band)
            head = f"  {face.name} — {state}" + ("" if fits else "   (window too small: the reactor stands in)")
            try:
                stdscr.addstr(0, 0, head[:w - 1], curses.A_BOLD)
                stdscr.addstr(1, 0, "  space: next state   ·   q: quit"[:w - 1],
                              curses.A_DIM)
            except curses.error:
                pass
            drawer = face if fits else Reactor()
            cy = (band[0] + band[1]) // 2
            label_y, _ = drawer.draw(stdscr, cy, w // 2, t, state, pair, band)
            label = state.upper()
            try:
                stdscr.addstr(label_y, max(0, w // 2 - len(label) // 2), label,
                              curses.color_pair(pair) | curses.A_BOLD)
            except curses.error:
                pass
            stdscr.refresh()
            time.sleep(0.05)

    curses.wrapper(run)
    return 0


def _curses_colors():
    return (curses.COLOR_CYAN, curses.COLOR_YELLOW, curses.COLOR_GREEN,
            curses.COLOR_WHITE, curses.COLOR_MAGENTA, curses.COLOR_BLUE,
            curses.COLOR_RED, curses.COLOR_BLACK)


def main() -> int:
    args = [a for a in sys.argv[1:]]
    if args and args[0] in ("-h", "--help", "help"):
        print(__doc__.strip())
        return 0
    if not args:
        show()
        return 0
    name = args[0]
    if name not in options():
        print(f"  no face called {name}")
        show()
        return 1
    if "--dump" in args:
        return dump(name)
    return preview(name)


if __name__ == "__main__":
    sys.exit(main())
