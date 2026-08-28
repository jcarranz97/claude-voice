"""One sound at a time, in order: the queue, the player and the drain.

Nothing here opens the speakers. ``aplay`` is replaced by a stand-in child, and
the player loop -- which is a ``while True`` around a quarter-second sleep -- is
driven by a clock that does not wait, so a bounded queue empties in no time at
all.
"""

import fcntl
import importlib.util
import json
import os
import sys
from types import SimpleNamespace

import pytest

import claude_voice.audioq as audioq


class Clock:
    """A stand-in for ``time``: it counts the waits instead of taking them."""

    def __init__(self, now=1_700_000_000.0):
        self.now = now
        self.slept = []

    def time(self):
        return self.now

    def sleep(self, secs):
        self.slept.append(secs)
        self.now += secs


def wav(home, name="line.wav", data=b"RIFF....WAVEfmt "):
    p = home / name
    p.write_bytes(data)
    return p


def queue_item(n, text="", session="", meta=None):
    """Put one already-synthesized item in the queue, as enqueue would.

    The counter moves with it, so the next real enqueue takes the next number
    rather than writing over what was staged here.
    """
    audioq.QUEUE.mkdir(parents=True, exist_ok=True)
    w = audioq.QUEUE / f"{n:08d}.wav"
    w.write_bytes(b"RIFF....WAVEfmt ")
    (audioq.QUEUE / f"{n:08d}.json").write_text(
        meta if meta is not None else json.dumps({"wav": str(w), "text": text, "session": session})
    )
    audioq.SEQ.write_text(str(n))
    return w


@pytest.fixture
def held_lock(home):
    """Hold the player lock the way a running player would.

    ``flock`` is per open file description rather than per process, so a second
    handle in this same process conflicts exactly as another player's would --
    which is what lets the whole test run without spawning anything.
    """
    audioq.BASE.mkdir(parents=True, exist_ok=True)
    with open(audioq.LOCK, "a+") as f:
        # Closing the handle drops the lock, which is also how a real player
        # releases it: there is nothing to unwind by hand.
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield f


class TestSequence:
    """Arrival order has to survive, so the number only ever goes up."""

    def test_the_first_line_is_number_one(self, home, no_subprocess):
        assert audioq._next_seq() == 1

    def test_each_line_takes_the_next_number(self, home, no_subprocess):
        assert [audioq._next_seq() for _ in range(3)] == [1, 2, 3]

    def test_an_empty_counter_starts_again_at_one(self, home, no_subprocess):
        audioq.QUEUE.mkdir(parents=True, exist_ok=True)
        audioq.SEQ.write_text("")
        assert audioq._next_seq() == 1


class TestEnqueue:
    """Producers only enqueue: a slow hook is a stalled session."""

    @pytest.fixture(autouse=True)
    def quiet(self, home, monkeypatch):
        self.spawned = []
        self.logged = []
        monkeypatch.setattr(audioq, "ensure_player", lambda: self.spawned.append(1))
        monkeypatch.setattr(
            audioq, "_record", lambda text, session="": self.logged.append((text, session))
        )

    def test_the_wav_lands_in_the_queue_with_its_meta(self, home):
        audioq.enqueue(wav(home), text="The tests pass.", session="s1")
        meta = json.loads((audioq.QUEUE / "00000001.json").read_text())
        assert meta["text"] == "The tests pass."
        assert meta["session"] == "s1"
        assert (audioq.QUEUE / "00000001.wav").exists()
        # The synthesizer's file was moved, not copied: nothing is left behind.
        assert not (home / "line.wav").exists()

    def test_it_logs_the_line_and_starts_the_player(self, home):
        audioq.enqueue(wav(home), text="The tests pass.", session="s1")
        assert self.logged == [("The tests pass.", "s1")]
        assert self.spawned == [1]

    def test_a_wav_on_another_filesystem_is_copied_instead(self, home, monkeypatch):
        def _cross_device(src, dst):
            raise OSError(18, "Invalid cross-device link")

        monkeypatch.setattr(audioq.os, "replace", _cross_device)
        audioq.enqueue(wav(home), text="copied")
        assert (audioq.QUEUE / "00000001.wav").read_bytes().startswith(b"RIFF")

    def test_flush_drops_this_sessions_pending_lines(self, home):
        queue_item(1, text="let me check the config", session="s1")
        audioq.enqueue(wav(home), text="Done.", flush_pending=True, session="s1")
        assert sorted(p.name for p in audioq.QUEUE.glob("*.json")) == ["00000002.json"]
        # The wav went with the meta, rather than being left to accumulate.
        assert not (audioq.QUEUE / "00000001.wav").exists()

    def test_flush_drops_the_lines_nobody_claimed(self, home):
        queue_item(1, text="an unattributed line", session="")
        audioq.enqueue(wav(home), text="Done.", flush_pending=True, session="s1")
        assert sorted(p.name for p in audioq.QUEUE.glob("*.json")) == ["00000002.json"]

    def test_flush_leaves_another_windows_narration_alone(self, home):
        queue_item(1, text="another window is talking", session="s2")
        audioq.enqueue(wav(home), text="Done.", flush_pending=True, session="s1")
        assert sorted(p.name for p in audioq.QUEUE.glob("*.json")) == [
            "00000001.json",
            "00000002.json",
        ]

    def test_flush_with_no_session_clears_everything_pending(self, home):
        queue_item(1, text="one", session="s2")
        audioq.enqueue(wav(home), text="Done.", flush_pending=True)
        assert sorted(p.name for p in audioq.QUEUE.glob("*.json")) == ["00000002.json"]

    def test_a_corrupt_meta_does_not_stop_the_flush(self, home):
        queue_item(1, meta="{ half a write")
        audioq.enqueue(wav(home), text="Done.", flush_pending=True, session="s1")
        assert (audioq.QUEUE / "00000002.json").exists()


class TestRecord:
    """The one place every spoken line passes through, so the place to log it."""

    def test_a_line_with_no_text_is_not_logged(self, home, no_subprocess):
        audioq._record("", "s1")

    def test_the_line_reaches_the_spoken_log(self, home, no_subprocess):
        audioq._record("The tests pass.", "s1")
        logs = list(home.glob("spoken-*.jsonl"))
        assert len(logs) == 1
        assert "The tests pass." in logs[0].read_text()

    def test_a_broken_import_never_costs_a_hook(self, home, monkeypatch, no_subprocess):
        def _boom(*a, **kw):
            raise ImportError("spokenlog")

        monkeypatch.setattr(importlib.util, "spec_from_file_location", _boom)
        audioq._record("The tests pass.", "s1")


class TestIsBusy:
    """A player is alive exactly while it holds the lock."""

    def test_nothing_playing_is_not_busy(self, home, no_subprocess):
        assert audioq.is_busy() is False

    def test_a_held_lock_reads_as_busy(self, held_lock, no_subprocess):
        assert audioq.is_busy() is True

    def test_a_lock_that_cannot_be_opened_is_not_busy(self, home, monkeypatch, no_subprocess):
        # Better to spawn a spare player than to swallow a line because the
        # state directory was in an unexpected shape.
        blocked = home / "blocked"
        blocked.mkdir()
        monkeypatch.setattr(audioq, "LOCK", blocked)
        assert audioq.is_busy() is False


class TestEnsurePlayer:
    def test_it_spawns_one_detached_player(self, home, monkeypatch):
        spawned = []
        monkeypatch.setattr(audioq.subprocess, "Popen", lambda cmd, **kw: spawned.append((cmd, kw)))
        audioq.ensure_player()
        cmd, kw = spawned[0]
        assert cmd[0] == sys.executable
        assert cmd[1].endswith("audioq.py")
        assert cmd[2] == "--play"
        # Detached, so it outlives the hook that started it.
        assert kw["start_new_session"] is True

    def test_it_does_not_spawn_a_second_one(self, held_lock, no_subprocess):
        audioq.ensure_player()

    def test_a_spawn_that_fails_is_swallowed(self, home, monkeypatch):
        def _boom(*a, **kw):
            raise OSError("no fork for you")

        monkeypatch.setattr(audioq.subprocess, "Popen", _boom)
        audioq.ensure_player()


class TestSetState:
    """The state of the speaker, which is global because there is one pair."""

    def test_it_writes_the_shape_of_the_line_and_when_it_started(
        self, home, monkeypatch, no_subprocess
    ):
        monkeypatch.setattr(audioq, "time", Clock(now=1_700_000_000.0))
        audioq._set_state("speaking", "The tests pass.", 2.5, "s1", [0, 40, 80], 1_699_999_999.0)
        state = json.loads((home / "state.json").read_text())
        assert state["state"] == "speaking"
        assert state["session"] == "s1"
        assert state["env"] == [0, 40, 80]
        assert state["t0"] == 1_699_999_999.0
        assert state["until"] == pytest.approx(1_700_000_002.5)
        # The step the envelope was sampled at, so a HUD can read it back.
        assert state["step"] == audioq._level().STEP

    def test_a_line_with_no_duration_has_no_deadline(self, home, no_subprocess):
        audioq._set_state("ready")
        assert json.loads((home / "state.json").read_text())["until"] == 0

    def test_an_unwritable_state_directory_is_swallowed(self, home, monkeypatch, no_subprocess):
        blocked = home / "blocked"
        blocked.write_text("not a directory")
        monkeypatch.setattr(audioq, "BASE", blocked / "state")
        audioq._set_state("ready")


class TestLevel:
    def test_it_loads_the_neighbour_module(self, home, no_subprocess):
        assert audioq._level().STEP > 0


class TestPlayLoop:
    """One player, one item at a time, and everything cleaned up behind it."""

    @pytest.fixture(autouse=True)
    def wired(self, home, monkeypatch):
        self.clock = Clock()
        self.played = []
        self.states = []
        monkeypatch.setattr(audioq, "time", self.clock)
        monkeypatch.setattr(
            audioq, "_level", lambda: SimpleNamespace(STEP=0.04, envelope=lambda w: (1.5, [0, 90]))
        )
        monkeypatch.setattr(
            audioq,
            "_set_state",
            lambda state, *a, **kw: self.states.append(state),
        )

    def player(self, monkeypatch, fake_proc):
        def _popen(cmd, **kw):
            self.played.append(cmd)
            return fake_proc(pid=5150)

        monkeypatch.setattr(audioq.subprocess, "Popen", _popen)

    def test_it_plays_the_queue_in_arrival_order(self, home, monkeypatch, fake_proc):
        self.player(monkeypatch, fake_proc)
        queue_item(2, text="second")
        queue_item(1, text="first")
        assert audioq.play_loop() == 0
        assert [cmd[-1] for cmd in self.played] == [
            str(audioq.QUEUE / "00000001.wav"),
            str(audioq.QUEUE / "00000002.wav"),
        ]

    def test_it_clears_up_after_each_item(self, home, monkeypatch, fake_proc):
        self.player(monkeypatch, fake_proc)
        queue_item(1, text="first")
        audioq.play_loop()
        assert list(audioq.QUEUE.iterdir()) == []
        assert not audioq.NOWFILE.exists()

    def test_it_declares_the_speaker_ready_when_the_queue_runs_dry(
        self, home, monkeypatch, fake_proc
    ):
        self.player(monkeypatch, fake_proc)
        queue_item(1, text="first")
        audioq.play_loop()
        assert self.states == ["speaking", "ready"]
        # Four empty rounds of grace, in case something enqueues as it empties.
        assert self.clock.slept == [0.25, 0.25, 0.25]

    def test_a_corrupt_meta_is_dropped_rather_than_retried(self, home, monkeypatch, fake_proc):
        self.player(monkeypatch, fake_proc)
        queue_item(1, meta="{ half a write")
        assert audioq.play_loop() == 0
        assert self.played == []
        assert list(audioq.QUEUE.glob("*.json")) == []

    def test_an_envelope_it_cannot_read_costs_only_the_reactor(self, home, monkeypatch, fake_proc):
        self.player(monkeypatch, fake_proc)

        def _boom():
            raise RuntimeError("not a WAV")

        monkeypatch.setattr(audioq, "_level", _boom)
        queue_item(1, text="first")
        audioq.play_loop()
        assert len(self.played) == 1

    def test_a_machine_with_no_aplay_still_empties_the_queue(self, home, monkeypatch):
        def _missing(*a, **kw):
            raise FileNotFoundError("aplay")

        monkeypatch.setattr(audioq.subprocess, "Popen", _missing)
        queue_item(1, text="first")
        assert audioq.play_loop() == 0
        assert list(audioq.QUEUE.iterdir()) == []

    def test_a_second_player_stands_down(self, held_lock, monkeypatch, no_subprocess):
        queue_item(1, text="first")
        assert audioq.play_loop() == 0
        assert self.played == []
        assert (audioq.QUEUE / "00000001.json").exists()


class TestDrain:
    """The panic button: empty the queue and cut what is playing."""

    def test_an_empty_queue_drains_nothing(self, home, no_subprocess):
        assert audioq.drain() == 0

    def test_it_removes_every_wav_and_meta(self, home, no_subprocess):
        queue_item(1, text="one")
        queue_item(2, text="two")
        assert audioq.drain() == 4
        assert list(audioq.QUEUE.iterdir()) == []

    def test_something_it_cannot_remove_does_not_stop_the_rest(self, home, no_subprocess):
        queue_item(1, text="one")
        (audioq.QUEUE / "stray").mkdir()
        assert audioq.drain() == 2

    def test_it_cuts_whatever_is_playing(self, home, monkeypatch, no_subprocess):
        signalled = []
        monkeypatch.setattr(os, "kill", lambda pid, sig: signalled.append((pid, sig)))
        audioq.NOWFILE.write_text("5150\n")
        assert audioq.drain() == 1
        assert signalled == [(5150, 15)]
        assert not audioq.NOWFILE.exists()

    def test_a_player_that_already_exited_leaves_no_pid_file(
        self, home, monkeypatch, no_subprocess
    ):
        def _gone(pid, sig):
            raise ProcessLookupError(pid)

        monkeypatch.setattr(os, "kill", _gone)
        audioq.NOWFILE.write_text("5150\n")
        assert audioq.drain() == 0
        assert not audioq.NOWFILE.exists()

    def test_a_pid_file_that_is_not_a_pid_is_swept_away(self, home, no_subprocess):
        audioq.NOWFILE.write_text("half a pid\n")
        assert audioq.drain() == 0
        assert not audioq.NOWFILE.exists()
