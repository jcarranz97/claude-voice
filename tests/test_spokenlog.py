"""The transcript of what was actually said, in both directions.

History is a bonus, never a dependency: nearly every test here is about the log
degrading rather than raising, because the writer runs inside a hook and the
reader runs inside the HUD's redraw loop.
"""

import json
import os
import time
from pathlib import Path

import pytest

import claude_voice.config as config
import claude_voice.spokenlog as spokenlog


class _NoBase:
    """A BASE whose glob refuses, for the unreadable state directory."""

    def glob(self, pattern):
        raise OSError("no such directory")

    def __truediv__(self, other):
        return Path("/nonexistent") / other


@pytest.fixture(autouse=True)
def fresh_cache():
    """`newest_session` caches its answer for two seconds, module-wide.

    The cache is the whole reason the HUD can ask twenty times a second, and it
    would otherwise leak one test's answer into the next.
    """
    spokenlog._newest.update(t=0.0, key=None, sid="")
    yield
    spokenlog._newest.update(t=0.0, key=None, sid="")


@pytest.fixture
def with_cfg(monkeypatch):
    """Swap the configuration the module captured at import time.

    `CFG` is read once when the module loads -- a hook process lives for a
    fraction of a second -- so reloading the config file underneath it changes
    nothing, and a test that wants another value has to hand it over.
    """

    def _set(**values):
        cfg = config.Config({"history": values})
        monkeypatch.setattr(spokenlog, "CFG", cfg)
        return cfg

    return _set


def _age(path: Path, seconds: float) -> None:
    old = time.time() - seconds
    os.utime(path, (old, old))


def _lines(path: Path) -> list:
    return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]


class TestLimits:
    """The two numbers, and what they do with a config file full of nonsense."""

    def test_the_cap_is_per_session(self, with_cfg):
        with_cfg(cap=120)
        assert spokenlog.cap() == 120

    def test_the_cap_has_a_floor(self, with_cfg):
        # One window talking all afternoon must not be able to shrink the log
        # to nothing for every other window.
        with_cfg(cap=1)
        assert spokenlog.cap() == 20

    def test_an_unreadable_cap_falls_back(self, with_cfg):
        with_cfg(cap="lots")
        assert spokenlog.cap() == 400

    def test_keep_days_becomes_seconds(self, with_cfg):
        with_cfg(keep_days=2)
        assert spokenlog.keep_secs() == 2 * 86400

    def test_keep_days_never_drops_below_a_day(self, with_cfg):
        with_cfg(keep_days=0)
        assert spokenlog.keep_secs() == 86400

    def test_an_unreadable_keep_days_falls_back(self, with_cfg):
        with_cfg(keep_days="a while")
        assert spokenlog.keep_secs() == spokenlog.STALE_DAYS * 86400


class TestPath:
    """One file per conversation, named the way turn state is."""

    def test_it_is_keyed_by_session(self, home):
        assert spokenlog.path("s1") == home / "spoken-s1.jsonl"

    def test_no_session_lands_in_the_default_log(self, home):
        assert spokenlog.path("") == home / "spoken-default.jsonl"

    def test_the_legacy_log_is_not_one_of_ours(self, home):
        # Written before history was keyed by session, so it carries no session
        # to be split by and is read-only from here on.
        assert spokenlog.LEGACY.name == "spoken.jsonl"
        assert spokenlog.LEGACY.parent == home


class TestRecord:
    """Appending, which happens inside a hook and must not slow it down."""

    def test_it_appends_one_line_per_spoken_thing(self, home):
        spokenlog.record("out", "The tests pass.", "s1")
        spokenlog.record("in", "run them again", "s1")
        entries = _lines(home / "spoken-s1.jsonl")
        assert [e["side"] for e in entries] == ["out", "in"]
        assert [e["text"] for e in entries] == ["The tests pass.", "run them again"]
        assert entries[0]["session"] == "s1"

    def test_anything_that_is_not_in_is_out(self, home):
        # Only two sides exist; an unknown one is a line that was heard.
        spokenlog.record("sideways", "said aloud", "s1")
        assert _lines(home / "spoken-s1.jsonl")[0]["side"] == "out"

    def test_empty_text_is_not_a_line(self, home):
        spokenlog.record("out", "   ", "s1")
        spokenlog.record("out", None, "s1")
        assert not (home / "spoken-s1.jsonl").exists()

    def test_text_is_stripped(self, home):
        spokenlog.record("out", "  padded  ", "s1")
        assert _lines(home / "spoken-s1.jsonl")[0]["text"] == "padded"

    def test_a_line_nobody_can_attribute_still_lands(self, home):
        # The CLI speaks with no session to name; the line was still said.
        spokenlog.record("out", "spoken from the CLI")
        assert (home / "spoken-default.jsonl").exists()

    def test_disabled_history_writes_nothing(self, home, with_cfg):
        with_cfg(enabled=False)
        spokenlog.record("out", "The tests pass.", "s1")
        assert not (home / "spoken-s1.jsonl").exists()

    def test_the_first_line_of_a_session_sweeps(self, home, monkeypatch):
        # Once per session rather than once per line: the sweep is the only
        # thing here that touches other files.
        swept = []
        monkeypatch.setattr(spokenlog, "sweep", lambda *a, **kw: swept.append(True))
        spokenlog.record("out", "first", "s1")
        spokenlog.record("out", "second", "s1")
        assert len(swept) == 1

    def test_a_log_grown_past_twice_its_cap_is_trimmed(self, home, with_cfg):
        with_cfg(cap=20)
        for i in range(60):
            spokenlog.record("out", f"line {i} " + "x" * 300, "s1")
        kept = _lines(home / "spoken-s1.jsonl")
        assert len(kept) <= 40  # trimmed back to the cap, then growing again
        assert kept[-1]["text"].startswith("line 59")

    def test_a_broken_state_directory_is_silent(self, home, monkeypatch):
        blocked = home / "not-a-directory"
        blocked.write_text("in the way")
        monkeypatch.setattr(spokenlog, "BASE", blocked)
        spokenlog.record("out", "The tests pass.", "s1")
        assert not list(home.glob("spoken-*.jsonl"))


class TestTrim:
    """Keeping the last `cap` entries without a reader ever seeing half a log."""

    def test_it_keeps_the_tail(self, home, with_cfg):
        with_cfg(cap=20)
        log = home / "spoken-s1.jsonl"
        log.write_text("".join(f'{{"t": 0, "text": "{i}"}}\n' for i in range(100)))
        spokenlog._trim(log)
        kept = _lines(log)
        assert len(kept) == 20
        assert kept[0]["text"] == "80"

    def test_it_leaves_no_temporary_file_behind(self, home, with_cfg):
        with_cfg(cap=20)
        log = home / "spoken-s1.jsonl"
        log.write_text("".join(f'{{"t": 0, "text": "{i}"}}\n' for i in range(50)))
        spokenlog._trim(log)
        assert not list(home.glob("*.tmp"))

    def test_trimming_a_log_that_is_not_there_is_silent(self, home):
        spokenlog._trim(home / "gone.jsonl")
        assert not (home / "gone.jsonl").exists()


class TestFilesAndSessions:
    """Which conversations have said anything."""

    def test_only_per_session_logs_are_listed(self, home):
        spokenlog.record("out", "hello", "s1")
        spokenlog.LEGACY.write_text('{"t": 1, "text": "old"}\n')
        (home / "turn-s1.json").write_text("{}")
        assert [p.name for p in spokenlog.files()] == ["spoken-s1.jsonl"]

    def test_sessions_drop_the_prefix_and_the_suffix(self):
        spokenlog.record("out", "hello", "aaa")
        spokenlog.record("out", "hello", "bbb")
        assert sorted(spokenlog.sessions()) == ["aaa", "bbb"]

    def test_an_unreadable_directory_lists_nothing(self, monkeypatch):
        monkeypatch.setattr(spokenlog, "BASE", _NoBase())
        assert spokenlog.files() == []


class TestNewestSession:
    """The guess a reader makes when it cannot name the session itself."""

    def test_nothing_logged_is_no_session(self):
        assert spokenlog.newest_session() == ""

    def test_the_most_recently_written_log_wins(self, home):
        spokenlog.record("out", "older", "old")
        _age(home / "spoken-old.jsonl", 600)
        spokenlog.record("out", "newer", "new")
        assert spokenlog.newest_session() == "new"

    def test_candidates_narrow_the_guess(self, home):
        # A guess has to stay inside the question: a pane that cannot be named
        # is still certainly one of the conversations of its own project.
        spokenlog.record("out", "elsewhere", "other")
        _age(home / "spoken-other.jsonl", -600)  # the liveliest on the machine
        spokenlog.record("out", "here", "mine")
        assert spokenlog.newest_session(["mine"]) == "mine"

    def test_candidates_are_sanitised_like_filenames(self, home):
        spokenlog.record("out", "here", "a/b")
        assert spokenlog.newest_session(["a/b"]) == "ab"

    def test_no_candidate_has_spoken_is_no_session(self):
        spokenlog.record("out", "elsewhere", "other")
        assert spokenlog.newest_session(["absent"]) == ""

    def test_the_answer_is_cached_for_a_moment(self, home):
        # The HUD asks twenty times a second; without this the panel would
        # stat every log in the directory on every redraw.
        spokenlog.record("out", "first", "one")
        assert spokenlog.newest_session() == "one"
        spokenlog.record("out", "second", "two")
        assert spokenlog.newest_session() == "one"

    def test_a_different_question_is_not_answered_from_the_cache(self):
        spokenlog.record("out", "first", "one")
        assert spokenlog.newest_session() == "one"
        assert spokenlog.newest_session(["one"]) == "one"

    def test_a_log_that_cannot_be_stat_ed_is_skipped(self, home):
        spokenlog.record("out", "real", "real")
        os.symlink(home / "gone.jsonl", home / "spoken-dangling.jsonl")
        assert spokenlog.newest_session() == "real"


class TestTail:
    """Reading one conversation back, oldest first."""

    def test_it_returns_what_was_said_in_order(self):
        spokenlog.record("out", "The tests pass.", "s1")
        spokenlog.record("in", "run them again", "s1")
        assert [e["text"] for e in spokenlog.tail(10, "s1")] == [
            "The tests pass.",
            "run them again",
        ]

    def test_it_returns_at_most_n(self):
        for i in range(10):
            spokenlog.record("out", f"line {i}", "s1")
        got = spokenlog.tail(3, "s1")
        assert [e["text"] for e in got] == ["line 7", "line 8", "line 9"]

    def test_asking_for_none_still_returns_the_last_one(self):
        spokenlog.record("out", "only line", "s1")
        assert len(spokenlog.tail(0, "s1")) == 1

    def test_no_session_reads_nothing(self):
        # Falling back in here is what made the panel show the busiest window
        # on the machine instead of the one being watched.
        spokenlog.record("out", "somebody spoke", "s1")
        assert spokenlog.tail(10, "") == []

    def test_a_session_that_never_spoke_reads_nothing(self):
        assert spokenlog.tail(10, "never") == []

    def test_an_empty_log_reads_nothing(self, home):
        (home / "spoken-s1.jsonl").write_text("")
        assert spokenlog.tail(10, "s1") == []

    def test_one_corrupt_line_does_not_lose_the_rest(self, home):
        (home / "spoken-s1.jsonl").write_text(
            '{"t": 1, "side": "out", "text": "before"}\n'
            "{ this line is not json\n"
            '{"t": 3, "side": "out", "text": "after"}\n'
        )
        assert [e["text"] for e in spokenlog.tail(10, "s1")] == ["before", "after"]

    def test_a_line_with_no_text_is_not_an_entry(self, home):
        (home / "spoken-s1.jsonl").write_text(
            '{"t": 1, "text": ""}\n{"t": 2}\n{"t": 3, "text": "kept"}\n'
        )
        assert [e["text"] for e in spokenlog.tail(10, "s1")] == ["kept"]

    def test_missing_fields_get_defaults(self, home):
        (home / "spoken-s1.jsonl").write_text('{"text": "bare"}\n')
        e = spokenlog.tail(10, "s1")[0]
        assert e == {"t": 0.0, "side": "out", "session": "", "text": "bare"}


class TestTailAll:
    """Every session on the machine, which is the one read allowed to interleave."""

    def test_it_merges_sessions_by_the_clock(self, home):
        (home / "spoken-a.jsonl").write_text(
            '{"t": 1, "side": "out", "text": "first"}\n{"t": 3, "side": "out", "text": "third"}\n'
        )
        (home / "spoken-b.jsonl").write_text('{"t": 2, "side": "in", "text": "second"}\n')
        assert [e["text"] for e in spokenlog.tail_all(10)] == ["first", "second", "third"]

    def test_it_is_the_only_way_to_read_the_pre_split_log(self, home):
        spokenlog.LEGACY.write_text('{"t": 1, "side": "out", "text": "from before the split"}\n')
        assert [e["text"] for e in spokenlog.tail_all(10)] == ["from before the split"]

    def test_it_returns_at_most_n_across_all_of_them(self, home):
        (home / "spoken-a.jsonl").write_text(
            "".join(f'{{"t": {i}, "text": "a{i}"}}\n' for i in range(5))
        )
        (home / "spoken-b.jsonl").write_text(
            "".join(f'{{"t": {i + 0.5}, "text": "b{i}"}}\n' for i in range(5))
        )
        got = spokenlog.tail_all(3)
        assert [e["text"] for e in got] == ["b3", "a4", "b4"]

    def test_nothing_logged_is_nothing_to_read(self):
        assert spokenlog.tail_all(10) == []


class TestMtime:
    """The stamp a reader caches against."""

    def test_it_moves_when_the_log_does(self):
        spokenlog.record("out", "hello", "s1")
        assert spokenlog.mtime("s1") > 0

    def test_no_session_has_no_stamp(self):
        assert spokenlog.mtime("") == 0.0

    def test_a_log_that_is_not_there_has_no_stamp(self):
        assert spokenlog.mtime("never") == 0.0

    def test_a_permission_change_counts_as_a_change(self, home):
        # ctime, not just mtime: a log that becomes readable again is new to a
        # reader even though its contents are not.
        spokenlog.record("out", "hello", "s1")
        log = home / "spoken-s1.jsonl"
        _age(log, 600)
        log.chmod(0o600)
        assert spokenlog.mtime("s1") > log.stat().st_mtime


class TestSweep:
    """Logs of sessions that stopped talking days ago."""

    def test_it_drops_the_old_and_keeps_the_recent(self, home):
        spokenlog.record("out", "old", "old")
        spokenlog.record("out", "new", "new")
        _age(home / "spoken-old.jsonl", spokenlog.STALE_DAYS * 86400 + 60)
        spokenlog.sweep()
        assert not (home / "spoken-old.jsonl").exists()
        assert (home / "spoken-new.jsonl").exists()

    def test_it_never_touches_the_pre_split_log(self, home):
        # --all is the only thing that can still show it, so nothing is allowed
        # to age it out.
        spokenlog.LEGACY.write_text('{"t": 1, "text": "old"}\n')
        _age(spokenlog.LEGACY, 400 * 86400)
        spokenlog.sweep(max_age=1.0)
        assert spokenlog.LEGACY.exists()

    def test_a_log_that_vanishes_mid_sweep_is_not_fatal(self, home):
        os.symlink(home / "gone.jsonl", home / "spoken-dangling.jsonl")
        spokenlog.sweep(max_age=1.0)
        assert True  # the point is that stat() on a broken link did not raise


class TestSiblingModules:
    """The readers' paths, loaded late so a hook does not pay for them."""

    def test_a_sibling_loads_by_file(self):
        m = spokenlog._mod("turn")
        assert m.safe_session("a/b") == "ab"


class TestFollow:
    """The one policy for whose history a reader is looking at."""

    def test_a_named_session_is_the_answer(self):
        assert spokenlog.follow("s1", "/some/project") == "s1"

    def test_an_unnamed_pane_stays_inside_its_own_project(self, monkeypatch):
        # Without this step a session that cannot be named showed whichever
        # window had spoken last -- the same panel in every session.
        spokenlog.record("out", "elsewhere", "other")
        spokenlog.record("out", "here", "mine")

        class _Thinking:
            @staticmethod
            def sessions_in(cwd):
                return ["mine"]

        monkeypatch.setattr(spokenlog, "_mod", lambda name: _Thinking)
        assert spokenlog.follow("", "/some/project") == "mine"

    def test_a_project_that_has_said_nothing_gets_nothing(self, monkeypatch):
        spokenlog.record("out", "elsewhere", "other")

        class _Thinking:
            @staticmethod
            def sessions_in(cwd):
                return []

        monkeypatch.setattr(spokenlog, "_mod", lambda name: _Thinking)
        assert spokenlog.follow("", "/some/project") == ""

    def test_a_reader_that_cannot_ask_gets_nothing(self, monkeypatch):
        def _explode(name):
            raise ImportError("no thinking module here")

        monkeypatch.setattr(spokenlog, "_mod", _explode)
        assert spokenlog.follow("", "/some/project") == ""

    def test_outside_tmux_it_is_the_liveliest_on_the_box(self):
        spokenlog.record("out", "hello", "only")
        assert spokenlog.follow("", "") == "only"


class TestTarget:
    """Where dictation would land, for the CLI."""

    def test_it_reports_the_session_and_the_pane_directory(self, monkeypatch):
        class _Dictate:
            @staticmethod
            def claude_panes():
                return [{"id": "%1", "path": "/elsewhere"}, {"id": "%2", "path": "/project"}]

            @staticmethod
            def cfg():
                return {"pane": "%2"}

            @staticmethod
            def target_session():
                return "s1"

        monkeypatch.setattr(spokenlog, "_mod", lambda name: _Dictate)
        assert spokenlog.target() == ("s1", "/project")

    def test_no_pane_bound_still_answers(self, monkeypatch):
        class _Dictate:
            @staticmethod
            def claude_panes():
                return []

            @staticmethod
            def cfg():
                return {}

            @staticmethod
            def target_session():
                return ""

        monkeypatch.setattr(spokenlog, "_mod", lambda name: _Dictate)
        assert spokenlog.target() == ("", "")

    def test_a_broken_dictation_module_is_not_fatal(self, monkeypatch):
        def _explode(name):
            raise ImportError("no dictate module here")

        monkeypatch.setattr(spokenlog, "_mod", _explode)
        assert spokenlog.target() == ("", "")


class TestMain:
    """`spokenlog.py`, the command."""

    @pytest.fixture(autouse=True)
    def no_tmux(self, monkeypatch):
        """The command asks tmux where dictation goes; a test machine has none."""
        monkeypatch.setattr(spokenlog, "target", lambda: ("", ""))

    def _run(self, monkeypatch, *args):
        monkeypatch.setattr("sys.argv", ["spokenlog.py", *args])
        return spokenlog.main()

    def test_it_prints_both_sides_of_the_conversation(self, monkeypatch, capsys):
        spokenlog.record("in", "run the tests", "s1")
        spokenlog.record("out", "The tests pass.", "s1")
        assert self._run(monkeypatch, "--session", "s1") == 0
        out = capsys.readouterr().out
        assert "run the tests" in out and "The tests pass." in out
        assert "you" in out and "said" in out

    def test_nothing_spoken_says_which_session_it_looked_at(self, monkeypatch, capsys):
        assert self._run(monkeypatch, "--session", "abcdef0123") == 0
        assert "nothing spoken yet (session abcdef01)" in capsys.readouterr().out

    def test_no_session_at_all_says_so(self, monkeypatch, capsys):
        assert self._run(monkeypatch) == 0
        assert "no session to read" in capsys.readouterr().out

    def test_it_follows_the_pane_when_no_session_is_named(self, monkeypatch, capsys):
        spokenlog.record("out", "from the pane", "paned")
        monkeypatch.setattr(spokenlog, "target", lambda: ("paned", ""))
        assert self._run(monkeypatch) == 0
        assert "from the pane" in capsys.readouterr().out

    def test_session_without_a_value_is_no_session(self, monkeypatch, capsys):
        assert self._run(monkeypatch, "--session") == 0
        assert "no session to read" in capsys.readouterr().out

    def test_all_names_whose_line_each_one_is(self, monkeypatch, capsys):
        # Only --all can show two conversations at once, so only --all has to
        # say who is talking.
        spokenlog.record("out", "from one", "sessionone")
        spokenlog.record("out", "from two", "sessiontwo")
        assert self._run(monkeypatch, "--all") == 0
        out = capsys.readouterr().out
        assert "sessiono" in out and "sessiont" in out

    def test_all_shows_a_dash_for_a_line_with_no_session(self, home, monkeypatch, capsys):
        spokenlog.LEGACY.write_text('{"t": 0, "side": "out", "text": "from before"}\n')
        assert self._run(monkeypatch, "--all") == 0
        out = capsys.readouterr().out
        assert "-  said" in out
        assert "     " in out  # no timestamp on a line that never had one

    def test_a_count_limits_the_output(self, monkeypatch, capsys):
        for i in range(5):
            spokenlog.record("out", f"line {i}", "s1")
        assert self._run(monkeypatch, "2", "--session", "s1") == 0
        out = capsys.readouterr().out
        assert "line 3" in out and "line 4" in out and "line 2" not in out

    def test_a_count_that_is_not_a_number_falls_back(self, monkeypatch, capsys):
        for i in range(50):
            spokenlog.record("out", f"line {i}", "s1")
        assert self._run(monkeypatch, "banana", "--session", "s1") == 0
        out = capsys.readouterr().out
        assert "line 49" in out and "line 9" not in out  # the last 40
