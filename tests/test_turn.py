"""One file per session, and what happens when one of them is unreadable."""

import json
import os
import time
from pathlib import Path

import claude_voice.turn as turn


class _NoBase:
    """A BASE whose glob refuses. Stands in for a state directory that has been
    removed or made unreadable underneath a running HUD."""

    def glob(self, pattern):
        raise OSError("no such directory")

    def __truediv__(self, other):
        return Path("/nonexistent") / other


def _age(path: Path, seconds: float) -> None:
    old = time.time() - seconds
    os.utime(path, (old, old))


class TestSafeSession:
    """`safe_session` turns anything into one filename, and always the same one."""

    def test_a_uuid_passes_through_unchanged(self):
        sid = "0f9c1e2a-3b4d-5e6f-7a8b-9c0d1e2f3a4b"
        assert turn.safe_session(sid) == sid

    def test_path_separators_are_stripped(self):
        # The session id reaches this from a hook payload, so it is untrusted
        # input on its way to being a filename.
        assert turn.safe_session("../../etc/passwd") == "....etcpasswd"

    def test_it_truncates_to_sixty_four_characters(self):
        assert len(turn.safe_session("a" * 200)) == 64

    def test_nothing_left_becomes_default(self):
        for empty in ("", None, "///", "!!!"):
            assert turn.safe_session(empty) == "default"

    def test_the_spoken_log_keys_files_the_same_way(self):
        # Two names for one session would mean two files for it, so this is
        # the shared spelling rather than a private detail of turn state.
        import claude_voice.spokenlog as spokenlog

        assert spokenlog.path("a/b").stem.endswith(turn.safe_session("a/b"))


class TestPath:
    """Where one session's state lives."""

    def test_it_is_named_for_the_session(self, home):
        assert turn.path("abc") == home / "turn-abc.json"

    def test_no_session_is_still_a_file(self, home):
        assert turn.path("") == home / "turn-default.json"


class TestWriteAndRead:
    """The round trip a hook makes: publish a state, someone reads it back."""

    def test_a_written_state_reads_back(self):
        turn.write("s1", "thinking", "checking the tests")
        d = turn.read("s1")
        assert d["state"] == "thinking"
        assert d["text"] == "checking the tests"
        assert d["session"] == "s1"
        assert d["ts"] > 0

    def test_seconds_become_a_deadline_and_zero_stays_zero(self):
        turn.write("s1", "speaking", secs=5.0)
        assert turn.read("s1")["until"] > time.time()
        turn.write("s2", "thinking")
        assert turn.read("s2")["until"] == 0

    def test_a_session_that_never_wrote_is_idle(self):
        assert turn.read("nobody") == turn.IDLE

    def test_the_idle_answer_is_a_copy(self):
        # Callers pass this dict around; handing out the module constant would
        # let one reader edit every future answer.
        d = turn.read("nobody")
        d["state"] = "vandalised"
        assert turn.IDLE["state"] == "idle"

    def test_a_corrupt_file_reads_as_idle(self, home):
        (home / "turn-s1.json").write_text("{not json")
        assert turn.read("s1")["state"] == "idle"

    def test_a_state_without_a_session_field_gets_the_one_asked_for(self, home):
        # Files written before the state was keyed by session have no session
        # in them; the reader knows which one it opened.
        (home / "turn-old.json").write_text(json.dumps({"state": "thinking"}))
        assert turn.read("old")["session"] == "old"

    def test_writing_where_it_cannot_write_is_silent(self, home, monkeypatch):
        # A hook that raises breaks the editor it runs in, so a broken state
        # directory has to cost nothing.
        blocked = home / "not-a-directory"
        blocked.write_text("in the way")
        monkeypatch.setattr(turn, "BASE", blocked)
        turn.write("s1", "thinking")
        assert not list(home.glob("turn-*.json"))


class TestNewest:
    """The fallback for a reader that cannot say which session it is watching."""

    def test_nothing_written_is_idle(self):
        assert turn.newest()["state"] == "idle"

    def test_the_highest_timestamp_wins(self, home):
        (home / "turn-old.json").write_text(json.dumps({"state": "done", "ts": 10}))
        (home / "turn-new.json").write_text(json.dumps({"state": "thinking", "ts": 20}))
        assert turn.newest()["state"] == "thinking"

    def test_a_corrupt_file_is_skipped_rather_than_fatal(self, home):
        (home / "turn-bad.json").write_text("}{")
        (home / "turn-good.json").write_text(json.dumps({"state": "done", "ts": 5}))
        assert turn.newest()["state"] == "done"


class TestFilesAndSessions:
    """Enumerating what has reported anything."""

    def test_it_lists_only_turn_files(self, home):
        turn.write("s1", "thinking")
        (home / "spoken-s1.jsonl").write_text("{}\n")
        (home / "enabled").write_text("")
        assert [p.name for p in turn.files()] == ["turn-s1.json"]

    def test_sessions_are_the_ids_without_the_prefix(self):
        turn.write("aaa", "thinking")
        turn.write("bbb", "done")
        assert sorted(turn.sessions()) == ["aaa", "bbb"]

    def test_an_unreadable_state_directory_lists_nothing(self, monkeypatch):
        monkeypatch.setattr(turn, "BASE", _NoBase())
        assert turn.files() == []


class TestSweep:
    """A session that died mid-thought leaves a file nobody will clear."""

    def test_old_files_go_and_fresh_ones_stay(self, home):
        turn.write("stale", "thinking")
        turn.write("live", "thinking")
        _age(home / "turn-stale.json", turn.STALE + 60)
        turn.sweep()
        assert not (home / "turn-stale.json").exists()
        assert (home / "turn-live.json").exists()

    def test_a_write_sweeps_on_its_way_past(self, home):
        # Sweeping on write costs nothing anybody is waiting on, which is why
        # there is no separate cleaner process.
        (home / "turn-stale.json").write_text(json.dumps({"state": "thinking", "ts": 1}))
        _age(home / "turn-stale.json", turn.STALE + 60)
        turn.write("live", "thinking")
        assert not (home / "turn-stale.json").exists()

    def test_a_file_that_vanishes_mid_sweep_is_not_fatal(self, home):
        os.symlink(home / "gone.json", home / "turn-dangling.json")
        turn.sweep(max_age=0.0)
        assert True  # the point is that stat() on a broken link did not raise


class TestPidfiles:
    """The heartbeat and the acknowledgement, one pidfile each per session."""

    def test_a_session_keys_the_name(self, home):
        assert turn.pidfile("thinking", "s1") == home / "thinking-s1.pid"

    def test_no_session_keeps_the_legacy_global_name(self, home):
        # The CLI has no session id, and orphan sweeping still has to find it.
        assert turn.pidfile("ack", "") == home / "ack.pid"

    def test_listing_includes_the_legacy_name(self, home):
        (home / "thinking-s1.pid").write_text("1")
        (home / "thinking.pid").write_text("2")
        (home / "ack-s1.pid").write_text("3")
        names = [p.name for p in turn.pidfiles("thinking")]
        assert names == ["thinking-s1.pid", "thinking.pid"]

    def test_an_unreadable_directory_still_answers(self, monkeypatch):
        monkeypatch.setattr(turn, "BASE", _NoBase())
        assert turn.pidfiles("thinking") == []


class TestMain:
    """`turn.py`, the command."""

    def test_one_session_prints_its_state(self, monkeypatch, capsys):
        turn.write("abcdef0123456789", "thinking")
        monkeypatch.setattr("sys.argv", ["turn.py", "abcdef0123456789"])
        assert turn.main() == 0
        out = capsys.readouterr().out
        assert "abcdef01" in out and "thinking" in out

    def test_nothing_reported_says_so(self, monkeypatch, capsys):
        monkeypatch.setattr("sys.argv", ["turn.py"])
        assert turn.main() == 0
        assert "no session has reported anything yet" in capsys.readouterr().out

    def test_every_session_is_listed_liveliest_first(self, monkeypatch, capsys):
        turn.write("older", "done", "finished up")
        turn.write("newer", "thinking", "still working")
        monkeypatch.setattr("sys.argv", ["turn.py"])
        assert turn.main() == 0
        lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()]
        assert lines[0].strip().startswith("newer")
        assert "still working" in lines[0]
