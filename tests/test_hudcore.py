"""The shared state layer behind the three HUD front ends.

Everything here is a question two windows have to answer identically, so the
tests are written against the public functions rather than against whichever
window happens to call them. Where a branch is only reachable through one of
the module's caches, the cache is poked directly and said so.

Nothing in this file may spawn, sleep or wait. `spawn_guard` makes a stray
subprocess an error rather than a slow test, and every poll loop in the module
is driven by a patched clock.
"""

import json
import os
import time
import types

import pytest

import claude_voice.hudcore as hudcore

# --- the harness ---------------------------------------------------------


@pytest.fixture(autouse=True)
def fresh(home, monkeypatch):
    """Every module-level cache back to its import-time value.

    hudcore is written as one long-lived process, so its caches are globals
    with no reset of their own. Two tests sharing a two-second window would
    otherwise see each other's answers, which is the one failure mode a cache
    test cannot distinguish from a bug.
    """
    hudcore._target_cache.update(t=0.0, pane={})
    hudcore._focus_cache.update(t=0.0, val=("", ""), pane="")
    hudcore._session_cache.update(t=0.0, key=None, sid="", cwd="")
    hudcore._agents_cache.update(t=0.0, list=[])
    hudcore._hist_sid.update(t=0.0, key=None, sid="")
    hudcore._hist_cache.update(mtime=-1.0, w=-1, sid=None, rows=[])
    hudcore._lang_cache.update(preset=None, name="", label="")
    hudcore._mods.clear()
    # spokenlog memoises "the liveliest session" for two seconds, and that is
    # long enough to leak one test's log into the next one.
    if hudcore._spokenlog is not None:
        hudcore._spokenlog._newest.update(t=0.0, key=None, sid="")
    hudcore.reload_cfg()
    yield
    hudcore._mods.clear()


@pytest.fixture(autouse=True)
def spawn_guard(monkeypatch):
    """Nothing may actually be launched, and nothing may touch the machine.

    The expensive microphone questions and the GitHub lookup are stubbed to
    their quiet answers rather than left real: a unit test that shells out to
    pw-dump reports whatever the developer's desktop happens to be doing.
    """

    def _no(*a, **kw):
        raise RuntimeError(f"the test tried to spawn {a[:1]}")

    monkeypatch.setattr(hudcore.subprocess, "run", _no)
    monkeypatch.setattr(hudcore.subprocess, "Popen", _no)
    monkeypatch.setattr(hudcore, "mic_open", lambda fresh=False: False)
    monkeypatch.setattr(hudcore, "mic_held", lambda: [])
    monkeypatch.setattr(hudcore, "sweep_orphans", lambda: 0)
    monkeypatch.setattr(hudcore._repo, "info", lambda where: {})


@pytest.fixture
def pane(monkeypatch):
    """Answer the `dictate.py --target` query without running it.

    Patched at the subprocess seam rather than at ``dictate_target_info`` so
    that the two-second cache and every ``fresh=True`` that clears it stay in
    the code under test.
    """
    box = {"out": "{}", "calls": 0}

    def _run(cmd, **kw):
        box["calls"] += 1
        return types.SimpleNamespace(stdout=box["out"], stderr="", returncode=0)

    monkeypatch.setattr(hudcore.subprocess, "run", _run)

    def _set(d=None, raw=None):
        box["out"] = box["out"] if d is None and raw is None else (raw or json.dumps(d))
        hudcore._target_cache["t"] = 0.0
        return box

    _set.box = box
    return _set


@pytest.fixture
def thinking(monkeypatch):
    """A stand-in for the heartbeat module, which is where agents live."""
    mod = types.SimpleNamespace(
        session_for=lambda cwd, title, pane="": "",
        agents_live=lambda sid, cwd: [],
        sessions_in=lambda cwd: [],
        bound_session=lambda pane, cwd="": "",
    )
    hudcore._mods["thinking"] = mod
    return mod


@pytest.fixture
def dictate(monkeypatch):
    """A stand-in for dictate.py, asked only for the list of Claude panes."""
    mod = types.SimpleNamespace(claude_panes=lambda: [])
    hudcore._mods["dictate"] = mod
    return mod


def unstattable():
    """A marker file that refuses every question asked of it.

    Patched in place of a real path rather than patching pathlib itself: a
    Path.exists that raises for the rest of the test also breaks the config
    reload the harness does on the way out.
    """

    def _boom(*a, **kw):
        raise OSError("unreadable")

    return types.SimpleNamespace(
        exists=_boom, touch=_boom, unlink=_boom, parent=types.SimpleNamespace(mkdir=_boom)
    )


def a_pane(**over):
    """A `--target` answer that says yes."""
    d = {"ok": True, "pane_id": "%1", "path": "/proj/one", "dir": "one", "title": "a task"}
    d.update(over)
    return d


# --- labels and the title ------------------------------------------------


class TestLabels:
    """`L` is the only way a front end asks the language pack for a word."""

    def test_a_missing_key_reads_as_its_fallback(self):
        assert hudcore.L("no_such_label", "FALLBACK") == "FALLBACK"

    def test_a_configured_key_wins(self, write_config):
        write_config('[hud]\nidle = "D O R M I D O"\n')
        hudcore.reload_cfg()
        assert hudcore.L("idle", "IDLE") == "D O R M I D O"

    def test_an_empty_value_reads_as_the_fallback(self, write_config):
        # A language pack that blanks a label must not blank the HUD: an empty
        # string is a missing translation, not a request for no word at all.
        write_config('[hud]\nidle = ""\n')
        hudcore.reload_cfg()
        assert hudcore.L("idle", "IDLE") == "IDLE"

    def test_the_title_is_letterspaced(self):
        assert " ".join(hudcore.CFG.name.upper()) == hudcore.TITLE

    def test_reload_picks_up_a_new_title_without_reopening(self, write_config):
        write_config('[hud]\ntitle = "borra"\n')
        hudcore.reload_cfg()
        assert hudcore.TITLE == "B O R R A"


class TestNextLanguage:
    """The legend has to name the language `l` switches into, cheaply."""

    def test_it_names_the_following_preset_and_how_it_calls_itself(self, monkeypatch):
        monkeypatch.setattr(hudcore._lang, "following", lambda p: "es")
        monkeypatch.setattr(hudcore._lang, "label", lambda p: "Espanol")
        assert hudcore.next_language() == ("es", "Espanol")

    def test_it_is_asked_once_per_preset(self, monkeypatch):
        calls = []
        monkeypatch.setattr(hudcore._lang, "following", lambda p: calls.append(p) or "es")
        monkeypatch.setattr(hudcore._lang, "label", lambda p: "Espanol")
        hudcore.next_language()
        hudcore.next_language()
        assert calls == [hudcore.CFG.preset]

    def test_a_preset_with_no_successor_has_no_label(self, monkeypatch):
        # following() returns "" when nothing else on disk can speak; asking
        # for the label of "" would be a lookup with no answer.
        monkeypatch.setattr(hudcore._lang, "following", lambda p: "")
        monkeypatch.setattr(hudcore._lang, "label", lambda p: pytest.fail("asked anyway"))
        assert hudcore.next_language() == ("", "")


# --- the pane dictation is aimed at --------------------------------------


class TestDictateTargetInfo:
    """The one tmux question every other one is built on."""

    def test_it_parses_the_helper_output(self, pane):
        pane(a_pane())
        assert hudcore.dictate_target_info()["pane_id"] == "%1"

    def test_the_answer_is_held_for_two_seconds(self, pane):
        pane(a_pane())
        hudcore.dictate_target_info()
        pane(a_pane(pane_id="%9"))
        hudcore._target_cache["t"] = time.time()  # undo the reset _set() does
        assert hudcore.dictate_target_info()["pane_id"] == "%1"
        assert pane.box["calls"] == 1

    def test_unparseable_output_is_no_target_rather_than_a_crash(self, pane):
        pane(raw="not json at all")
        assert hudcore.dictate_target_info() == {}

    def test_empty_output_is_no_target(self, pane):
        pane(raw="   ")
        assert hudcore.dictate_target_info() == {}

    def test_a_helper_that_will_not_run_is_no_target(self, monkeypatch):
        assert hudcore.dictate_target_info() == {}  # spawn_guard makes it raise


class TestDictateTarget:
    """How the watched session is named on screen."""

    def test_it_joins_the_directory_and_the_title(self, pane):
        pane(a_pane())
        assert hudcore.dictate_target() == "one · a task"

    def test_a_refused_target_has_no_name(self, pane):
        pane({"ok": False, "why": "no window"})
        assert hudcore.dictate_target() == ""

    def test_a_missing_title_leaves_no_dangling_separator(self, pane):
        pane(a_pane(title=""))
        assert hudcore.dictate_target() == "one"


class TestDictateBlocked:
    """The warning that nobody is on the other end."""

    def test_a_working_target_is_not_blocked(self, pane):
        pane(a_pane())
        assert hudcore.dictate_blocked() == ""

    def test_the_helper_reason_is_passed_through(self, pane):
        pane({"ok": False, "why": "target session is gone"})
        assert hudcore.dictate_blocked() == "target session is gone"

    def test_a_refusal_with_no_reason_still_says_something(self, pane):
        pane({"ok": False})
        assert hudcore.dictate_blocked() == "no Claude Code session"

    def test_fresh_spends_a_query_rather_than_trusting_the_cache(self, pane):
        pane({"ok": False, "why": "no window"})
        hudcore.dictate_blocked()
        pane(a_pane())
        hudcore._target_cache["t"] = time.time()
        assert hudcore.dictate_blocked() == "no window"
        assert hudcore.dictate_blocked(fresh=True) == ""


# --- focus ---------------------------------------------------------------


class TestFocusState:
    """Whether the pane holding the voice is still there."""

    def test_nothing_focused_is_two_empty_strings(self, dictate):
        assert hudcore.focus_state() == ("", "")

    def test_a_focused_pane_with_a_claude_in_it_is_live(self, dictate):
        dictate.claude_panes = lambda: [{"pane_id": "%1", "dir": "one", "title": "a task"}]
        hudcore._focus.set_pane("%1", "one · a task")
        assert hudcore.focus_state() == ("live", "one · a task")

    def test_a_focused_pane_that_has_been_closed_is_gone(self, dictate):
        # The whole reason this asks tmux instead of reading focus.json: a
        # focus on a closed window silences every session, everywhere.
        hudcore._focus.set_pane("%7", "two · gone")
        assert hudcore.focus_state() == ("gone", "two · gone")

    def test_a_gone_pane_with_no_stored_label_falls_back_to_its_id(self, dictate):
        hudcore._focus.set_pane("%7")
        assert hudcore.focus_state() == ("gone", "%7")

    def test_a_pane_query_that_fails_reads_as_no_panes(self, monkeypatch):
        def _boom():
            raise OSError("no tmux")

        hudcore._mods["dictate"] = types.SimpleNamespace(claude_panes=_boom)
        hudcore._focus.set_pane("%7", "two")
        assert hudcore.focus_state() == ("gone", "two")

    def test_the_answer_is_held_for_two_seconds(self, dictate):
        hudcore.focus_state()
        hudcore._focus.set_pane("%7", "two")
        assert hudcore.focus_state() == ("", "")

    def test_fresh_reasks_immediately(self, dictate):
        hudcore.focus_state()
        hudcore._focus.set_pane("%7", "two")
        assert hudcore.focus_state(fresh=True) == ("gone", "two")


class TestFocusHere:
    """Whether the voice and the keyboard point at the same window."""

    def test_the_focused_pane_being_the_target_is_here(self, pane, dictate):
        pane(a_pane())
        hudcore._focus.set_pane("%1", "one")
        assert hudcore.focus_here() is True

    def test_a_focus_pointing_somewhere_else_is_not_here(self, pane, dictate):
        pane(a_pane())
        hudcore._focus.set_pane("%2", "two")
        assert hudcore.focus_here() is False

    def test_no_focus_at_all_is_not_here(self, pane, dictate):
        pane(a_pane())
        assert hudcore.focus_here() is False


# --- the module loader ---------------------------------------------------


class TestModuleLoader:
    """Siblings are loaded by path, once, because read_state runs per frame."""

    def test_a_sibling_is_loaded_and_kept(self):
        first = hudcore._mod("turn")
        assert first is hudcore._mod("turn")
        assert hasattr(first, "safe_session")

    def test_the_heartbeat_module_is_the_one_agents_come_from(self):
        assert hudcore._thinking() is hudcore._mod("thinking")


# --- which session is on screen ------------------------------------------


class TestTargetSession:
    """The uuid behind the pane, which everything session-shaped hangs off."""

    def test_it_resolves_the_pane_to_a_session(self, pane, thinking):
        pane(a_pane())
        thinking.session_for = lambda cwd, title, p="": "sid-1"
        assert hudcore.target_session() == ("sid-1", "/proj/one")

    def test_a_pane_with_no_path_is_not_looked_up(self, pane, thinking):
        thinking.session_for = lambda *a, **k: pytest.fail("looked up anyway")
        pane({"ok": False})
        assert hudcore.target_session() == ("", "")

    def test_a_lookup_that_fails_leaves_the_directory_intact(self, pane, thinking):
        def _boom(*a, **k):
            raise OSError("no transcripts")

        thinking.session_for = _boom
        pane(a_pane())
        assert hudcore.target_session() == ("", "/proj/one")

    def test_the_same_pane_is_only_resolved_once(self, pane, thinking):
        calls = []
        thinking.session_for = lambda cwd, title, p="": calls.append(cwd) or "sid-1"
        pane(a_pane())
        hudcore.target_session()
        hudcore.target_session()
        assert calls == ["/proj/one"]

    def test_switching_pane_reresolves_before_the_cache_expires(self, pane, thinking):
        seen = []
        thinking.session_for = lambda cwd, title, p="": seen.append(p) or f"sid{p}"
        pane(a_pane())
        assert hudcore.target_session()[0] == "sid%1"
        pane(a_pane(pane_id="%2"))
        assert hudcore.target_session()[0] == "sid%2"
        assert seen == ["%1", "%2"]


class TestAgentsLive:
    """Subagents of the watched session, and of no other."""

    def test_it_reports_the_watched_session_agents(self, pane, thinking):
        pane(a_pane())
        thinking.session_for = lambda *a, **k: "sid-1"
        thinking.agents_live = lambda sid, cwd: [f"work in {sid}"]
        assert hudcore.agents_live() == ["work in sid-1"]

    def test_the_sweep_is_held_for_a_second_and_a_half(self, pane, thinking):
        pane(a_pane())
        calls = []
        thinking.agents_live = lambda sid, cwd: calls.append(1) or []
        hudcore.agents_live()
        hudcore.agents_live()
        assert len(calls) == 1

    def test_a_failing_sweep_is_no_agents(self, pane, thinking):
        def _boom(sid, cwd):
            raise OSError("disk")

        thinking.agents_live = _boom
        pane(a_pane())
        assert hudcore.agents_live() == []


# --- the history panel ---------------------------------------------------


class TestHistorySession:
    """Whose conversation the panel is showing."""

    def test_a_named_session_needs_no_guess(self, pane, thinking):
        pane(a_pane())
        thinking.session_for = lambda *a, **k: "sid-1"
        assert hudcore.history_session() == "sid-1"

    def test_an_unnamed_pane_falls_back_to_the_shared_policy(self, pane, thinking, monkeypatch):
        pane(a_pane())
        monkeypatch.setattr(hudcore._spokenlog, "follow", lambda s, cwd: f"newest-in-{cwd}")
        assert hudcore.history_session() == "newest-in-/proj/one"

    def test_the_fallback_is_held_for_two_seconds(self, pane, thinking, monkeypatch):
        pane(a_pane())
        calls = []
        monkeypatch.setattr(
            hudcore._spokenlog, "follow", lambda s, cwd: calls.append(cwd) or "guessed"
        )
        hudcore.history_session()
        hudcore.history_session()
        assert calls == ["/proj/one"]

    def test_a_fallback_that_raises_is_no_session(self, pane, thinking, monkeypatch):
        def _boom(s, cwd):
            raise OSError("unreadable")

        pane(a_pane())
        monkeypatch.setattr(hudcore._spokenlog, "follow", _boom)
        assert hudcore.history_session() == ""


def fake_log(monkeypatch, entries, mtime=1.0):
    """Replace the spoken log wholesale, so a row test is about wrapping."""
    box = {"calls": 0}

    def _tail(n, sid):
        box["calls"] += 1
        return entries

    monkeypatch.setattr(
        hudcore,
        "_spokenlog",
        types.SimpleNamespace(tail=_tail, mtime=lambda sid: mtime, follow=lambda s, c: "sid-1"),
    )
    return box


class TestHistoryRows:
    """The panel's wrapped rows, for the front end that cannot wrap itself."""

    def test_a_pane_too_narrow_to_read_shows_nothing(self, monkeypatch):
        fake_log(monkeypatch, [{"t": 0, "side": "out", "text": "hello"}])
        assert hudcore.history_rows(10) == []

    def test_no_log_module_shows_nothing(self, monkeypatch):
        monkeypatch.setattr(hudcore, "_spokenlog", None)
        assert hudcore.history_rows(80) == []

    def test_a_spoken_line_is_labelled_said_and_pointed_inward(self, monkeypatch, pane, thinking):
        pane(a_pane())
        fake_log(monkeypatch, [{"t": 0, "side": "out", "text": "the tests pass"}])
        rows = hudcore.history_rows(80)
        assert len(rows) == 1
        text, side, cont = rows[0]
        assert side == "out" and cont is False
        assert "said ‹ the tests pass" in text

    def test_a_dictated_line_is_labelled_you_and_pointed_outward(self, monkeypatch, pane, thinking):
        pane(a_pane())
        fake_log(monkeypatch, [{"t": 0, "side": "in", "text": "run it again"}])
        text, side, cont = hudcore.history_rows(80)[0]
        assert side == "in" and "you › run it again" in text

    def test_a_timestamp_is_shown_when_there_is_one(self, monkeypatch, pane, thinking):
        pane(a_pane())
        when = time.mktime((2026, 8, 27, 14, 5, 0, 0, 0, -1))
        fake_log(monkeypatch, [{"t": when, "side": "out", "text": "x"}])
        assert hudcore.history_rows(80)[0][0].startswith("14:05")

    def test_a_long_line_continues_on_indented_rows(self, monkeypatch, pane, thinking):
        pane(a_pane())
        fake_log(monkeypatch, [{"t": 0, "side": "out", "text": "word " * 40}])
        rows = hudcore.history_rows(40)
        assert len(rows) > 1
        assert rows[0][2] is False
        assert all(cont is True and text.startswith(" ") for text, _, cont in rows[1:])

    def test_rows_are_rebuilt_only_when_the_log_moves(self, monkeypatch, pane, thinking):
        pane(a_pane())
        box = fake_log(monkeypatch, [{"t": 0, "side": "out", "text": "one"}])
        first = hudcore.history_rows(80)
        assert hudcore.history_rows(80) == first
        assert box["calls"] == 1

    def test_a_log_that_will_not_stat_shows_nothing(self, monkeypatch, pane, thinking):
        def _boom(sid):
            raise OSError("gone")

        pane(a_pane())
        monkeypatch.setattr(
            hudcore, "_spokenlog", types.SimpleNamespace(mtime=_boom, follow=lambda s, c: "")
        )
        assert hudcore.history_rows(80) == []

    def test_a_log_that_will_not_read_wraps_to_no_rows(self, monkeypatch, pane, thinking):
        def _boom(n, sid):
            raise OSError("gone")

        pane(a_pane())
        monkeypatch.setattr(
            hudcore,
            "_spokenlog",
            types.SimpleNamespace(mtime=lambda s: 1.0, tail=_boom, follow=lambda s, c: ""),
        )
        assert hudcore.history_rows(80) == []


class TestHistoryEntries:
    """The unwrapped answer, for a browser that does its own wrapping."""

    def test_it_returns_the_raw_entries(self, monkeypatch, pane, thinking):
        pane(a_pane())
        fake_log(monkeypatch, [{"t": 0, "side": "out", "text": "hello"}])
        assert hudcore.history_entries() == [{"t": 0, "side": "out", "text": "hello"}]

    def test_the_count_comes_from_the_config_when_none_is_given(
        self, monkeypatch, pane, thinking, write_config
    ):
        write_config("[history]\nshow = 7\n")
        hudcore.reload_cfg()
        seen = []
        monkeypatch.setattr(
            hudcore,
            "_spokenlog",
            types.SimpleNamespace(tail=lambda n, sid: seen.append(n) or [], follow=lambda s, c: ""),
        )
        pane(a_pane())
        hudcore.history_entries()
        hudcore.history_entries(3)
        assert seen == [7, 3]

    def test_no_log_module_has_no_entries(self, monkeypatch):
        monkeypatch.setattr(hudcore, "_spokenlog", None)
        assert hudcore.history_entries() == []

    def test_a_log_that_raises_has_no_entries(self, monkeypatch, pane, thinking):
        def _boom(n, sid):
            raise OSError("gone")

        pane(a_pane())
        monkeypatch.setattr(
            hudcore,
            "_spokenlog",
            types.SimpleNamespace(tail=_boom, follow=lambda s, c: ""),
        )
        assert hudcore.history_entries() == []


class TestPanelMarker:
    """Whether the panel was open, remembered as a file like the voice is."""

    def test_it_starts_closed(self):
        assert hudcore.panel_open() is False

    def test_opening_and_closing_survive_as_a_marker(self):
        hudcore.set_panel_open(True)
        assert hudcore.panel_open() is True
        hudcore.set_panel_open(False)
        assert hudcore.panel_open() is False

    def test_closing_twice_is_not_an_error(self):
        hudcore.set_panel_open(False)
        hudcore.set_panel_open(False)
        assert hudcore.panel_open() is False

    def test_a_marker_that_cannot_be_written_is_swallowed(self, monkeypatch):
        # Remembering a panel is never worth taking the window down for.
        monkeypatch.setattr(hudcore, "PANEL_OPEN", unstattable())
        hudcore.set_panel_open(True)
        hudcore.set_panel_open(False)

    def test_a_marker_that_cannot_be_stat_ed_reads_as_closed(self, monkeypatch):
        monkeypatch.setattr(hudcore, "PANEL_OPEN", unstattable())
        assert hudcore.panel_open() is False


class TestPosition:
    """Where the panel sits, with anything unrecognised reading left."""

    def test_the_default_is_left(self):
        assert hudcore.position() == "left"

    @pytest.mark.parametrize("want", ["right", "bottom", "left"])
    def test_the_three_understood_sides(self, write_config, want):
        write_config(f'[history]\nposition = "{want}"\n')
        hudcore.reload_cfg()
        assert hudcore.position() == want

    def test_case_and_spacing_do_not_matter(self, write_config):
        write_config('[history]\nposition = "  RIGHT "\n')
        hudcore.reload_cfg()
        assert hudcore.position() == "right"

    def test_a_side_that_does_not_exist_reads_left(self, write_config):
        write_config('[history]\nposition = "diagonal"\n')
        hudcore.reload_cfg()
        assert hudcore.position() == "left"


# --- what the session on screen is doing ---------------------------------


def speaker(**over):
    """Write the global speaker state, the way audioq does."""
    d = {"state": "speaking", "text": "a line", "until": time.time() + 30, "session": ""}
    d.update(over)
    hudcore.STATE.write_text(json.dumps(d))
    return d


class TestReadState:
    """One session file, with the global speaker laid over it."""

    def test_no_session_and_no_state_is_idle(self, pane, thinking):
        assert hudcore.read_state()["state"] == "idle"

    def test_an_unknown_session_falls_back_to_the_liveliest_one(self, pane, thinking):
        hudcore._mod("turn").write("other", "thinking", "elsewhere")
        assert hudcore.read_state()["state"] == "thinking"

    def test_the_watched_session_state_is_the_one_shown(self, pane, thinking):
        pane(a_pane())
        thinking.session_for = lambda *a, **k: "sid-1"
        turn = hudcore._mod("turn")
        turn.write("sid-1", "thinking", "mine")
        turn.write("sid-2", "ready", "somebody else")
        assert hudcore.read_state()["text"] == "mine"

    def test_a_turn_module_that_raises_leaves_the_hud_idle(self, pane, thinking):
        def _boom(*a, **k):
            raise OSError("unreadable")

        hudcore._mods["turn"] = types.SimpleNamespace(read=_boom, newest=_boom)
        assert hudcore.read_state() == {
            "state": "idle",
            "text": "",
            "until": 0,
            "ts": 0,
            "session": "",
        }

    def test_the_speaker_wins_while_its_line_belongs_to_this_session(self, pane, thinking):
        pane(a_pane())
        thinking.session_for = lambda *a, **k: "sid-1"
        hudcore._mod("turn").write("sid-1", "ready")
        speaker(session="sid-1")
        assert hudcore.read_state()["state"] == "speaking"

    def test_a_line_with_no_owner_is_shown_rather_than_swallowed(self, pane, thinking):
        # The CLI speaks with no session to name, and silence would be worse.
        pane(a_pane())
        thinking.session_for = lambda *a, **k: "sid-1"
        hudcore._mod("turn").write("sid-1", "ready")
        speaker(session="")
        assert hudcore.read_state()["state"] == "speaking"

    def test_another_session_line_does_not_reach_this_window(self, pane, thinking):
        pane(a_pane())
        thinking.session_for = lambda *a, **k: "sid-1"
        hudcore._mod("turn").write("sid-1", "thinking", "mine")
        speaker(session="sid-2")
        assert hudcore.read_state()["text"] == "mine"

    def test_a_speaking_state_past_its_deadline_expires_on_its_own(self, pane, thinking):
        pane(a_pane())
        thinking.session_for = lambda *a, **k: "sid-1"
        hudcore._mod("turn").write("sid-1", "ready")
        speaker(session="sid-1", until=time.time() - 1)
        assert hudcore.read_state()["state"] == "ready"

    def test_a_speaker_doing_anything_else_does_not_override(self, pane, thinking):
        pane(a_pane())
        thinking.session_for = lambda *a, **k: "sid-1"
        hudcore._mod("turn").write("sid-1", "thinking")
        speaker(state="ready", session="sid-1")
        assert hudcore.read_state()["state"] == "thinking"

    def test_an_unreadable_speaker_file_leaves_the_session_alone(self, pane, thinking):
        pane(a_pane())
        thinking.session_for = lambda *a, **k: "sid-1"
        hudcore._mod("turn").write("sid-1", "thinking")
        hudcore.STATE.write_text("{ not json")
        assert hudcore.read_state()["state"] == "thinking"


# --- conversation mode ---------------------------------------------------


class TestConversationAlive:
    """A pidfile is not an answer; a live process is."""

    def test_no_pidfile_is_not_alive(self):
        assert hudcore.conversation_alive() is False

    def test_our_own_pid_reads_as_alive(self):
        hudcore.LISTEN_PID.write_text(f"{os.getpid()}\n")
        assert hudcore.conversation_alive() is True

    def test_a_dead_pid_is_swept_rather_than_believed(self, monkeypatch):
        def _boom(pid, sig):
            raise ProcessLookupError

        monkeypatch.setattr(hudcore.os, "kill", _boom)
        hudcore.LISTEN_PID.write_text("424242")
        assert hudcore.conversation_alive() is False
        assert not hudcore.LISTEN_PID.exists()


class TestConversationStop:
    """Stopping, and then checking that the microphone actually closed."""

    def test_it_signals_the_whole_group_and_drops_the_pidfile(self, monkeypatch):
        # pw-record is a child of the daemon, so the group is the unit.
        sent = []
        monkeypatch.setattr(hudcore.os, "killpg", lambda pid, sig: sent.append((pid, sig)))
        monkeypatch.setattr(hudcore.time, "sleep", lambda s: None)
        hudcore.LISTEN_PID.write_text("991")
        hudcore.conversation_stop()
        assert sent == [(991, hudcore.signal.SIGTERM)]
        assert not hudcore.LISTEN_PID.exists()

    def test_a_microphone_still_open_afterwards_is_swept(self, monkeypatch):
        monkeypatch.setattr(hudcore.os, "killpg", lambda pid, sig: None)
        monkeypatch.setattr(hudcore.time, "sleep", lambda s: None)
        monkeypatch.setattr(hudcore, "mic_open", lambda fresh=False: True)
        swept = []
        monkeypatch.setattr(hudcore, "sweep_orphans", lambda: swept.append(1) or 1)
        hudcore.LISTEN_PID.write_text("991")
        hudcore.conversation_stop()
        assert swept == [1]

    def test_a_missing_pidfile_stops_nothing_and_raises_nothing(self, monkeypatch):
        monkeypatch.setattr(hudcore.time, "sleep", lambda s: None)
        hudcore.conversation_stop()


class TestConversationStart:
    """Launching the daemon, with somewhere for its dying words to go."""

    def test_it_launches_the_listener_detached_with_a_logfile(self, monkeypatch):
        seen = {}

        def _popen(cmd, **kw):
            seen["cmd"], seen["kw"] = cmd, kw
            return types.SimpleNamespace(pid=1)

        monkeypatch.setattr(hudcore.subprocess, "Popen", _popen)
        hudcore.conversation_start()
        assert seen["cmd"][1].endswith("listen.py")
        assert seen["kw"]["start_new_session"] is True
        assert seen["kw"]["stderr"] is not hudcore.subprocess.DEVNULL

    def test_a_log_that_cannot_be_opened_falls_back_to_discarding(self, monkeypatch, home):
        # Opening a directory fails, which is the cheapest honest stand-in for
        # a state directory that is not writable.
        monkeypatch.setattr(hudcore, "LISTEN_LOG", home)
        seen = {}
        monkeypatch.setattr(
            hudcore.subprocess,
            "Popen",
            lambda cmd, **kw: seen.update(kw) or types.SimpleNamespace(pid=1),
        )
        hudcore.conversation_start()
        assert seen["stderr"] is hudcore.subprocess.DEVNULL


class TestListenFailed:
    """The last line the daemon managed before it stopped existing."""

    def test_no_log_says_nothing(self):
        assert hudcore.listen_failed() == ""

    def test_the_last_line_is_what_is_shown(self):
        hudcore.LISTEN_LOG.write_text("starting\nModuleNotFoundError: no faster_whisper\n")
        assert hudcore.listen_failed() == "ModuleNotFoundError: no faster_whisper"

    def test_a_long_traceback_line_is_cut_to_its_tail(self):
        hudcore.LISTEN_LOG.write_text("x" * 200)
        assert hudcore.listen_failed() == "x" * 90

    def test_an_unreadable_log_says_nothing(self, monkeypatch, home):
        monkeypatch.setattr(hudcore, "LISTEN_LOG", home)
        assert hudcore.listen_failed() == ""


class TestRun:
    """How every helper script in the package is invoked from a key press."""

    def test_a_foreground_run_waits_and_discards_output(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(
            hudcore.subprocess,
            "run",
            lambda cmd, **kw: seen.update(cmd=cmd, kw=kw) or types.SimpleNamespace(returncode=0),
        )
        hudcore.run("voice.py", "silence")
        assert seen["cmd"][0] == hudcore.sys.executable
        assert seen["cmd"][1].endswith("voice.py") and seen["cmd"][2] == "silence"
        assert seen["kw"]["check"] is False

    def test_a_detached_run_does_not_wait(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(
            hudcore.subprocess,
            "Popen",
            lambda cmd, **kw: seen.update(cmd=cmd, kw=kw) or types.SimpleNamespace(pid=1),
        )
        hudcore.run("dictate.py", "--toggle", detach=True)
        assert seen["kw"]["start_new_session"] is True
        assert seen["cmd"][2] == "--toggle"


# --- the graphics card ---------------------------------------------------


class TestTheNumbersMovedButTheNamesDidNot:
    """system_stats and gpu_stats read /proc in sysstat.py now, so that the
    panel showing them could become a plugin. Both windows and a good deal
    of this suite still ask hudcore for them."""

    def test_gpu_stats_is_the_one_in_sysstat(self, monkeypatch):
        monkeypatch.setattr(hudcore._sysstat, "gpu_stats", lambda: {"busy": 3.0})
        assert hudcore.gpu_stats() == {"busy": 3.0}

    def test_system_stats_is_the_one_in_sysstat(self, monkeypatch):
        monkeypatch.setattr(hudcore._sysstat, "system_stats", lambda: {"cpu": 1.0})
        assert hudcore.system_stats() == {"cpu": 1.0}

    def test_a_panel_that_is_off_does_not_read_the_numbers(self, write_config, monkeypatch):
        write_config("[plugins.enabled]\nsystem = false\n")
        hudcore.reload_cfg()
        monkeypatch.setattr(hudcore._sysstat, "system_stats", lambda: pytest.fail("read anyway"))
        assert hudcore.system_stats() == {}


class TestVoiceOn:
    def test_the_switch_starts_off(self):
        assert hudcore.voice_on() is False

    def test_the_marker_is_the_switch(self):
        hudcore.ENABLED.touch()
        assert hudcore.voice_on() is True

    def test_a_marker_that_cannot_be_stat_ed_reads_off(self, monkeypatch):
        monkeypatch.setattr(hudcore, "ENABLED", unstattable())
        assert hudcore.voice_on() is False


class TestDisplayState:
    """The precedence both windows have to get identically right."""

    @pytest.fixture(autouse=True)
    def _quiet(self, monkeypatch, pane, thinking):
        monkeypatch.setattr(hudcore, "mic_speaking", lambda: False)
        monkeypatch.setattr(hudcore, "daemon_alive", lambda: False)
        monkeypatch.setattr(hudcore, "listen_stranded", lambda: "")
        monkeypatch.setattr(hudcore, "agents_live", lambda: [])
        hudcore.ENABLED.touch()
        self.pane, self.thinking = pane, thinking

    def _session(self, state, **over):
        self.pane(a_pane())
        self.thinking.session_for = lambda *a, **k: "sid-1"
        d = {"state": state, "text": "a line", "until": 0, "ts": time.time(), "session": "sid-1"}
        d.update(over)
        hudcore._mods["turn"] = types.SimpleNamespace(read=lambda s: d, newest=lambda: d)

    def test_a_working_session_is_thinking(self):
        self._session("thinking")
        assert hudcore.display_state()[0] == "thinking"

    def test_the_spoken_line_comes_back_with_the_state(self):
        self._session("speaking")
        st, said, agents, stranded = hudcore.display_state()
        assert (st, said, agents, stranded) == ("speaking", "a line", [], "")

    def test_a_session_that_has_been_thinking_for_hours_is_asleep(self):
        # No Stop hook ever fired; the alternative is a reactor that spins
        # overnight because a window was closed mid-answer.
        self._session("thinking", ts=time.time() - hudcore.IDLE_AFTER - 10)
        assert hudcore.display_state()[0] == "idle"

    def test_a_state_with_no_timestamp_reads_as_ancient(self):
        # A turn file with no clock in it cannot be shown to be recent, and
        # the safe reading of "cannot be shown to be recent" is asleep.
        self._session("thinking", ts=0)
        assert hudcore.display_state()[0] == "idle"

    def test_you_talking_wins_over_anything_i_am_doing(self, monkeypatch):
        self._session("thinking")
        monkeypatch.setattr(hudcore, "mic_speaking", lambda: True)
        assert hudcore.display_state()[0] == "listening"

    def test_nothing_on_the_other_end_is_louder_still(self, monkeypatch):
        self._session("thinking")
        monkeypatch.setattr(hudcore, "mic_speaking", lambda: True)
        monkeypatch.setattr(hudcore, "daemon_alive", lambda: True)
        monkeypatch.setattr(hudcore, "listen_stranded", lambda: "no session to send to")
        st, _, _, stranded = hudcore.display_state()
        assert st == "stranded" and stranded == "no session to send to"

    def test_a_stranded_marker_with_no_daemon_is_stale(self, monkeypatch):
        self._session("thinking")
        monkeypatch.setattr(hudcore, "listen_stranded", lambda: pytest.fail("asked anyway"))
        assert hudcore.display_state()[0] == "thinking"

    def test_agents_replace_thinking_with_who_is_thinking(self, monkeypatch):
        self._session("thinking")
        monkeypatch.setattr(hudcore, "agents_live", lambda: ["reading the config"])
        st, _, agents, _ = hudcore.display_state()
        assert st == "agents" and agents == ["reading the config"]

    def test_agents_do_not_take_the_state_from_the_speaker(self, monkeypatch):
        self._session("speaking")
        monkeypatch.setattr(hudcore, "agents_live", lambda: ["reading the config"])
        assert hudcore.display_state()[0] == "speaking"

    def test_the_voice_being_off_replaces_a_calm_state(self):
        self._session("idle")
        hudcore.ENABLED.unlink()
        assert hudcore.display_state()[0] == "voice_off"

    def test_the_voice_being_off_never_replaces_a_live_one(self):
        self._session("thinking")
        hudcore.ENABLED.unlink()
        assert hudcore.display_state()[0] == "thinking"


class TestPanels:
    """Which blocks the window draws at all."""

    def test_everything_is_on_by_default(self):
        # The two that became plugins answer here too, under the names both
        # windows draw them by, so that neither surface had to learn a word.
        assert hudcore.panels() == dict.fromkeys(list(hudcore.PANELS) + ["system", "repo"], True)

    def test_a_block_can_be_switched_off(self, write_config):
        write_config("[plugins.enabled]\ngithub = false\n")
        hudcore.reload_cfg()
        show = hudcore.panels()
        assert show["repo"] is False and show["system"] is True


class TestRepoNow:
    """The branch and pull request of the pane being watched."""

    def test_it_asks_about_the_target_real_path(self, pane, monkeypatch):
        # The pretty `dir` is a basename, and a basename would resolve against
        # whatever directory this process happens to be in.
        seen = []
        monkeypatch.setattr(hudcore._repo, "info", lambda w: seen.append(w) or {"branch": "main"})
        pane(a_pane())
        assert hudcore.repo_now() == {"branch": "main"}
        assert seen == ["/proj/one"]


class TestLevel:
    """How loud the voice is, for the reactor."""

    def test_the_mouth_wins_while_a_line_is_playing(self, monkeypatch):
        seen = []
        monkeypatch.setattr(
            hudcore._level, "at", lambda env, t0, step: seen.append((env, t0, step)) or 0.5
        )
        assert (
            hudcore.level_now({"state": "speaking", "env": [100], "t0": 9.0, "step": 0.04}) == 0.5
        )
        assert seen == [([100], 9.0, 0.04)]

    def test_otherwise_the_ear_is_the_answer(self, monkeypatch):
        monkeypatch.setattr(hudcore._level, "live", lambda: 0.25)
        assert hudcore.level_now({"state": "thinking"}) == 0.25

    def test_a_speaking_state_with_no_envelope_falls_back_to_the_ear(self, monkeypatch):
        monkeypatch.setattr(hudcore._level, "live", lambda: 0.1)
        assert hudcore.level_now({"state": "speaking"}) == 0.1

    def test_the_state_is_read_when_the_caller_has_not(self, monkeypatch, pane, thinking):
        monkeypatch.setattr(hudcore._level, "live", lambda: 0.0)
        assert hudcore.level_now() == 0.0

    def test_the_ear_alone_is_one_stat_and_one_read(self, monkeypatch):
        monkeypatch.setattr(hudcore._level, "publishing", lambda: True)
        monkeypatch.setattr(hudcore._level, "live", lambda: 0.75)
        assert hudcore.ear_level() == (True, 0.75)


class TestLevelShape:
    """What the browser gets instead of a number."""

    def test_a_line_being_spoken_carries_its_whole_envelope(self, monkeypatch):
        monkeypatch.setattr(
            hudcore,
            "read_state",
            lambda: {"state": "speaking", "env": [1, 2, 3], "t0": 7.0, "step": 0.02},
        )
        shape = hudcore.level_shape()
        assert shape["env"] == [1, 2, 3] and shape["t0"] == 7.0 and shape["step"] == 0.02
        assert shape["lead"] == hudcore._level.LEAD

    def test_silence_carries_no_envelope_to_interpolate(self, monkeypatch):
        monkeypatch.setattr(hudcore, "read_state", lambda: {"state": "idle", "env": [1], "t0": 7.0})
        shape = hudcore.level_shape()
        assert shape["env"] == [] and shape["t0"] == 0
        assert shape["step"] == hudcore._level.STEP


class TestSnapshot:
    """One frame, as plain JSON, so a socket never shows half a window."""

    @pytest.fixture(autouse=True)
    def _world(self, monkeypatch, pane, thinking, dictate):
        monkeypatch.setattr(hudcore, "mic_speaking", lambda: False)
        monkeypatch.setattr(hudcore, "daemon_alive", lambda: False)
        monkeypatch.setattr(hudcore, "listen_stranded", lambda: "")
        monkeypatch.setattr(hudcore, "gpu_stats", lambda: None)
        monkeypatch.setattr(hudcore._lang, "following", lambda p: "es")
        monkeypatch.setattr(hudcore._lang, "label", lambda p: "Espanol")
        thinking.session_for = lambda *a, **k: "sid-1"
        pane(a_pane())
        self.pane = pane

    def test_it_is_json_serialisable_in_one_piece(self):
        json.dumps(hudcore.snapshot())

    def test_it_names_the_window_the_state_and_the_language(self):
        hudcore.ENABLED.touch()
        s = hudcore.snapshot()
        assert s["title"] == hudcore.TITLE
        assert s["voice_on"] is True
        assert s["language"] == {
            "preset": hudcore.CFG.preset,
            "name": hudcore.CFG.get("general.language", ""),
            "next": "es",
            "next_label": "Espanol",
        }

    def test_the_state_label_comes_from_the_language_pack(self):
        s = hudcore.snapshot()
        assert s["label"] == hudcore.L(hudcore.STATE_LABELS[s["state"]], s["state"].upper())

    def test_a_state_with_no_label_of_its_own_borrows_idle(self, monkeypatch):
        monkeypatch.setattr(hudcore, "display_state", lambda: ("weird", "", [], ""))
        assert hudcore.snapshot()["label"] == hudcore.L("idle", "WEIRD")

    def test_the_session_block_names_the_pane_being_watched(self):
        s = hudcore.snapshot()
        assert s["session"] == {"id": "sid-1", "dir": "one", "title": "a task"}

    def test_a_pane_that_already_knows_its_session_is_not_looked_up_again(self):
        self.pane(a_pane(session="sid-from-pane"))
        assert hudcore.snapshot()["session"]["id"] == "sid-from-pane"

    def test_a_recording_in_progress_is_visible(self):
        (hudcore.BASE / "dictate.pid").write_text("1")
        assert hudcore.snapshot()["dictation"]["recording"] is True

    def test_the_repo_block_is_empty_when_the_panel_is_off(self, write_config, monkeypatch):
        # Off is off: a hidden panel does not quietly keep calling GitHub.
        write_config("[plugins.enabled]\ngithub = false\n")
        hudcore.reload_cfg()
        monkeypatch.setattr(hudcore._repo, "info", lambda w: pytest.fail("asked anyway"))
        assert hudcore.snapshot()["repo"] == {}

    def test_every_label_the_front_ends_draw_is_present(self):
        labels = hudcore.snapshot()["labels"]
        assert set(labels) == {
            "history",
            "history_empty",
            "history_you",
            "history_said",
            "mic_ready",
            "mic_hearing",
            "mic_deaf",
        }
        assert all(labels.values())


# --- the things a key press does -----------------------------------------


class TestActVoice:
    """The switch, which off means shut up now rather than don't start."""

    def test_turning_it_on_writes_the_marker(self, monkeypatch):
        monkeypatch.setattr(hudcore, "run", lambda *a, **k: pytest.fail("silenced anyway"))
        assert hudcore.act_voice() == (True, "voice on")
        assert hudcore.ENABLED.exists()

    def test_turning_it_off_also_stops_the_line_in_progress(self, monkeypatch):
        ran = []
        monkeypatch.setattr(hudcore, "run", lambda *a, **k: ran.append(a))
        hudcore.ENABLED.touch()
        assert hudcore.act_voice() == (True, "voice off, silence")
        assert not hudcore.ENABLED.exists()
        assert ran == [("voice.py", "silence")]


class TestActFocus:
    """Giving the voice to one session, or taking it back."""

    def test_focusing_a_session_silences_the_others_mid_sentence(self, pane, dictate, monkeypatch):
        ran = []
        monkeypatch.setattr(hudcore, "run", lambda *a, **k: ran.append(a))
        pane(a_pane())
        assert hudcore.act_focus() == (True, "this session only")
        assert hudcore._focus.pane() == "%1"
        assert ran == [("voice.py", "silence")]

    def test_pressing_it_again_gives_every_session_its_voice_back(self, pane, dictate, monkeypatch):
        monkeypatch.setattr(hudcore, "run", lambda *a, **k: None)
        hudcore._focus.set_pane("%1", "one")
        assert hudcore.act_focus() == (True, "every session speaks")
        assert hudcore._focus.pane() == ""

    def test_a_refusal_says_why_nothing_could_be_focused(self, pane, dictate):
        pane({"ok": False, "why": "no Claude Code session"})
        assert hudcore.act_focus() == (False, "no Claude Code session")

    def test_a_target_with_no_pane_still_refuses_in_words(self, pane, dictate):
        pane({"ok": True})
        assert hudcore.act_focus() == (False, "no session to focus")


class TestActDictate:
    """Recording is refused; stopping never is."""

    def test_it_toggles_the_recorder_in_the_background(self, pane, monkeypatch):
        ran = []
        monkeypatch.setattr(hudcore, "run", lambda *a, **k: ran.append((a, k)))
        pane(a_pane())
        assert hudcore.act_dictate() == (True, "")
        assert ran == [(("dictate.py", "--toggle"), {"detach": True})]

    def test_with_nowhere_to_dictate_it_refuses_and_says_so(self, pane, monkeypatch):
        monkeypatch.setattr(hudcore, "run", lambda *a, **k: pytest.fail("recorded anyway"))
        pane({"ok": False, "why": "no window"})
        ok, msg = hudcore.act_dictate()
        assert ok is False and msg.startswith("no window")

    def test_a_recording_already_running_is_always_allowed_to_stop(self, pane, monkeypatch):
        ran = []
        monkeypatch.setattr(hudcore, "run", lambda *a, **k: ran.append(a))
        pane({"ok": False, "why": "no window"})
        (hudcore.BASE / "dictate.pid").write_text("1")
        assert hudcore.act_dictate() == (True, "")
        assert ran == [("dictate.py", "--toggle")]


class TestActConversation:
    """Continuous listening, verified rather than assumed."""

    @pytest.fixture(autouse=True)
    def _instant(self, monkeypatch):
        monkeypatch.setattr(hudcore.time, "sleep", lambda s: None)

    def test_stopping_is_never_refused(self, monkeypatch, pane):
        # A daemon left over from a session since closed still has to be
        # killable from whatever window is open now.
        monkeypatch.setattr(hudcore, "conversation_alive", lambda: True)
        stopped = []
        monkeypatch.setattr(hudcore, "conversation_stop", lambda: stopped.append(1))
        pane({"ok": False, "why": "no window"})
        assert hudcore.act_conversation() == (True, "conversation off")
        assert stopped == [1]

    def test_with_nowhere_to_deliver_it_refuses(self, monkeypatch, pane):
        monkeypatch.setattr(hudcore, "conversation_alive", lambda: False)
        monkeypatch.setattr(hudcore, "conversation_start", lambda: pytest.fail("started anyway"))
        pane({"ok": False, "why": "no window"})
        ok, msg = hudcore.act_conversation()
        assert ok is False and msg.startswith("no window")

    def test_a_daemon_that_comes_up_is_reported_on(self, monkeypatch, pane):
        alive = iter([False, False, True])
        monkeypatch.setattr(hudcore, "conversation_alive", lambda: next(alive))
        monkeypatch.setattr(hudcore, "conversation_start", lambda: None)
        pane(a_pane())
        assert hudcore.act_conversation() == (True, "conversation on")

    def test_a_daemon_that_dies_on_import_reports_its_last_words(self, monkeypatch, pane):
        # The button lighting while the daemon is already dead is the failure
        # this whole poll loop exists to make impossible.
        monkeypatch.setattr(hudcore, "conversation_alive", lambda: False)
        monkeypatch.setattr(hudcore, "conversation_start", lambda: None)
        monkeypatch.setattr(hudcore, "listen_failed", lambda: "ImportError: no torch")
        pane(a_pane())
        assert hudcore.act_conversation() == (False, "ImportError: no torch")

    def test_a_daemon_that_dies_silently_still_says_something(self, monkeypatch, pane):
        monkeypatch.setattr(hudcore, "conversation_alive", lambda: False)
        monkeypatch.setattr(hudcore, "conversation_start", lambda: None)
        monkeypatch.setattr(hudcore, "listen_failed", lambda: "")
        pane(a_pane())
        assert hudcore.act_conversation() == (False, "the listener would not start")


class TestActSessionNext:
    """Switching session, with the voice going along."""

    def test_it_advances_and_names_the_new_target(self, pane, dictate, monkeypatch):
        ran = []
        monkeypatch.setattr(hudcore, "run", lambda *a, **k: ran.append(a))
        pane(a_pane())
        assert hudcore.act_session_next() == (True, "one · a task")
        assert ran == [("dictate.py", "--next")]

    def test_the_agent_sweep_is_invalidated_at_once(self, pane, dictate, monkeypatch):
        # The first thing you look at after switching is whether the new
        # session has agents, and a two-second lag there reads as a lie.
        monkeypatch.setattr(hudcore, "run", lambda *a, **k: None)
        hudcore._agents_cache.update(t=time.time(), list=["stale"])
        pane(a_pane())
        hudcore.act_session_next()
        assert hudcore._agents_cache["t"] == 0.0

    def test_a_focus_follows_the_switch(self, pane, dictate, monkeypatch):
        ran = []
        monkeypatch.setattr(hudcore, "run", lambda *a, **k: ran.append(a))
        hudcore._focus.set_pane("%9", "an older window")
        pane(a_pane())
        hudcore.act_session_next()
        assert hudcore._focus.pane() == "%1"
        assert ("voice.py", "silence") in ran

    def test_with_no_session_to_move_to_it_says_so(self, pane, dictate, monkeypatch):
        monkeypatch.setattr(hudcore, "run", lambda *a, **k: None)
        hudcore._focus.set_pane("%9", "an older window")
        pane({"ok": False, "why": "no window"})
        assert hudcore.act_session_next() == (True, "no session")
        assert hudcore._focus.pane() == "%9"


class TestActLanguage:
    """The switch, and the one daemon that has to be restarted for it."""

    def test_a_switch_that_cannot_speak_is_refused_with_its_reason(self, monkeypatch):
        monkeypatch.setattr(hudcore._lang, "switch_next", lambda: (False, "no Espanol voice"))
        assert hudcore.act_language() == (False, "no Espanol voice")

    def test_a_successful_switch_relabels_the_window_in_place(self, monkeypatch, home):
        def _switch():
            (home / "config.toml").write_text('[hud]\ntitle = "otro"\n')
            return True, "switched to es"

        monkeypatch.setattr(hudcore._lang, "switch_next", _switch)
        monkeypatch.setattr(hudcore, "conversation_alive", lambda: False)
        assert hudcore.act_language() == (True, "switched to es")
        assert hudcore.TITLE == "O T R O"

    def test_conversation_mode_is_restarted_because_it_holds_its_language(self, monkeypatch):
        # Everything else re-reads the config per invocation; the listening
        # daemon read its Whisper language once, when it started.
        monkeypatch.setattr(hudcore._lang, "switch_next", lambda: (True, "switched to es"))
        monkeypatch.setattr(hudcore, "conversation_alive", lambda: True)
        order = []
        monkeypatch.setattr(hudcore, "conversation_stop", lambda: order.append("stop"))
        monkeypatch.setattr(hudcore, "conversation_start", lambda: order.append("start"))
        hudcore.act_language()
        assert order == ["stop", "start"]


class TestActSweep:
    def test_it_counts_what_it_closed(self, monkeypatch):
        monkeypatch.setattr(hudcore, "sweep_orphans", lambda: 2)
        assert hudcore.act_sweep() == (True, "closed 2")

    def test_nothing_to_close_is_still_an_answer(self, monkeypatch):
        monkeypatch.setattr(hudcore, "sweep_orphans", lambda: 0)
        assert hudcore.act_sweep() == (True, "nothing to close")


class TestAct:
    """The one entry point both the key handler and the browser go through."""

    def test_every_named_action_is_reachable(self):
        assert set(hudcore.ACTIONS) == {
            "voice",
            "focus",
            "dictate",
            "conversation",
            "session",
            "language",
            "sweep",
        }

    def test_a_name_nobody_defined_is_a_refusal_not_a_crash(self):
        assert hudcore.act("teleport") == (False, "unknown action: teleport")

    def test_an_action_that_raises_becomes_a_message(self, monkeypatch):
        monkeypatch.setitem(
            hudcore.ACTIONS, "sweep", lambda: (_ for _ in ()).throw(OSError("no permission"))
        )
        assert hudcore.act("sweep") == (False, "no permission")

    def test_an_exception_with_nothing_to_say_is_named_by_its_class(self, monkeypatch):
        monkeypatch.setitem(hudcore.ACTIONS, "sweep", lambda: (_ for _ in ()).throw(ValueError()))
        assert hudcore.act("sweep") == (False, "ValueError")

    def test_it_dispatches_to_the_named_function(self, monkeypatch):
        monkeypatch.setitem(hudcore.ACTIONS, "sweep", lambda: (True, "done"))
        assert hudcore.act("sweep") == (True, "done")
