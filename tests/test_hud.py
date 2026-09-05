"""The terminal HUD: what it puts on screen, and what a key does to it.

Every line of this module ends up inside `curses`, which means a real terminal,
a real `initscr` and a real screen -- none of which a test session has. So the
screen is a double that records what was drawn, and `curses` itself is stubbed
down to the handful of screen-level calls the module makes. What is left is the
part worth testing: the strings.

The event loop is driven by a scripted list of keystrokes. `FakeScreen.getch`
answers `q` once the script runs out, so every loop in here terminates on its
own input rather than on a clock.
"""

import curses

import pytest

import claude_voice.hud as hud

# --- the doubles ---------------------------------------------------------


class FakeScreen:
    """A curses window that remembers what was written to it.

    `writes` is every `addstr` since the window opened; `frames` splits them at
    each `erase`, so a test that runs several passes of the loop can look at
    one frame instead of all of them.
    """

    def __init__(self, keys=(), h=40, w=120, failing=False):
        self.keys = list(keys)
        self.h, self.w = h, w
        self.failing = failing  # every addstr raises, as a full window does
        self.writes = []
        self.frames = [[]]
        self.refreshes = 0
        self.erases = 0
        self.cursor = (0, 0)
        self.attrs = []
        self.nodelay_arg = None
        self.keypad_arg = None
        self.clears = 0
        self._getch_calls = 0

    # -- what hud.py actually calls

    def addstr(self, y, x, text, attr=0):
        if self.failing:
            raise curses.error("addstr() returned ERR")
        self.writes.append((y, x, text, attr))
        self.frames[-1].append((y, x, text, attr))

    def addnstr(self, y, x, text, n, attr=0):
        self.addstr(y, x, text[:n], attr)

    def getmaxyx(self):
        return self.h, self.w

    def erase(self):
        self.erases += 1
        self.frames.append([])

    def clear(self):
        self.clears += 1
        self.erase()

    def refresh(self):
        self.refreshes += 1

    def getch(self):
        self._getch_calls += 1
        # A runaway loop is a hung suite, so the double refuses to be endless.
        assert self._getch_calls < 500, "the HUD loop never quit"
        return self.keys.pop(0) if self.keys else ord("q")

    def nodelay(self, flag):
        self.nodelay_arg = flag

    def keypad(self, flag):
        self.keypad_arg = flag

    def move(self, y, x):
        self.cursor = (y, x)

    def attron(self, attr):
        self.attrs.append(("on", attr))

    def attroff(self, attr):
        self.attrs.append(("off", attr))

    # -- what a test asks it

    def row(self, y, frame=None):
        """Everything written to row `y`, in the order it was written."""
        src = self.writes if frame is None else self.frames[frame]
        return "".join(t for (yy, _x, t, _a) in src if yy == y)

    @property
    def text(self):
        return "\n".join(t for (_y, _x, t, _a) in self.writes)

    def frame(self, i):
        """The text of one frame, for a loop that drew more than one."""
        return "\n".join(t for (_y, _x, t, _a) in self.frames[i])

    def glyphs(self):
        return {t for (_y, _x, t, _a) in self.writes}


class Env:
    """Every question `hud.main` asks the rest of the package, as attributes.

    The HUD reads a dozen independent facts per frame -- the microphone, the
    focus, the agents, the state file -- and a test is usually about one of
    them. Setting the others once, here, is what keeps each test to its point.
    """

    def __init__(self):
        self.state = {"state": "idle", "text": "", "ts": 0}
        self.level = 0.5
        self.agents = []
        self.speaking = False
        self.open = False
        self.held = []
        self.daemon = False
        self.stranded = ""
        self.focus = ("", "")
        self.here = True
        self.target = ""
        self.blocked = ""
        self.panel = False
        self.position = "left"
        self.rows = []
        self.panels = {"session": True, "agents": True}
        self.plugin_panels = []
        self.language = ("", "")
        self.acted = []  # every action name the key handler ran
        self.results = {}  # action name -> (ok, message)
        self.saved = []  # every set_panel_open() argument


@pytest.fixture
def curses_stub(monkeypatch):
    """The screen-level curses calls, neutered.

    `color_pair` is the one with a return value the module uses, and it raises
    without an `initscr` behind it. Everything else here only ever touched the
    terminal.
    """
    monkeypatch.setattr(curses, "color_pair", lambda n: n << 8)
    monkeypatch.setattr(curses, "curs_set", lambda n: 0)
    monkeypatch.setattr(curses, "use_default_colors", lambda: None)
    monkeypatch.setattr(curses, "start_color", lambda: None)
    monkeypatch.setattr(curses, "init_pair", lambda *a: None)
    monkeypatch.setattr(curses, "initscr", lambda: FakeScreen())
    monkeypatch.setattr(curses, "wrapper", lambda fn, *a: fn(FakeScreen(), *a))
    return curses


@pytest.fixture
def env(monkeypatch, curses_stub, home):
    """A HUD wired to an `Env` instead of to the machine."""
    e = Env()

    def act(name):
        e.acted.append(name)
        return e.results.get(name, (True, ""))

    def set_panel_open(on):
        e.saved.append(on)
        e.panel = on

    monkeypatch.setattr(hud, "read_state", lambda: e.state)
    monkeypatch.setattr(hud, "agents_live", lambda: e.agents)
    monkeypatch.setattr(hud, "mic_speaking", lambda: e.speaking)
    monkeypatch.setattr(hud, "mic_open", lambda: e.open)
    monkeypatch.setattr(hud, "mic_held", lambda: e.held)
    monkeypatch.setattr(hud, "daemon_alive", lambda: e.daemon)
    monkeypatch.setattr(hud, "listen_stranded", lambda: e.stranded)
    monkeypatch.setattr(hud, "focus_state", lambda: e.focus)
    monkeypatch.setattr(hud, "focus_here", lambda: e.here)
    monkeypatch.setattr(hud, "dictate_target", lambda: e.target)
    monkeypatch.setattr(hud, "dictate_blocked", lambda: e.blocked)
    monkeypatch.setattr(hud, "panel_open", lambda: e.panel)
    monkeypatch.setattr(hud, "set_panel_open", set_panel_open)
    monkeypatch.setattr(hud, "position", lambda: e.position)
    monkeypatch.setattr(hud, "history_rows", lambda w: e.rows)
    monkeypatch.setattr(hud.core, "act", act)
    monkeypatch.setattr(hud.core, "level_now", lambda d=None: e.level)
    monkeypatch.setattr(hud.core, "panels", lambda: e.panels)
    monkeypatch.setattr(hud.core, "plugin_panels", lambda window: e.plugin_panels)
    monkeypatch.setattr(hud.core, "next_language", lambda: e.language)
    monkeypatch.setattr(hud.core, "TITLE", "C L A U D E")
    # A frame costs a twentieth of a second on a real HUD and nothing here.
    monkeypatch.setattr(hud.time, "sleep", lambda _s: None)
    return e


@pytest.fixture
def run(env):
    """Drive `hud.main` through a scripted list of keys and hand back the screen."""

    def _run(keys=(), h=40, w=120, failing=False, voice_on=True):
        if voice_on:
            hud.ENABLED.parent.mkdir(parents=True, exist_ok=True)
            hud.ENABLED.touch()
        else:
            hud.ENABLED.unlink(missing_ok=True)
        screen = FakeScreen(keys=keys, h=h, w=w, failing=failing)
        hud.main(screen)
        return screen

    return _run


# --- the pure geometry ---------------------------------------------------


class TestLayout:
    """`layout` decides whether the panel shares the window or takes it."""

    def test_without_history_the_reactor_owns_the_body(self):
        panel, reactor, divider = hud.layout(40, 120, False)
        assert (panel, divider) == (None, None)
        assert reactor == (hud.BODY_TOP, 39, 0, 120)

    def test_a_left_panel_splits_the_window_vertically(self, monkeypatch):
        monkeypatch.setattr(hud, "position", lambda: "left")
        panel, reactor, divider = hud.layout(40, 120, True)
        assert panel == (hud.BODY_TOP, 39, 0, 48)
        assert reactor[2] == 49
        assert divider == ("v", 48)

    def test_a_right_panel_sits_against_the_right_edge(self, monkeypatch):
        monkeypatch.setattr(hud, "position", lambda: "right")
        panel, reactor, divider = hud.layout(40, 120, True)
        assert panel[2] + panel[3] == 120
        assert reactor[2] == 0
        assert divider == ("v", 120 - panel[3] - 1)

    def test_the_panel_width_is_clamped_at_both_ends(self, monkeypatch):
        monkeypatch.setattr(hud, "position", lambda: "left")
        assert hud.layout(40, 80, True)[0][3] == hud.PANEL_MIN_W
        assert hud.layout(40, 400, True)[0][3] == hud.PANEL_MAX_W

    def test_a_bottom_panel_is_a_strip_under_the_reactor(self, monkeypatch):
        monkeypatch.setattr(hud, "position", lambda: "bottom")
        panel, reactor, divider = hud.layout(40, 120, True)
        assert divider[0] == "h"
        assert panel[0] == divider[1] + 1
        assert reactor[1] == divider[1] - 1

    def test_a_short_window_gives_the_panel_the_whole_screen(self, monkeypatch):
        # Not enough rows for the reactor and a worthwhile strip both.
        monkeypatch.setattr(hud, "position", lambda: "bottom")
        assert hud.layout(28, 120, True) == ((0, 27, 0, 120), None, None)

    def test_a_narrow_window_gives_the_panel_the_whole_screen(self, monkeypatch):
        monkeypatch.setattr(hud, "position", lambda: "left")
        assert hud.layout(40, 60, True) == ((0, 39, 0, 60), None, None)


class TestCentered:
    """`centered` works in a band, not on the screen."""

    def test_text_is_centred_inside_the_band(self, curses_stub):
        win = FakeScreen()
        hud.centered(win, 2, "abc", 20, 0, 10)
        assert win.writes == [(2, 10 + 8, "abc", 0)]

    def test_text_wider_than_the_band_is_truncated(self, curses_stub):
        win = FakeScreen()
        hud.centered(win, 0, "abcdefghij", 5)
        assert win.writes[0][2] == "abcd"

    def test_a_refused_write_is_swallowed(self, curses_stub):
        # The bottom right cell always fails; a HUD must not die of it.
        hud.centered(FakeScreen(failing=True), 0, "abc", 20)


# --- the history panel ---------------------------------------------------

ROWS = [(f"line {i}", "out" if i % 2 else "in", False) for i in range(12)]
# Long enough that a full-height panel cannot show it all, which is what makes
# the scroll keys mean anything.
LONG_ROWS = [(f"line {i}", "out", False) for i in range(60)]


class TestDrawHistory:
    """The spoken log: bottom-aligned, clamped, and marked at both ends."""

    def test_an_empty_log_says_so(self, curses_stub, env):
        win = FakeScreen()
        assert hud.draw_history(win, 5, 15, 0, 40, 0, hud.AMBER, hud.MAGENTA) == 0
        assert "nothing spoken yet" in win.text

    def test_the_newest_line_anchors_the_bottom(self, curses_stub, env):
        env.rows = ROWS
        win = FakeScreen()
        hud.draw_history(win, 10, 15, 0, 40, 0, hud.AMBER, hud.MAGENTA)
        assert win.row(15) == "line 11"

    def test_scrolling_up_marks_what_is_above_and_below(self, curses_stub, env):
        env.rows = ROWS
        win = FakeScreen()
        assert hud.draw_history(win, 10, 15, 0, 40, 3, hud.AMBER, hud.MAGENTA) == 3
        assert "older" in win.row(9)
        assert "↓ 3 newer" in win.row(16)

    def test_scroll_past_the_oldest_line_is_clamped(self, curses_stub, env):
        env.rows = ROWS
        win = FakeScreen()
        # `g` asks for a million; the answer is however many lines there are.
        assert hud.draw_history(win, 10, 15, 0, 40, 10**6, hud.AMBER, hud.MAGENTA) == 6

    def test_a_page_never_opens_on_a_continuation_line(self, curses_stub, env):
        # The head of a wrapped sentence is scrolled off, so its tail is
        # dropped rather than shown as if somebody had said it.
        env.rows = [
            ("head", "out", False),
            ("tail", "out", True),
            ("next", "out", False),
            ("last", "out", False),
        ]
        win = FakeScreen()
        hud.draw_history(win, 10, 11, 0, 40, 1, hud.AMBER, hud.MAGENTA)
        assert "tail" not in win.text

    def test_a_refused_write_is_swallowed(self, curses_stub, env):
        env.rows = ROWS
        hud.draw_history(FakeScreen(failing=True), 10, 15, 0, 40, 0, hud.AMBER, hud.MAGENTA)


class TestDrawPanel:
    """The panel's own furniture: a title, and a hint when there is room."""

    def test_a_tall_panel_shows_the_scroll_hint(self, curses_stub, env):
        win = FakeScreen()
        hud.draw_panel(win, (4, 30, 0, 40), False, 0)
        assert "H I S T O R Y" in win.row(5)
        assert "g/G: ends" in win.row(6)

    def test_a_bottom_strip_spends_no_row_on_the_hint(self, curses_stub, env):
        win = FakeScreen()
        hud.draw_panel(win, (30, 39, 0, 40), True, 0)
        assert "H I S T O R Y" in win.row(30)
        assert "g/G: ends" not in win.text


class TestDrawDivider:
    """One rule between the panel and the reactor, either way round."""

    def test_a_vertical_divider_runs_the_height_of_the_body(self, curses_stub):
        win = FakeScreen()
        hud.draw_divider(win, ("v", 48), 20, 120)
        assert [y for (y, _x, _t, _a) in win.writes] == list(range(hud.BODY_TOP, 19))
        assert {x for (_y, x, _t, _a) in win.writes} == {48}

    def test_a_horizontal_divider_spans_the_window(self, curses_stub):
        win = FakeScreen()
        hud.draw_divider(win, ("h", 26), 40, 120)
        assert win.writes[0][:3] == (26, 1, "─" * 118)

    def test_refused_writes_are_swallowed(self, curses_stub):
        hud.draw_divider(FakeScreen(failing=True), ("v", 48), 20, 120)
        hud.draw_divider(FakeScreen(failing=True), ("h", 26), 40, 120)


# --- the reactor ---------------------------------------------------------


class TestDrawReactor:
    """Three rings, drawn differently for each thing the voice is doing."""

    @pytest.mark.parametrize("state", ["thinking", "speaking", "listening", "idle"])
    def test_every_state_draws_all_three_rings(self, curses_stub, state):
        win = FakeScreen(h=40, w=120)
        hud.draw_reactor(win, 20, 60, 1.0, state, hud.CYAN, level=1.0)
        assert {"·", "○", "◦"} <= win.glyphs()

    def test_a_band_clips_the_rings_to_its_own_columns(self, curses_stub):
        win = FakeScreen(h=40, w=120)
        band = (10, 30, 60, 40)
        hud.draw_reactor(win, 20, 78, 0.0, "idle", hud.CYAN, band)
        assert all(60 <= x < 99 for (_y, x, _t, _a) in win.writes)
        assert all(10 <= y < 31 for (y, _x, _t, _a) in win.writes)

    def test_a_ring_off_the_edge_draws_nothing(self, curses_stub):
        win = FakeScreen(h=40, w=120)
        hud.draw_reactor(win, -50, -50, 0.0, "idle", hud.CYAN)
        assert win.writes == []

    def test_a_refused_write_is_swallowed(self, curses_stub):
        hud.draw_reactor(FakeScreen(failing=True), 20, 60, 0.0, "idle", hud.CYAN)

    def test_a_quiet_voice_lights_fewer_rings_than_a_loud_one(self, curses_stub):
        # Loudness is spent on reach: silence lights the inner ring only.
        def bold(level):
            win = FakeScreen(h=40, w=120)
            hud.draw_reactor(win, 20, 60, 0.0, "speaking", hud.AMBER, level=level)
            return sum(1 for (_y, _x, _t, a) in win.writes if a & curses.A_BOLD)

        assert bold(0.0) <= bold(1.0)


class TestDrawBars:
    """The meter under the reactor."""

    @pytest.mark.parametrize("state", ["speaking", "listening", "thinking", "idle"])
    def test_every_state_draws_twenty_one_bars(self, curses_stub, state):
        win = FakeScreen()
        hud.draw_bars(win, 30, 60, 1.0, state, hud.CYAN, 120, 0, 0.7)
        assert len(win.writes[0][2]) == 21
        assert set(win.writes[0][2]) <= set("▁▂▃▄▅▆▇█")

    def test_a_meter_that_would_not_fit_is_not_drawn(self, curses_stub):
        win = FakeScreen()
        hud.draw_bars(win, 30, 10, 1.0, "idle", hud.CYAN, 15)
        assert win.writes == []

    def test_a_refused_write_is_swallowed(self, curses_stub):
        hud.draw_bars(FakeScreen(failing=True), 30, 60, 1.0, "idle", hud.CYAN, 120)


def a_row(label, value, **kw):
    """One row in the shape the registry hands over, defaults and all."""
    row = {
        "label": label,
        "short": label,
        "value": value,
        "meter": None,
        "state": "",
        "detail": "",
        "action": "",
        "key": "",
    }
    row.update(kw)
    return row


def a_panel(*rows, plugin="github", order=20):
    return {
        "plugin": plugin,
        "title": plugin,
        "mark": "",
        "rows": list(rows),
        "tiles": [],
        "note": "",
        "slot": "right",
        "order": order,
        "action": None,
    }


class TestDrawPanels:
    """Every plugin panel on the one row a terminal can spare."""

    def test_nothing_at_all_is_drawn_without_panels(self, curses_stub):
        win = FakeScreen()
        hud.draw_panels(win, 0, 120, [])
        assert win.writes == []

    def test_the_label_is_dim_and_the_value_is_not(self, curses_stub):
        win = FakeScreen()
        hud.draw_panels(win, 0, 120, [a_panel(a_row("branch", "main"))])
        assert "branch main" in win.row(0)
        labels = [w for w in win.writes if w[2].strip() == "branch"]
        values = [w for w in win.writes if w[2] == "main"]
        assert labels[0][3] & curses.A_DIM
        assert not values[0][3] & curses.A_DIM

    def test_rows_are_separated_and_panels_are_ordered(self, curses_stub):
        win = FakeScreen()
        hud.draw_panels(
            win,
            0,
            160,
            [
                a_panel(a_row("pr", "#40"), plugin="github", order=20),
                a_panel(a_row("cpu", "38%"), plugin="system", order=10),
            ],
        )
        assert "cpu 38%  ·  pr #40" in win.row(0)

    def test_a_state_colours_the_value_and_a_warning_bolds_it(self, curses_stub):
        win = FakeScreen()
        hud.draw_panels(win, 0, 120, [a_panel(a_row("checks", "✗ 1 failing", state="warn"))])
        seg = next(w for w in win.writes if w[2] == "✗ 1 failing")
        assert seg[3] & curses.A_BOLD

    def test_the_detail_follows_the_value(self, curses_stub):
        win = FakeScreen()
        rows = [a_row("checks", "✗ 2 failing", state="warn", detail="lint, tests")]
        hud.draw_panels(win, 0, 200, [a_panel(*rows)])
        assert "✗ 2 failing — lint, tests" in win.row(0)

    def test_what_does_not_fit_is_cut_rather_than_wrapped(self, curses_stub):
        win = FakeScreen()
        hud.draw_panels(win, 0, 20, [a_panel(a_row("branch", "a-very-long-branch-name"))])
        assert len(win.row(0).rstrip()) < 20
        assert all(y == 0 for y, *_ in win.writes)

    def test_a_refused_write_is_swallowed(self, curses_stub):
        hud.draw_panels(FakeScreen(failing=True), 0, 120, [a_panel(a_row("branch", "main"))])


# --- the loop ------------------------------------------------------------


class TestBanner:
    """The word in the middle of the reactor, for each thing that can be true."""

    def test_an_idle_session_is_standing_by(self, run):
        assert "S T A N D I N G   B Y" in run([-1]).text

    def test_a_thinking_session_says_thinking(self, run, env):
        env.state = {"state": "thinking", "text": "", "ts": hud.time.time()}
        assert "T H I N K I N G" in run([-1]).text

    def test_a_speaking_session_says_speaking_and_quotes_the_line(self, run, env):
        env.state = {"state": "speaking", "text": "the tests pass", "ts": hud.time.time()}
        screen = run([-1])
        assert "S P E A K I N G" in screen.text
        assert "«the tests pass»" in screen.text

    def test_a_stale_state_falls_back_to_idle(self, run, env):
        # Fifteen minutes of "thinking" is a session that went to sleep.
        env.state = {"state": "thinking", "text": "", "ts": hud.time.time() - hud.IDLE_AFTER - 10}
        assert "S T A N D I N G   B Y" in run([-1]).text

    def test_a_ready_session_says_ready(self, run, env):
        env.state = {"state": "ready", "text": "", "ts": hud.time.time()}
        assert "R E A D Y" in run([-1]).text

    def test_you_talking_wins_over_whatever_i_am_doing(self, run, env):
        env.state = {"state": "speaking", "text": "", "ts": hud.time.time()}
        env.speaking = True
        assert "L I S T E N I N G" in run([-1]).text

    def test_nowhere_to_deliver_it_wins_over_listening(self, run, env):
        env.speaking = True
        env.daemon = True
        env.stranded = "no session"
        assert "N O T   L I S T E N I N G" in run([-1]).text

    def test_a_single_agent_is_named_without_a_count(self, run, env):
        env.agents = ["reading the config"]
        screen = run([-1])
        assert "A G E N T S" in screen.text
        assert "x1" not in screen.text

    def test_several_agents_carry_their_count(self, run, env):
        env.agents = ["one", "two", "three"]
        screen = run([-1])
        assert "A G E N T S   x3" in screen.text
        assert "· one" in screen.text

    def test_the_agent_list_can_be_switched_off_without_the_banner(self, run, env):
        env.agents = ["one", "two"]
        env.panels = dict(env.panels, agents=False)
        screen = run([-1])
        assert "A G E N T S" in screen.text
        assert "· one" not in screen.text

    def test_the_agent_list_stops_at_the_bottom_of_its_band(self, run, env):
        # A bottom strip leaves two rows under the reactor, not three.
        env.agents = ["one", "two", "three"]
        env.panel, env.position = True, "bottom"
        assert "· three" not in run([-1]).text

    def test_the_voice_being_off_replaces_a_calm_state(self, run):
        assert "V O I C E   O F F" in run([-1], voice_on=False).text

    def test_the_voice_being_off_does_not_replace_listening(self, run, env):
        env.speaking = True
        assert "L I S T E N I N G" in run([-1], voice_on=False).text

    def test_the_badge_says_which_sessions_can_speak(self, run, env):
        assert "  VOICE ON  " in run([-1]).text
        assert "  VOICE OFF  " in run([-1], voice_on=False).text
        env.focus = ("live", "voice · claude")
        assert "VOICE ON · ONE SESSION" in run([-1]).text

    def test_the_title_sits_above_the_badge(self, run):
        assert "C L A U D E" in run([-1]).row(1)

    def test_a_window_too_small_says_only_that(self, run):
        screen = run([-1], h=10, w=30)
        assert "window too small" in screen.text
        assert "C L A U D E" not in screen.text

    def test_the_plugin_row_sits_above_the_title(self, run, env):
        assert "branch main" not in run([-1]).text
        env.plugin_panels = [a_panel(a_row("branch", "main"))]
        assert "branch main" in run([-1]).row(0)

    def test_a_full_screen_never_raises(self, run, env):
        # Every addstr fails, which is what a window resized mid-frame does.
        env.agents = ["one"]
        env.state = {"state": "speaking", "text": "said", "ts": hud.time.time()}
        run([-1], failing=True)


class TestLegend:
    """The key row, and the three widths it degrades through."""

    def test_the_keys_say_what_they_will_do(self, run):
        keys = run([-1]).row(3)
        for key in ("m:", "f:", "d: dictate", "c: conversation", "t: session", "q: quit"):
            assert key in keys

    def test_the_voice_key_names_the_next_state(self, run):
        assert "turn OFF and silence" in run([-1]).row(3)
        assert "turn the voice ON" in run([-1], voice_on=False).row(3)

    def test_the_focus_key_names_the_next_state(self, run, env):
        assert "mute the rest" in run([-1]).row(3)
        env.focus = ("live", "voice · claude")
        assert "unmute the rest" in run([-1]).row(3)

    def test_the_history_key_names_the_next_state(self, run, env):
        assert "h: history" in run([-1]).row(3)
        env.panel = True
        assert "h: hide history" in run([-1]).row(3)

    def test_a_language_with_no_voice_on_disk_gets_no_key(self, run, env):
        assert "l:" not in run([-1]).row(3)
        env.language = ("xx", "Español")
        assert "l: Español" in run([-1]).row(3)

    def test_a_wide_window_keeps_the_wide_separators(self, run, env):
        env.language = ("xx", "Español")
        assert "   ·   " in run([-1], w=160).row(3)

    def test_a_narrower_window_tightens_the_separators(self, run, env):
        env.language = ("xx", "Español")
        assert "  ·  " in run([-1], w=145).row(3)
        keys = run([-1], w=130).row(3)
        assert "  ·  " not in keys
        assert " · " in keys

    def test_a_narrower_window_still_shortens_the_labels(self, run, env):
        env.language = ("xx", "Español")
        assert "m: OFF, silence · f: mute the rest" in run([-1], w=120).row(3)
        assert "m: OFF, silence · f: mute rest" in run([-1], w=114).row(3)


class TestKeys:
    """One keystroke, one shared action -- and a refusal that is never swallowed."""

    @pytest.mark.parametrize(
        ("key", "action"),
        [
            ("m", "voice"),
            (" ", "voice"),
            ("f", "focus"),
            ("d", "dictate"),
            ("c", "conversation"),
            ("t", "session"),
            ("l", "language"),
            ("x", "sweep"),
        ],
    )
    def test_a_key_runs_its_action(self, run, env, key, action):
        run([ord(key)])
        assert env.acted == [action]

    def test_an_unbound_key_runs_nothing(self, run, env):
        run([ord("z")])
        assert env.acted == []

    def test_a_refusal_is_put_on_screen(self, run, env):
        env.results["dictate"] = (False, "no Claude Code session")
        assert "⚠ no Claude Code session" in run([ord("d")]).row(4)

    def test_a_message_is_put_on_screen_as_a_notice(self, run, env):
        env.results["voice"] = (True, "voice off, silence")
        assert "· voice off, silence ·" in run([ord("m")]).row(4)

    def test_a_silent_success_says_nothing(self, run, env):
        env.results["dictate"] = (True, "")
        assert run([ord("d")]).row(4).strip() == ""

    def test_q_quits(self, run, env):
        screen = run([ord("q")])
        assert screen.erases == 0  # it never got as far as a frame

    def test_escape_quits(self, run, env):
        assert run([27]).erases == 0

    def test_h_opens_the_panel_and_remembers_it(self, run, env):
        run([ord("h")])
        assert env.saved == [True]

    def test_h_closes_the_panel_again(self, run, env):
        env.panel = True
        run([ord("h")])
        assert env.saved == [False]

    def test_in_a_narrow_window_q_leaves_the_panel_first(self, run, env):
        # There the panel is a view you are inside, so q means back.
        env.panel = True
        screen = run([ord("q")], w=60)
        assert env.saved == [False]
        assert screen.erases == 1  # the frame drawn after backing out

    @pytest.mark.parametrize(
        ("key", "expected"),
        [
            (curses.KEY_UP, 1),
            (ord("k"), 1),
            (curses.KEY_PPAGE, 10),
            (ord("g"), 29),  # clamped to the oldest line
        ],
    )
    def test_the_panel_scrolls_back(self, run, env, key, expected):
        env.panel, env.rows = True, LONG_ROWS
        screen = run([key, -1], h=40, w=120)
        assert f"↓ {expected} newer" in screen.text

    @pytest.mark.parametrize("key", [curses.KEY_DOWN, ord("j"), curses.KEY_NPAGE, ord("G")])
    def test_the_panel_never_scrolls_below_the_newest_line(self, run, env, key):
        env.panel, env.rows = True, ROWS
        assert "newer" not in run([key, -1]).text

    def test_scroll_keys_do_nothing_while_the_panel_is_shut(self, run, env):
        env.rows = LONG_ROWS
        assert "newer" not in run([curses.KEY_UP, -1]).text


class TestHistoryPanel:
    """The panel beside, under, or instead of the reactor."""

    def test_the_panel_shares_the_window_with_the_reactor(self, run, env):
        env.panel, env.rows = True, ROWS
        screen = run([-1])
        assert "H I S T O R Y" in screen.text
        assert "line 11" in screen.text
        assert "S T A N D I N G   B Y" in screen.text  # the reactor kept animating
        assert "│" in screen.glyphs()

    def test_a_bottom_strip_is_ruled_off_horizontally(self, run, env):
        env.panel, env.position, env.rows = True, "bottom", ROWS
        screen = run([-1])
        assert any(t.startswith("─") for (_y, _x, t, _a) in screen.writes)

    def test_a_strip_drops_the_last_spoken_line(self, run, env):
        # The strip below already ends with it.
        env.state = {"state": "speaking", "text": "said aloud", "ts": hud.time.time()}
        env.panel, env.position = True, "bottom"
        assert "«said aloud»" not in run([-1]).text

    def test_a_narrow_window_gives_the_panel_the_whole_screen(self, run, env):
        env.panel, env.rows = True, ROWS
        screen = run([-1, ord("q")], w=60)
        assert "h/q: back" in screen.frame(1)
        assert "line 11" in screen.frame(1)
        assert "S T A N D I N G   B Y" not in screen.frame(1)


class TestMicNotices:
    """An open microphone is the thing this window exists to make obvious."""

    def test_an_open_microphone_with_a_daemon_says_conversation(self, run, env):
        env.open, env.daemon = True, True
        assert "● CONVERSATION — microphone open" in run([-1]).text

    def test_an_open_microphone_with_no_owner_offers_the_key(self, run, env):
        env.open = True
        assert "⚠ MICROPHONE OPEN, NO OWNER — press x" in run([-1]).text

    def test_a_stranded_microphone_says_you_are_talking_to_nothing(self, run, env):
        env.open, env.daemon, env.stranded, env.speaking = True, True, "no session", True
        assert "you are talking to nothing" in run([-1]).text

    def test_a_stranded_conversation_at_rest_says_it_is_on_hold(self, run, env):
        env.open, env.daemon, env.stranded = True, True, "no session"
        assert "conversation on hold" in run([-1]).text

    def test_a_microphone_held_by_somebody_else_is_named_without_alarm(self, run, env):
        env.held = ["firefox"]
        screen = run([-1])
        assert "mic held open by firefox — not recording" in screen.text
        assert "⚠" not in screen.text

    def test_the_notice_moves_onto_the_divider_under_a_strip(self, run, env):
        env.open, env.panel, env.position = True, True, "bottom"
        _panel, _reactor, divider = hud.layout(40, 120, True)
        assert "NO OWNER" in run([-1]).row(divider[1])


class TestFooter:
    """Where the voice goes and where dictation goes, when they differ."""

    def test_a_focus_on_a_dead_pane_is_the_loudest_thing_on_the_row(self, run, env):
        env.focus = ("gone", "voice · claude")
        assert "⚠ voice held by voice · claude — that pane is gone, press f" in run([-1]).text

    def test_voice_and_dictation_pointing_apart_are_both_named(self, run, env):
        env.focus = ("live", "one")
        env.here = False
        env.target = "two"
        assert "⚠ voice → one · dictation → two" in run([-1]).text

    def test_a_focus_pointing_nowhere_shows_a_dash(self, run, env):
        env.focus = ("live", "one")
        env.here = False
        assert "dictation → —" in run([-1]).text

    def test_a_target_alone_is_stated_plainly(self, run, env):
        env.target = "claude-voice · claude"
        assert "dictation → claude-voice · claude" in run([-1]).text

    def test_a_focused_target_says_the_voice_goes_there_too(self, run, env):
        env.focus = ("live", "claude-voice · claude")
        env.target = "claude-voice · claude"
        assert "voice + dictation → claude-voice · claude" in run([-1]).text

    def test_nothing_to_dictate_to_is_a_warning(self, run, env):
        env.blocked = "no Claude Code session"
        assert "⚠ no Claude Code session — dictation disabled" in run([-1]).text


class TestShutdown:
    """What the window takes with it when it closes."""

    def test_the_last_window_out_closes_everything(self, monkeypatch, home):
        done = []
        monkeypatch.setattr(hud._presence, "leave", lambda: done.append("leave"))
        monkeypatch.setattr(hud._presence, "last_one_out", lambda: True)
        monkeypatch.setattr(hud, "conversation_alive", lambda: True)
        monkeypatch.setattr(hud.core, "conversation_stop", lambda: done.append("conversation"))
        monkeypatch.setattr(hud.core, "run", lambda *a, **kw: done.append(("run", a)))
        monkeypatch.setattr(hud, "sweep_orphans", lambda: done.append("sweep"))
        hud.shutdown()
        assert done == ["leave", "conversation", ("run", ("voice.py", "silence")), "sweep"]

    def test_no_conversation_running_is_nothing_to_stop(self, monkeypatch, home):
        done = []
        monkeypatch.setattr(hud._presence, "leave", lambda: None)
        monkeypatch.setattr(hud._presence, "last_one_out", lambda: True)
        monkeypatch.setattr(hud, "conversation_alive", lambda: False)
        monkeypatch.setattr(hud.core, "conversation_stop", lambda: done.append("conversation"))
        monkeypatch.setattr(hud.core, "run", lambda *a, **kw: None)
        monkeypatch.setattr(hud, "sweep_orphans", lambda: None)
        hud.shutdown()
        assert done == []

    def test_another_window_still_up_keeps_the_voice(self, monkeypatch, home):
        done = []
        monkeypatch.setattr(hud._presence, "leave", lambda: done.append("leave"))
        monkeypatch.setattr(hud._presence, "last_one_out", lambda: False)
        monkeypatch.setattr(hud, "sweep_orphans", lambda: done.append("sweep"))
        hud.shutdown()
        assert done == ["leave"]

    def test_a_failure_on_the_way_out_is_not_worth_a_traceback(self, monkeypatch, home):
        def boom(*a, **kw):
            raise RuntimeError("gone")

        monkeypatch.setattr(hud._presence, "leave", lambda: None)
        monkeypatch.setattr(hud._presence, "last_one_out", lambda: True)
        monkeypatch.setattr(hud, "conversation_alive", boom)
        monkeypatch.setattr(hud.core, "run", boom)
        monkeypatch.setattr(hud, "sweep_orphans", boom)
        hud.shutdown()

    def test_a_closed_terminal_exits_rather_than_leaving_the_microphone(self):
        import signal

        with pytest.raises(SystemExit):
            hud._bye(signal.SIGHUP, None)
