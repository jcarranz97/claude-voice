"""Which terminal owns the voice, and what happens when the answer goes stale.

The focus is filed under a terminal rather than a session, so most of these
tests are about the two shapes a terminal handle takes -- a tmux pane id
(`%12`) and a controlling pty (`pts:/dev/pts/3`) -- and about the one case
where a stored focus has to be thrown away: a tmux server that restarted and
started issuing the same pane ids again.
"""

import json
import os
from pathlib import Path

import pytest

import claude_voice.focus as focus


@pytest.fixture(autouse=True)
def fresh_config(home):
    """Drop the cache of the config module *this* module imported.

    The package imports its siblings by bare name, so ``config`` and
    ``claude_voice.config`` are two module objects with two caches, and the
    shared ``home`` fixture only reloads the dotted one.
    """
    focus._config.load(reload=True)


@pytest.fixture(autouse=True)
def no_ambient_terminal(monkeypatch):
    """No `$TMUX`, no `$TMUX_PANE`, no `$CLAUDE_PID`.

    The suite is run from a terminal that has some of these, and a test that
    inherits them is a test that passes on one machine.
    """
    for var in ("TMUX", "TMUX_PANE", "CLAUDE_PID"):
        monkeypatch.delenv(var, raising=False)


def write_focus(**d) -> None:
    focus.FOCUS.write_text(json.dumps(d))


class TestRead:
    """`read` never raises and never returns anything but a dict."""

    def test_no_file_is_no_focus(self, home):
        assert focus.read() == {}

    def test_a_written_focus_reads_back(self, home):
        write_focus(pane="%3", label="repo - main")
        assert focus.read()["pane"] == "%3"

    def test_broken_json_is_no_focus(self, home):
        focus.FOCUS.write_text("{not json")
        assert focus.read() == {}

    def test_json_that_is_not_an_object_is_no_focus(self, home):
        # A file holding `[]` parses fine and would then answer `.get` with an
        # AttributeError in every caller.
        focus.FOCUS.write_text("[1, 2]")
        assert focus.read() == {}


class TestStale:
    """A pane id outlives its tmux server, and means something else after."""

    def test_a_pty_focus_is_never_stale(self):
        # No tmux server ever issued it, so no restart can invalidate it.
        assert focus._stale({"pane": "pts:/dev/pts/3", "tmux": "/tmp/x,999,0"}) is False

    def test_an_empty_focus_is_never_stale(self):
        assert focus._stale({}) is False

    def test_a_pane_from_a_dead_server_is_stale(self, monkeypatch):
        monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,4242,0")
        assert focus._stale({"pane": "%1", "tmux": "/tmp/tmux-1000/default,17,0"}) is True

    def test_the_same_server_seen_from_another_client_is_not(self, monkeypatch):
        # The trailing session number differs per client and is not part of
        # the server's identity, which is why _server drops it.
        monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,17,4")
        assert focus._stale({"pane": "%1", "tmux": "/tmp/tmux-1000/default,17,0"}) is False

    def test_a_reader_outside_tmux_cannot_tell(self):
        # Guessing here would drop a live focus every time a hook that runs
        # outside tmux asked about one set inside it.
        assert focus._stale({"pane": "%1", "tmux": "/tmp/tmux-1000/default,17,0"}) is False

    def test_a_focus_with_no_server_recorded_is_kept(self, monkeypatch):
        monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,17,0")
        assert focus._stale({"pane": "%1", "tmux": ""}) is False


class TestPaneAndLabel:
    """What the rest of the package reads off the file."""

    def test_no_focus_is_the_empty_string(self, home):
        assert focus.pane() == ""
        assert focus.label() == ""

    def test_the_focused_pane_comes_back(self, home):
        write_focus(pane="%12", label="claude-voice - main")
        assert focus.pane() == "%12"
        assert focus.label() == "claude-voice - main"

    def test_a_stale_focus_reads_as_none_at_all(self, home, monkeypatch):
        monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,4242,0")
        write_focus(pane="%12", tmux="/tmp/tmux-1000/default,17,0")
        assert focus.pane() == ""

    def test_a_focus_with_no_label_is_the_empty_string(self, home):
        write_focus(pane="%12")
        assert focus.label() == ""


class TestHere:
    """The terminal this process is in, found without a controlling tty."""

    def test_the_claude_pid_gives_the_pty(self, monkeypatch):
        # A hook has no tty of its own; the claude process it is a child of
        # does, and /proc is the only way to read it back.
        monkeypatch.setenv("CLAUDE_PID", "1234")
        monkeypatch.setattr(focus.os, "readlink", lambda p: "/dev/pts/7")
        assert focus.here() == "pts:/dev/pts/7"

    def test_a_claude_pid_that_is_not_a_number_falls_through(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_PID", "not-a-pid")
        monkeypatch.setenv("TMUX_PANE", "%9")
        monkeypatch.setattr(focus.os, "ttyname", _no_tty)
        assert focus.here() == "%9"

    def test_a_dead_claude_pid_falls_through(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_PID", "1234")
        monkeypatch.setattr(focus.os, "readlink", _no_proc)
        monkeypatch.setattr(focus.os, "ttyname", lambda fd: "/dev/pts/2")
        assert focus.here() == "pts:/dev/pts/2"

    def test_run_straight_from_the_session_uses_its_own_tty(self, monkeypatch):
        monkeypatch.setattr(focus.os, "ttyname", lambda fd: "/dev/pts/2")
        assert focus.here() == "pts:/dev/pts/2"

    def test_the_pane_id_is_the_last_resort(self, monkeypatch):
        # Load-bearing ordering: a session inside tmux has both, and the pty
        # is the handle dictation knows it by.
        monkeypatch.setenv("TMUX_PANE", "%4")
        monkeypatch.setattr(focus.os, "ttyname", _no_tty)
        assert focus.here() == "%4"

    def test_nothing_knowable_is_the_empty_string(self, monkeypatch):
        monkeypatch.setattr(focus.os, "ttyname", _no_tty)
        assert focus.here() == ""


def _no_tty(fd):
    raise OSError("not a tty")


def _no_proc(path):
    raise OSError("no such process")


class TestSetAndClear:
    """The focus file is written and renamed, never written in place."""

    def test_setting_a_pane_records_the_server_too(self, home, monkeypatch):
        monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,17,0")
        assert focus.set_pane("%5", "repo - main") is True
        d = json.loads(focus.FOCUS.read_text())
        assert d["pane"] == "%5"
        assert d["label"] == "repo - main"
        assert d["tmux"] == "/tmp/tmux-1000/default,17,0"
        assert isinstance(d["ts"], float)

    def test_no_temporary_file_is_left_behind(self, home):
        focus.set_pane("pts:/dev/pts/3")
        assert list(home.glob("*.tmp")) == []

    def test_an_empty_pane_is_refused(self, home):
        assert focus.set_pane("") is False
        assert not focus.FOCUS.exists()

    def test_a_state_dir_that_cannot_be_made_is_refused(self, home, monkeypatch):
        blocked = home / "not-a-dir"
        blocked.write_text("")
        monkeypatch.setattr(focus, "BASE", blocked)
        monkeypatch.setattr(focus, "FOCUS", blocked / "focus.json")
        assert focus.set_pane("%5") is False

    def test_clearing_removes_the_file(self, home):
        focus.set_pane("%5")
        focus.clear()
        assert focus.pane() == ""

    def test_clearing_twice_is_harmless(self, home):
        focus.clear()
        focus.clear()

    def test_a_focus_file_that_cannot_be_removed_is_survived(self, home, monkeypatch):
        focus.FOCUS.mkdir()
        focus.clear()


class TestBoundSession:
    """The pane -> session binding thinking.py writes, read back by path."""

    def test_a_bound_pane_answers_with_its_session(self, home):
        (home / "pane-12.json").write_text(json.dumps({"session": "abc", "cwd": ""}))
        assert focus._bound_session("%12") == "abc"

    def test_an_unbound_pane_answers_with_nothing(self, home):
        assert focus._bound_session("%12") == ""

    def test_a_thinking_module_that_will_not_load_answers_with_nothing(self, monkeypatch):
        monkeypatch.setattr(focus, "HERE", Path("/nonexistent-claude-voice-dir"))
        assert focus._bound_session("%12") == ""


class TestAllows:
    """May this session make a sound?"""

    def test_no_focus_lets_everything_speak(self, home):
        assert focus.allows("any-session") is True

    def test_the_process_in_the_focused_terminal_speaks(self, home, monkeypatch):
        monkeypatch.setenv("TMUX_PANE", "%7")
        monkeypatch.setattr(focus.os, "ttyname", _no_tty)
        focus.set_pane("%7")
        assert focus.allows() is True

    def test_a_session_bound_to_the_focused_pane_speaks(self, home, monkeypatch):
        # Nothing of ours is running in that pane right now -- this is the
        # path for anything speaking on a session's behalf from outside it.
        monkeypatch.setattr(focus.os, "ttyname", _no_tty)
        focus.set_pane("%12")
        (home / "pane-12.json").write_text(json.dumps({"session": "abc", "cwd": ""}))
        assert focus.allows("abc") is True

    def test_another_session_stays_quiet(self, home, monkeypatch):
        monkeypatch.setattr(focus.os, "ttyname", _no_tty)
        focus.set_pane("%12")
        (home / "pane-12.json").write_text(json.dumps({"session": "abc", "cwd": ""}))
        assert focus.allows("other") is False

    def test_a_focus_on_a_closed_window_is_silence(self, home, monkeypatch):
        # The honest reading of "only that pane talks": nothing runs there and
        # nothing is bound, so nothing speaks.
        monkeypatch.setattr(focus.os, "ttyname", _no_tty)
        focus.set_pane("%12")
        assert focus.allows("abc") is False
        assert focus.allows() is False


class TestDescribe:
    """The one line `status` prints."""

    def test_no_focus_says_every_session_speaks(self, home):
        assert focus.describe() == "none (every session speaks)"

    def test_the_focused_session_says_this_session(self, home, monkeypatch):
        monkeypatch.setenv("TMUX_PANE", "%7")
        monkeypatch.setattr(focus.os, "ttyname", _no_tty)
        focus.set_pane("%7", "claude-voice - main")
        assert focus.describe() == "this session (claude-voice - main)"

    def test_another_pane_says_so_and_names_it(self, home, monkeypatch):
        monkeypatch.setattr(focus.os, "ttyname", _no_tty)
        focus.set_pane("%7", "other - main")
        assert focus.describe("mine") == "another pane (other - main)"

    def test_an_unlabelled_focus_falls_back_to_the_pane_id(self, home, monkeypatch):
        monkeypatch.setattr(focus.os, "ttyname", _no_tty)
        focus.set_pane("%7")
        assert focus.describe("mine") == "another pane (%7)"


class TestFilePaths:
    """The focus lives with the durable state, not with the mute markers."""

    def test_the_focus_file_sits_under_the_state_dir(self, home):
        assert focus.FOCUS.parent == focus.BASE
        assert focus.FOCUS.name == "focus.json"
        assert str(focus.BASE) == os.environ["CLAUDE_VOICE_HOME"]
