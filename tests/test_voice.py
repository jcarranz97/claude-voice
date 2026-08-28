"""The switch, and the UserPromptSubmit hook hanging off it.

Two things are stubbed everywhere in here, and both for the same reason: this
module's job is to start processes and cut them, and a test suite that does
either for real behaves differently on the machine it runs on. ``subprocess``
is replaced with a recorder, and so is the ``os`` the module reaches for its
signals -- so ``silence_all`` can be read back off a list instead of being
aimed at whatever happens to be running.

``tempfile.gettempdir`` is redirected into the test home as well. The mute
markers and the queue's scratch wavs are built with it, and a hook is not
entitled to leave anything in the real ``/tmp``.
"""

import json
import os
import tempfile
import types

import pytest

import claude_voice.voice as voice


class Cfg:
    """Stands in for the configuration the module froze at import time."""

    def __init__(self, instruction="Speak the line.", **over):
        self._d = {k.replace("__", "."): v for k, v in over.items()}
        self.instruction = instruction

    def get(self, dotted, default=None):
        return self._d.get(dotted, default)


@pytest.fixture(autouse=True)
def tmpdir_in_home(home, monkeypatch):
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(home))
    return home


@pytest.fixture
def signals(monkeypatch):
    """Record what would have been killed, and kill nothing."""
    sent = []

    def kill(pid, sig):
        sent.append(("kill", pid, sig))

    def killpg(pid, sig):
        sent.append(("killpg", pid, sig))

    monkeypatch.setattr(
        voice,
        "os",
        types.SimpleNamespace(
            environ=os.environ, getpid=os.getpid, kill=kill, killpg=killpg, replace=os.replace
        ),
    )
    return sent


@pytest.fixture
def spawns(monkeypatch):
    """Record what would have been spawned, and spawn nothing."""
    started = []

    def popen(cmd, **kw):
        started.append(cmd)
        return types.SimpleNamespace(pid=4242)

    monkeypatch.setattr(voice, "subprocess", types.SimpleNamespace(Popen=popen, DEVNULL=-3))
    return started


@pytest.fixture
def procs(home, monkeypatch):
    """A fake ``/proc`` so the panic button sweeps a tree we wrote.

    Walking the real one would make the test depend on whatever else is
    running on the machine, which for a function whose whole job is sending
    signals is not a risk worth taking.
    """
    root = home / "proc"
    root.mkdir()
    real = voice.Path

    def _path(arg="", *rest):
        return real(root, *rest) if str(arg) == "/proc" else real(arg, *rest)

    monkeypatch.setattr(voice, "Path", _path)

    def _add(name, cmdline=None):
        d = root / name
        d.mkdir()
        if cmdline is not None:
            (d / "cmdline").write_bytes(cmdline.encode())
        return d

    return _add


def stub_mods(monkeypatch, **fakes):
    """Swap the modules voice loads by path; anything unnamed stays real."""
    real = voice._mod
    monkeypatch.setattr(voice, "_mod", lambda name: fakes[name] if name in fakes else real(name))


def fake_audioq(drained=0):
    queued = []
    mod = types.SimpleNamespace(
        enqueue=lambda wav, text="", **kw: queued.append((wav, text, kw)),
        drain=lambda: drained,
    )
    mod.queued = queued
    return mod


class TestAckCache:
    """Where the cached acknowledgements live, and what they say."""

    def test_they_live_under_the_preset_that_built_them(self, home):
        assert voice.ack_dir().name == "en"
        assert voice.ack_dir("es").name == "es"

    def test_a_flat_cache_is_moved_under_its_preset(self, home):
        voice.ACKS.mkdir(parents=True)
        (voice.ACKS / "ack00.wav").write_bytes(b"RIFF")
        assert (voice.ack_dir() / "ack00.wav").exists()
        assert not (voice.ACKS / "ack00.wav").exists()

    def test_nothing_loose_is_nothing_to_migrate(self, home):
        voice.ACKS.mkdir(parents=True)
        voice.ack_dir()
        assert list(voice.ACKS.iterdir()) == []

    def test_a_migration_that_cannot_happen_is_left_alone(self, home):
        voice.ACKS.mkdir(parents=True)
        (voice.ACKS / "ack00.wav").write_bytes(b"RIFF")
        (voice.ACKS / "en").write_bytes(b"in the way")  # a file where the dir goes
        voice.ack_dir()
        assert (voice.ACKS / "ack00.wav").exists()

    def test_the_filename_carries_the_phrase(self, home):
        assert voice.ack_phrase("ack00.wav") == "One moment."

    def test_a_name_with_no_index_says_nothing(self, home):
        assert voice.ack_phrase("hello.wav") == ""

    def test_an_index_past_the_phrases_says_nothing(self, home):
        assert voice.ack_phrase("ack99.wav") == ""


class TestPlayAck:
    """The cached phrase, played straight from the switch."""

    def _cache(self, home, *names):
        d = voice.ack_dir()
        d.mkdir(parents=True, exist_ok=True)
        for n in names:
            (d / n).write_bytes(b"RIFF")
        return d

    def test_an_empty_cache_plays_nothing(self, home, monkeypatch):
        aq = fake_audioq()
        stub_mods(monkeypatch, audioq=aq)
        voice.play_ack("s1")
        assert aq.queued == []

    def test_it_queues_a_copy_and_keeps_the_original(self, home, monkeypatch):
        self._cache(home, "ack00.wav")
        aq = fake_audioq()
        stub_mods(monkeypatch, audioq=aq)
        voice.play_ack("s1")
        [(wav, text, kw)] = aq.queued
        assert text == "One moment." and kw["session"] == "s1"
        assert wav.read_bytes() == b"RIFF"
        assert (voice.ack_dir() / "ack00.wav").exists()

    def test_it_does_not_repeat_itself(self, home, monkeypatch):
        self._cache(home, "ack00.wav", "ack01.wav")
        voice.LAST_ACK.write_text("ack00.wav")
        aq = fake_audioq()
        stub_mods(monkeypatch, audioq=aq)
        voice.play_ack()
        assert aq.queued[0][1] == "On it."

    def test_an_unwritable_marker_does_not_stop_the_sound(self, home, monkeypatch):
        self._cache(home, "ack00.wav")
        monkeypatch.setattr(voice, "LAST_ACK", home / "gone" / "last-ack")
        aq = fake_audioq()
        stub_mods(monkeypatch, audioq=aq)
        voice.play_ack()
        assert len(aq.queued) == 1


class TestThinkingProcess:
    """Starting and stopping the heartbeat, one pidfile per session."""

    def test_starting_it_records_the_pid_under_the_session(self, home, spawns, signals):
        voice.start_thinking("s-1")
        pidfile = voice._mod("turn").pidfile("thinking", "s-1")
        assert pidfile.read_text() == "4242"
        assert spawns[0][-2:] == ["--session", "s-1"]

    def test_without_a_session_it_uses_the_legacy_name(self, home, spawns, signals):
        voice.start_thinking()
        assert (home / "thinking.pid").exists()
        assert "--session" not in spawns[0]

    def test_a_survivor_from_the_last_turn_is_killed_first(self, home, spawns, signals):
        pidfile = voice._mod("turn").pidfile("thinking", "s-1")
        pidfile.write_text("999")
        voice.start_thinking("s-1")
        assert ("killpg", 999, 15) in signals

    def test_a_spawn_that_fails_is_swallowed(self, home, monkeypatch, signals):
        def boom(*a, **kw):
            raise OSError("no fork for you")

        monkeypatch.setattr(voice, "subprocess", types.SimpleNamespace(Popen=boom, DEVNULL=-3))
        voice.start_thinking("s-1")  # a hook must not fail over the heartbeat
        assert not (home / "thinking-s-1.pid").exists()

    def test_stopping_nothing_kills_nothing(self, home, signals):
        assert voice.stop_thinking("s-1") == 0
        assert signals == []

    def test_stopping_kills_the_group_and_drops_the_pidfile(self, home, signals):
        pidfile = voice._mod("turn").pidfile("thinking", "s-1")
        pidfile.write_text("999")
        assert voice.stop_thinking("s-1") == 1
        assert signals == [("killpg", 999, 15)]
        assert not pidfile.exists()

    def test_a_pidfile_naming_a_dead_process_is_swept(self, home, monkeypatch):
        def gone(pid, sig):
            raise ProcessLookupError

        monkeypatch.setattr(
            voice, "os", types.SimpleNamespace(environ=os.environ, killpg=gone, getpid=os.getpid)
        )
        pidfile = voice._mod("turn").pidfile("thinking", "s-1")
        pidfile.write_text("999")
        assert voice.stop_thinking("s-1") == 0
        assert not pidfile.exists()

    def test_a_pidfile_that_will_not_go_is_not_worth_raising_over(self, home, monkeypatch, signals):
        def boom(**kw):
            raise PermissionError

        stub_mods(
            monkeypatch,
            turn=types.SimpleNamespace(
                pidfile=lambda kind, session: types.SimpleNamespace(
                    exists=lambda: True, read_text=lambda: "not a pid", unlink=boom
                )
            ),
        )
        assert voice.stop_thinking("s-1") == 0

    def test_a_corrupt_pidfile_is_swept_too(self, home, signals):
        pidfile = voice._mod("turn").pidfile("thinking", "s-1")
        pidfile.write_text("not a pid")
        assert voice.stop_thinking("s-1") == 0
        assert not pidfile.exists()


class TestSilenceAll:
    """The panic button: everything, every window, right now."""

    def test_it_drains_the_queue_and_reports_the_count(self, home, signals, procs, monkeypatch):
        stub_mods(monkeypatch, audioq=fake_audioq(drained=3))
        assert voice.silence_all() == 3

    def test_every_session_pidfile_goes(self, home, signals, procs, monkeypatch):
        stub_mods(monkeypatch, audioq=fake_audioq())
        (home / "thinking-s1.pid").write_text("11")
        (home / "ack-s2.pid").write_text("22")
        (home / "thinking.pid").write_text("33")
        assert voice.silence_all() == 3
        # The heartbeat spawns aplay, so its whole group has to go; the
        # acknowledgement is one process and gets one signal.
        assert ("killpg", 11, 15) in signals
        assert ("kill", 22, 15) in signals
        assert not (home / "thinking-s1.pid").exists()

    def test_a_pidfile_naming_nobody_is_still_removed(self, home, procs, monkeypatch):
        def gone(pid, sig):
            raise ProcessLookupError

        monkeypatch.setattr(
            voice,
            "os",
            types.SimpleNamespace(environ=os.environ, getpid=os.getpid, kill=gone, killpg=gone),
        )
        stub_mods(monkeypatch, audioq=fake_audioq())
        (home / "thinking-s1.pid").write_text("11")
        assert voice.silence_all() == 0
        assert not (home / "thinking-s1.pid").exists()

    def test_a_pidfile_that_vanished_first_is_no_error(self, home, signals, procs, monkeypatch):
        real = voice._mod("turn")
        stub_mods(
            monkeypatch,
            audioq=fake_audioq(),
            turn=types.SimpleNamespace(
                pidfiles=lambda kind: [home / f"{kind}-ghost.pid"],
                sessions=real.sessions,
                write=real.write,
            ),
        )
        assert voice.silence_all() == 0
        assert signals == []

    def test_an_orphaned_heartbeat_is_swept_by_process(self, home, signals, procs, monkeypatch):
        stub_mods(monkeypatch, audioq=fake_audioq())
        # Our own copy of the script, run by an interpreter. Nothing else is
        # a heartbeat, however much it looks like one from a distance.
        procs("101", f"python\x00{voice.HERE / 'thinking.py'}\x00--session\x00s1\x00")
        procs("102", f"aplay\x00-q\x00{tempfile.gettempdir()}/cv-ack-1.wav\x00")
        procs("103", f"aplay\x00-q\x00{voice.BASE}/tick.wav\x00")
        assert voice.silence_all() == 3
        assert {p for _, p, _ in signals} == {101, 102, 103}

    def test_a_heartbeat_that_exits_first_is_not_a_failure(self, home, procs, monkeypatch):
        def _gone(pid, sig):
            raise ProcessLookupError(pid)

        stub_mods(monkeypatch, audioq=fake_audioq())
        monkeypatch.setattr(
            voice,
            "os",
            types.SimpleNamespace(environ=os.environ, getpid=os.getpid, kill=_gone, killpg=_gone),
        )
        procs("105", f"python\x00{voice.HERE / 'thinking.py'}\x00")
        assert voice.silence_all() == 0

    def test_a_heartbeat_from_another_install_is_not_ours(self, home, signals, procs, monkeypatch):
        # Same filename, somebody else's copy. Their sweep, not ours.
        stub_mods(monkeypatch, audioq=fake_audioq())
        procs("104", "python\x00/opt/elsewhere/claude_voice/thinking.py\x00")
        assert voice.silence_all() == 0
        assert signals == []

    def test_a_process_that_merely_names_the_file_is_left_alone(
        self, home, signals, procs, monkeypatch
    ):
        # The reason this rule exists: matching the whole command line meant an
        # editor with the file open, or a grep across the repo, got a SIGTERM.
        stub_mods(monkeypatch, audioq=fake_audioq())
        script = voice.HERE / "thinking.py"
        procs("301", f"nvim\x00{script}\x00")
        procs("302", f"grep\x00-n\x00tick\x00{script}\x00")
        procs("303", f"bash\x00-c\x00python {script} --build\x00")
        assert voice.silence_all() == 0
        assert signals == []

    def test_it_leaves_other_applications_alone(self, home, signals, procs, monkeypatch):
        stub_mods(monkeypatch, audioq=fake_audioq())
        procs("201", "aplay\x00/home/someone/music.wav\x00")
        procs("202", "vim\x00thinking.txt\x00")
        procs("self")  # not a pid at all
        procs(str(os.getpid()), "python\x00/x/thinking.py\x00")  # us
        procs("203")  # no cmdline to read
        assert voice.silence_all() == 0
        assert signals == []

    def test_a_process_that_died_first_is_no_error(self, home, procs, monkeypatch):
        def gone(pid, sig):
            raise ProcessLookupError

        monkeypatch.setattr(
            voice,
            "os",
            types.SimpleNamespace(environ=os.environ, getpid=os.getpid, kill=gone, killpg=gone),
        )
        stub_mods(monkeypatch, audioq=fake_audioq())
        procs("101", "python\x00/x/thinking.py\x00")
        assert voice.silence_all() == 0

    def test_no_window_is_left_claiming_to_think(self, home, signals, procs, monkeypatch):
        stub_mods(monkeypatch, audioq=fake_audioq())
        turn = voice._mod("turn")
        turn.write("s1", "thinking")
        turn.write("s2", "thinking")
        voice.silence_all()
        assert [turn.read(s)["state"] for s in ("s1", "s2")] == ["idle", "idle"]
        assert json.loads((home / "state.json").read_text())["state"] == "idle"

    def test_an_unwritable_state_file_does_not_stop_it(self, home, signals, procs, monkeypatch):
        stub_mods(monkeypatch, audioq=fake_audioq())
        (home / "blocked").write_text("a file, not a directory")
        monkeypatch.setattr(voice, "BASE", home / "blocked" / "deeper")
        assert voice.silence_all() == 0


class TestBuildAcks:
    """Synthesizing the cache, per preset, without speaking a word of it."""

    @pytest.fixture
    def speak(self, monkeypatch):
        made = []

        def synthesize(text, path, cfg=None):
            made.append((text, path.name))
            path.write_bytes(b"RIFF")
            return True

        stub_mods(monkeypatch, speak=types.SimpleNamespace(synthesize=synthesize))
        return made

    def test_it_writes_one_wav_per_phrase(self, home, speak, capsys):
        voice.build_acks()
        wavs = sorted(p.name for p in voice.ack_dir().glob("*.wav"))
        assert wavs[0] == "ack00.wav" and len(wavs) == len(speak)
        assert "cached for en" in capsys.readouterr().out

    def test_the_previous_cache_is_cleared_first(self, home, speak, capsys):
        d = voice.ack_dir()
        d.mkdir(parents=True)
        (d / "ack99.wav").write_bytes(b"stale")
        voice.build_acks()
        assert not (d / "ack99.wav").exists()

    def test_a_named_preset_builds_that_language_instead(self, home, speak, capsys):
        voice.build_acks("es")
        assert (voice.ACKS / "es" / "ack00.wav").exists()
        assert not (voice.ACKS / "en").exists()

    def test_a_preset_with_no_phrases_builds_nothing(self, home, speak, capsys):
        voice.build_acks("no-such-language")
        assert "nothing to build" in capsys.readouterr().out
        assert speak == []


class TestEnabled:
    """The four questions, in the order speak.py asks them."""

    @pytest.fixture(autouse=True)
    def window(self, monkeypatch):
        monkeypatch.setattr(voice._presence, "open_now", lambda: True)

    def test_a_closed_window_answers_no_before_anything_else(self, home, monkeypatch):
        monkeypatch.setattr(voice._presence, "open_now", lambda: False)
        voice.STATE.touch()
        assert voice.enabled("s1") is False

    def test_the_switch_has_to_be_on(self, home):
        assert voice.enabled("s1") is False
        voice.STATE.touch()
        assert voice.enabled("s1") is True

    def test_a_muted_session_stays_quiet(self, home):
        voice.STATE.touch()
        voice.session_mute("s1").touch()
        assert voice.enabled("s1") is False
        assert voice.enabled("s2") is True  # only that one window

    def test_a_focus_elsewhere_silences_this_one(self, home, monkeypatch):
        voice.STATE.touch()
        voice._focus.set_pane("%9", "another window")
        monkeypatch.setattr(voice._focus, "here", lambda: "%1")
        assert voice.enabled("s1") is False

    def test_the_session_with_no_id_gets_a_marker_of_its_own(self, home):
        assert voice.session_mute("").name.endswith("default")


class TestHookContext:
    """UserPromptSubmit: the one hook that runs on literally every turn."""

    @pytest.fixture(autouse=True)
    def wiring(self, home, monkeypatch, spawns, signals):
        monkeypatch.setattr(voice._presence, "open_now", lambda: True)
        voice.STATE.touch()
        self.spawns = spawns
        self.aq = fake_audioq()
        stub_mods(monkeypatch, audioq=self.aq)

    def _run(self, monkeypatch, feed_stdin, payload):
        monkeypatch.setattr(voice.sys, "argv", ["voice.py", "--hook-context"])
        feed_stdin(payload)
        return voice.main()

    def test_a_malformed_event_is_ignored_rather_than_raised(
        self, home, monkeypatch, feed_stdin, capsys
    ):
        # A hook that raises breaks somebody's editor.
        assert self._run(monkeypatch, feed_stdin, "{ not json at all") == 0
        assert capsys.readouterr().out == ""

    def test_it_binds_the_pane_even_while_the_voice_is_off(
        self, home, monkeypatch, feed_stdin, hook_payload
    ):
        voice.STATE.unlink()
        monkeypatch.setenv("TMUX_PANE", "%4")
        payload = hook_payload(session_id="s-1", cwd="/tmp/p")
        assert self._run(monkeypatch, feed_stdin, payload) == 0
        assert voice._mod("thinking").bound_session("%4") == "s-1"
        assert self.spawns == []

    def test_a_binding_failure_does_not_stop_the_hook(
        self, home, monkeypatch, feed_stdin, hook_payload, capsys
    ):
        def boom(name):
            raise ImportError("no thinking module")

        monkeypatch.setattr(voice, "_mod", boom)
        voice.STATE.unlink()
        assert self._run(monkeypatch, feed_stdin, hook_payload()) == 0

    def test_the_switch_being_off_injects_nothing(
        self, home, monkeypatch, feed_stdin, hook_payload, capsys
    ):
        voice.STATE.unlink()
        assert self._run(monkeypatch, feed_stdin, hook_payload()) == 0
        assert capsys.readouterr().out == ""

    def test_a_muted_session_injects_nothing(
        self, home, monkeypatch, feed_stdin, hook_payload, capsys
    ):
        voice.session_mute("s-1").touch()
        assert self._run(monkeypatch, feed_stdin, hook_payload(session_id="s-1")) == 0
        assert capsys.readouterr().out == ""

    def test_a_focus_on_another_pane_injects_nothing(
        self, home, monkeypatch, feed_stdin, hook_payload, capsys
    ):
        voice._focus.set_pane("%9", "another window")
        monkeypatch.setattr(voice._focus, "here", lambda: "%1")
        assert self._run(monkeypatch, feed_stdin, hook_payload(session_id="s-1")) == 0
        assert capsys.readouterr().out == ""

    def test_a_live_turn_sounds_starts_the_heartbeat_and_injects(
        self, home, monkeypatch, feed_stdin, hook_payload, capsys
    ):
        payload = hook_payload(session_id="s-1", prompt="check the disk")
        assert self._run(monkeypatch, feed_stdin, payload) == 0
        ack_cmd, thinking_cmd = self.spawns
        assert ack_cmd[1].endswith("ack.py") and ack_cmd[-1] == "check the disk"
        assert thinking_cmd[1].endswith("thinking.py")
        assert voice._mod("turn").read("s-1")["state"] == "thinking"
        out = json.loads(capsys.readouterr().out)
        assert out["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
        assert "TTS" in out["hookSpecificOutput"]["additionalContext"]

    def test_an_acknowledgement_that_will_not_start_falls_back_to_the_cache(
        self, home, monkeypatch, feed_stdin, hook_payload, capsys
    ):
        def boom(cmd, **kw):
            raise OSError("no fork for you")

        monkeypatch.setattr(voice, "subprocess", types.SimpleNamespace(Popen=boom, DEVNULL=-3))
        d = voice.ack_dir()
        d.mkdir(parents=True)
        (d / "ack00.wav").write_bytes(b"RIFF")
        assert self._run(monkeypatch, feed_stdin, hook_payload(session_id="s-1")) == 0
        assert [t for _, t, _ in self.aq.queued] == ["One moment."]

    def test_each_half_can_be_switched_off_on_its_own(
        self, home, monkeypatch, feed_stdin, hook_payload, capsys
    ):
        monkeypatch.setattr(
            voice,
            "CFG",
            Cfg(ack__enabled=False, thinking__enabled=False, instruction__enabled=False),
        )
        assert self._run(monkeypatch, feed_stdin, hook_payload(session_id="s-1")) == 0
        assert self.spawns == []
        assert capsys.readouterr().out == ""

    def test_an_empty_instruction_injects_nothing(
        self, home, monkeypatch, feed_stdin, hook_payload, capsys
    ):
        monkeypatch.setattr(voice, "CFG", Cfg(instruction=""))
        assert self._run(monkeypatch, feed_stdin, hook_payload(session_id="s-1")) == 0
        assert capsys.readouterr().out == ""


class TestSwitch:
    """The commands typed by hand, and the status they print."""

    @pytest.fixture(autouse=True)
    def wiring(self, home, monkeypatch, signals, procs):
        stub_mods(monkeypatch, audioq=fake_audioq())
        monkeypatch.setattr(voice._presence, "open_now", lambda: True)
        monkeypatch.setattr(voice._presence, "windows", lambda: [111])

    def _run(self, monkeypatch, *args):
        monkeypatch.setattr(voice.sys, "argv", ["voice.py", *args])
        return voice.main()

    def test_on_writes_the_switch_and_unmutes_this_session(self, home, monkeypatch, capsys):
        monkeypatch.setenv("CLAUDE_SESSION_ID", "s-1")
        voice.session_mute("s-1").touch()
        assert self._run(monkeypatch, "on") == 0
        assert voice.STATE.exists() and not voice.session_mute("s-1").exists()
        assert "voice ON" in capsys.readouterr().out

    def test_on_with_no_window_says_why_nothing_happens(self, home, monkeypatch, capsys):
        monkeypatch.setattr(voice._presence, "open_now", lambda: False)
        assert self._run(monkeypatch, "on") == 0
        assert "no HUD open" in capsys.readouterr().out

    def test_off_cuts_the_sound_as_well_as_the_switch(self, home, monkeypatch, capsys):
        voice.STATE.touch()
        (home / "thinking-s1.pid").write_text("11")
        assert self._run(monkeypatch, "off") == 0
        assert not voice.STATE.exists()
        assert "1 cut" in capsys.readouterr().out

    def test_off_with_nothing_running_says_only_that(self, home, monkeypatch, capsys):
        voice.STATE.touch()
        assert self._run(monkeypatch, "off") == 0
        assert capsys.readouterr().out.strip() == "voice off"

    @pytest.mark.parametrize("word", ["mute", "solo"])
    def test_mute_touches_only_this_sessions_marker(self, home, monkeypatch, capsys, word):
        monkeypatch.setenv("CLAUDE_SESSION_ID", "s-abcdefgh")
        assert self._run(monkeypatch, word) == 0
        assert voice.session_mute("s-abcdefgh").exists()
        assert "s-abcdef" in capsys.readouterr().out

    @pytest.mark.parametrize("word", ["silence", "--silence", "shut"])
    def test_silence_reports_what_it_cut(self, home, monkeypatch, capsys, word):
        (home / "thinking-s1.pid").write_text("11")
        assert self._run(monkeypatch, word) == 0
        assert "(1 process cut)" in capsys.readouterr().out

    def test_silence_pluralises_honestly(self, home, monkeypatch, capsys):
        assert self._run(monkeypatch, "silence") == 0
        assert "(0 processes cut)" in capsys.readouterr().out

    def test_build_acks_is_reachable_from_the_switch(self, home, monkeypatch, capsys):
        stub_mods(
            monkeypatch,
            audioq=fake_audioq(),
            speak=types.SimpleNamespace(synthesize=lambda t, p, cfg=None: False),
        )
        assert self._run(monkeypatch, "--build-acks", "es") == 0
        assert "cached for es" in capsys.readouterr().out

    def test_focus_clear_gives_every_session_its_voice_back(self, home, monkeypatch, capsys):
        voice._focus.set_pane("%9", "somewhere")
        assert self._run(monkeypatch, "focus", "--clear") == 0
        assert voice._focus.pane() == ""

    def test_focus_refuses_when_the_terminal_cannot_be_named(self, home, monkeypatch, capsys):
        monkeypatch.setattr(voice._focus, "here", lambda: "")
        assert self._run(monkeypatch, "focus") == 1
        assert "cannot tell which terminal" in capsys.readouterr().out

    def test_focus_refuses_a_pane_with_no_conversation_in_it(self, home, monkeypatch, capsys):
        # Granting it would silence every window, and this pane is the last
        # place anyone would look for the cause.
        monkeypatch.setattr(voice._focus, "here", lambda: "pts:/dev/pts/9")
        stub_mods(
            monkeypatch,
            audioq=fake_audioq(),
            dictate=types.SimpleNamespace(aim_at_pane_id=lambda pane: ""),
        )
        assert self._run(monkeypatch, "focus") == 1
        assert voice._focus.pane() == ""
        assert "no claude running here" in capsys.readouterr().out

    def test_focus_survives_a_dictation_module_that_will_not_load(self, home, monkeypatch, capsys):
        def boom(name):
            raise ImportError

        monkeypatch.setattr(voice._focus, "here", lambda: "pts:/dev/pts/9")
        monkeypatch.setattr(voice, "_mod", boom)
        assert self._run(monkeypatch, "focus") == 1

    def test_focus_names_the_session_and_quiets_the_others(self, home, monkeypatch, capsys):
        monkeypatch.setattr(voice._focus, "here", lambda: "pts:/dev/pts/9")
        stub_mods(
            monkeypatch,
            audioq=fake_audioq(),
            dictate=types.SimpleNamespace(aim_at_pane_id=lambda pane: "repos/x · a title"),
        )
        (home / "thinking-s1.pid").write_text("11")
        assert self._run(monkeypatch, "focus") == 0
        assert voice._focus.pane() == "pts:/dev/pts/9"
        assert "a title" in capsys.readouterr().out

    def test_the_status_reads_every_gate(self, home, monkeypatch, capsys):
        monkeypatch.setenv("CLAUDE_SESSION_ID", "s-1")
        voice.STATE.touch()
        assert self._run(monkeypatch) == 0
        out = capsys.readouterr().out
        assert "open (1)" in out and "global  : ON" in out and "SPEAKS" in out

    def test_the_status_says_so_when_nothing_will_sound(self, home, monkeypatch, capsys):
        monkeypatch.setattr(voice._presence, "open_now", lambda: False)
        monkeypatch.setattr(voice._presence, "windows", lambda: [])
        assert self._run(monkeypatch) == 0
        out = capsys.readouterr().out
        assert "closed" in out and "off" in out and "silent" in out

    def test_a_headless_install_says_the_window_is_not_required(
        self, home, monkeypatch, capsys, write_config
    ):
        write_config("[hud]\nrequired = false\n")
        monkeypatch.setattr(voice._presence, "windows", lambda: [])
        assert self._run(monkeypatch) == 0
        assert "(not required)" in capsys.readouterr().out
