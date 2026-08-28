"""Push-to-talk dictation: which session the text is aimed at, and how it gets there.

The two ends are cut off. `run.py` -- the wrapper that owns the pty and is the
only thing that can actually type into a session -- is replaced by an object
that records what it was handed, and `WhisperModel` by a fake with the real
one's shape: an iterator of segments plus an info object. So nothing here opens
a microphone, downloads a model, or puts a keystroke anywhere.
"""

import importlib
import time
from types import SimpleNamespace

import faster_whisper
import pytest

import claude_voice.dictate as dictate


def session(sid="wrap:100", directory="claude-voice", title="fix the ear", pane="pts:3"):
    """One live wrapped session, shaped the way run.sessions() returns them."""
    return {
        "id": sid,
        "dir": directory,
        "title": title,
        "pane_id": pane,
        "session": f"uuid-of-{sid}",
        "sock": f"/tmp/{sid}.sock",
    }


class FakeRun:
    """The wrapper, without a pty: it remembers what it was asked to type."""

    def __init__(self):
        self.live = []
        self.typed = []
        self.takes = True
        self.raises = None

    def sessions(self):
        return list(self.live)

    def deliver(self, sess, text):
        if self.raises:
            raise self.raises
        self.typed.append((sess.get("id"), text))
        return self.takes


class FakeSpokenLog:
    def __init__(self):
        self.lines = []

    def record(self, side, text, session=""):
        self.lines.append((side, text, session))


class NoSleep:
    """`time`, minus the wait for arecord to close the WAV header."""

    def __init__(self):
        self.slept = []

    def sleep(self, secs):
        self.slept.append(secs)

    def time(self):
        return time.time()

    def strftime(self, fmt):
        return time.strftime(fmt)


@pytest.fixture
def wired(monkeypatch, home):
    """dictate with its siblings replaced and its recording kept in the test home.

    RECWAV normally lives in /tmp under a fixed name, which a test must not go
    near: a developer with a real dictation in flight would have it deleted.
    """
    run, spoken = FakeRun(), FakeSpokenLog()
    monkeypatch.setattr(dictate, "_mod", lambda name: {"run": run, "spokenlog": spoken}[name])
    monkeypatch.setattr(dictate, "RECWAV", home / "cv-dictation.wav")
    monkeypatch.setattr(dictate, "time", NoSleep())
    return SimpleNamespace(run=run, spoken=spoken, home=home)


@pytest.fixture
def whisper(monkeypatch):
    """``faster_whisper.WhisperModel``, replaced where stop_and_send imports it."""
    rec = SimpleNamespace(texts=["run the tests"], error=None, built=[], calls=[])

    class FakeModel:
        def __init__(self, name, **kw):
            rec.built.append((name, kw))

        def transcribe(self, audio, **kw):
            rec.calls.append((audio, kw))
            if rec.error:
                raise rec.error
            segments = iter([SimpleNamespace(text=f" {t} ") for t in rec.texts])
            return segments, SimpleNamespace(language="en", duration=2.0)

    monkeypatch.setattr(faster_whisper, "WhisperModel", FakeModel)
    return rec


@pytest.fixture
def captured(wired):
    """A recording big enough to be worth transcribing."""
    dictate.RECWAV.write_bytes(b"RIFF" + b"\0" * 8000)
    return dictate.RECWAV


@pytest.fixture
def reloaded(home):
    """Re-import the module so a config the test wrote reaches its constants."""

    def _reload():
        dictate._config.load(reload=True)
        importlib.reload(dictate)

    yield _reload
    for leftover in ("config.toml", "preset"):
        (home / leftover).unlink(missing_ok=True)
    dictate._config.load(reload=True)
    importlib.reload(dictate)


class TestLog:
    """The HUD launches this with output to /dev/null, so the log is all there is."""

    def test_writes_the_line_to_stdout_and_to_the_log(self, capsys):
        dictate.log("recording")
        assert "recording" in capsys.readouterr().out
        assert "recording" in dictate.LOG.read_text()

    def test_survives_a_state_directory_it_cannot_write(self, monkeypatch, home, capsys):
        (home / "wall").write_text("not a directory")
        monkeypatch.setattr(dictate, "BASE", home / "wall" / "under")
        dictate.log("still says it")  # must not raise
        assert "still says it" in capsys.readouterr().out


class TestSiblingModules:
    """These files are scripts loaded by path, not a package."""

    def test_loads_a_sibling_module_by_path(self):
        assert hasattr(dictate._mod("config"), "load")

    def test_a_sibling_that_will_not_load_means_no_sessions(self, monkeypatch):
        def boom(name):
            raise ImportError(name)

        monkeypatch.setattr(dictate, "_mod", boom)
        assert dictate.claude_panes() == []


class TestStoredTarget:
    """pane.json holds the delivery handle, and nothing else is trusted."""

    def test_an_absent_file_is_an_empty_configuration(self):
        assert dictate.cfg() == {}

    def test_a_corrupt_file_is_an_empty_configuration(self, home):
        (home / "pane.json").write_text("{not json")
        assert dictate.cfg() == {}

    def test_reads_the_target_back(self, home):
        (home / "pane.json").write_text('{"pane": "wrap:100"}')
        assert dictate.cfg()["pane"] == "wrap:100"


class TestChoosingTheTarget:
    """current() answers "where does the text go", asking only when it must."""

    def test_no_session_at_all_has_no_target(self, wired):
        assert dictate.current() == ""
        assert dictate.target_status() == (False, dictate.NO_SESSION)

    def test_the_only_session_is_the_answer_without_being_picked(self, wired):
        wired.run.live = [session()]
        assert dictate.current() == "wrap:100"
        assert dictate.target_status() == (True, "")

    def test_an_implicit_target_is_not_written_back_to_disk(self, wired, home):
        wired.run.live = [session()]
        dictate.current()
        assert not (home / "pane.json").exists()

    def test_a_stored_target_wins_while_it_is_alive(self, wired, home):
        wired.run.live = [session("wrap:100"), session("wrap:200")]
        (home / "pane.json").write_text('{"pane": "wrap:200"}')
        assert dictate.current() == "wrap:200"

    def test_a_target_that_died_is_not_the_same_as_no_session(self, wired, home):
        wired.run.live = [session("wrap:100"), session("wrap:200")]
        (home / "pane.json").write_text('{"pane": "wrap:999"}')
        assert dictate.target_status() == (False, dictate.STALE_TARGET)

    def test_several_fresh_sessions_want_a_choice(self, wired):
        wired.run.live = [session("wrap:100"), session("wrap:200")]
        assert dictate.current() == ""
        assert dictate.target_status() == (False, dictate.NO_TARGET)

    def test_finds_the_session_behind_a_handle(self, wired):
        wired.run.live = [session("wrap:100")]
        assert dictate.find("wrap:100")["dir"] == "claude-voice"
        assert dictate.find("wrap:404") == {}
        assert dictate.pane_is_claude("wrap:100") is True
        assert dictate.pane_is_claude("wrap:404") is False

    def test_describes_a_session_by_directory_and_title(self, wired):
        wired.run.live = [session("wrap:100")]
        assert dictate.describe("wrap:100") == "claude-voice · fix the ear"
        assert dictate.describe("wrap:404") == "wrap:404"
        assert dictate.describe("") == "(not set)"

    def test_resolves_the_conversation_behind_the_target(self, wired):
        wired.run.live = [session("wrap:100")]
        assert dictate.target_session() == "uuid-of-wrap:100"

    def test_no_target_resolves_to_no_conversation(self, wired):
        assert dictate.target_session() == ""


class TestCycling:
    """Moving the target by hand: the HUD's key, and the focus crossing."""

    def test_with_nothing_running_there_is_nothing_to_cycle_to(self, wired):
        assert dictate.cycle() == "(no Claude sessions)"

    def test_moves_to_the_next_session_and_wraps(self, wired, home):
        wired.run.live = [session("wrap:100"), session("wrap:200", title="write the tests")]
        (home / "pane.json").write_text('{"pane": "wrap:100"}')
        assert dictate.cycle() == "claude-voice · write the tests"
        assert dictate.cycle() == "claude-voice · fix the ear"

    def test_a_target_that_is_gone_cycles_back_to_the_first(self, wired, home):
        wired.run.live = [session("wrap:100"), session("wrap:200")]
        (home / "pane.json").write_text('{"pane": "wrap:999"}')
        dictate.cycle()
        assert dictate.cfg()["pane"] == "wrap:100"

    def test_aiming_at_a_terminal_crosses_over_to_its_session(self, wired):
        wired.run.live = [session("wrap:100", pane="pts:3")]
        assert dictate.aim_at_pane_id("pts:3") == "claude-voice · fix the ear"
        assert dictate.cfg()["pane"] == "wrap:100"

    def test_a_terminal_with_no_session_on_it_is_not_aimed_at(self, wired, home):
        wired.run.live = [session("wrap:100", pane="pts:3")]
        assert dictate.aim_at_pane_id("pts:9") == ""
        assert not (home / "pane.json").exists()


class TestDelivery:
    """The one place a spoken sentence becomes typed text."""

    def test_a_delivered_sentence_is_logged_as_yours(self, wired):
        wired.run.live = [session("wrap:100")]
        assert dictate.deliver("run the tests") is True
        assert wired.run.typed == [("wrap:100", "run the tests")]
        assert wired.spoken.lines == [("in", "run the tests", "uuid-of-wrap:100")]

    def test_nothing_is_typed_when_there_is_nowhere_to_type_it(self, wired):
        assert dictate.deliver("run the tests") is False
        assert wired.run.typed == []
        assert dictate.NO_SESSION in dictate.LOG.read_text()

    def test_a_session_that_refuses_the_text_is_a_failure(self, wired):
        wired.run.live = [session("wrap:100")]
        wired.run.takes = False
        assert dictate.deliver("run the tests") is False
        assert wired.spoken.lines == []
        assert "did not take it" in dictate.LOG.read_text()

    def test_a_wrapper_that_raises_does_not_take_the_caller_down(self, wired):
        wired.run.live = [session("wrap:100")]
        wired.run.raises = ConnectionRefusedError("socket is gone")
        assert dictate.deliver("run the tests") is False
        assert "delivery failed: socket is gone" in dictate.LOG.read_text()


class TestRecordingState:
    """A pid file is only believed while the pid behind it is alive."""

    def test_no_pidfile_means_nothing_is_recording(self, wired):
        assert dictate.recording() is False

    def test_a_live_recorder_is_recording(self, wired, monkeypatch):
        monkeypatch.setattr(dictate.os, "kill", lambda pid, sig: None)
        dictate.RECPID.write_text("4242")
        assert dictate.recording() is True

    def test_a_dead_recorder_sweeps_its_own_pidfile(self, wired, monkeypatch):
        def gone(pid, sig):
            raise ProcessLookupError

        monkeypatch.setattr(dictate.os, "kill", gone)
        dictate.RECPID.write_text("4242")
        assert dictate.recording() is False
        assert not dictate.RECPID.exists()

    def test_a_pidfile_that_is_not_a_pid_sweeps_itself_too(self, wired):
        dictate.RECPID.write_text("arecord")
        assert dictate.recording() is False
        assert not dictate.RECPID.exists()


class TestOpeningTheMicrophone:
    """start() refuses before the microphone opens, not after the transcription."""

    def test_refuses_when_there_is_nothing_to_deliver_to(self, wired, no_subprocess):
        assert dictate.start() is False
        assert "not recording: " + dictate.NO_SESSION in dictate.LOG.read_text()

    def test_records_from_the_device_by_name_and_remembers_the_pid(self, wired, monkeypatch):
        wired.run.live = [session("wrap:100")]
        seen = {}

        def fake_popen(cmd, **kw):
            seen["cmd"], seen["kw"] = cmd, kw
            return SimpleNamespace(pid=4242)

        monkeypatch.setattr(dictate.subprocess, "Popen", fake_popen)
        assert dictate.start() is True
        assert seen["cmd"][0] == "arecord"
        assert seen["cmd"][seen["cmd"].index("-D") + 1] == dictate.DEVICE
        assert seen["cmd"][seen["cmd"].index("-d") + 1] == str(dictate.MAX_SECS)
        assert seen["cmd"][-1] == str(dictate.RECWAV)
        # Its own session: the recorder must outlive the key that started it.
        assert seen["kw"]["start_new_session"] is True
        assert dictate.RECPID.read_text() == "4242"

    def test_the_previous_recording_is_gone_before_the_new_one_starts(self, wired, monkeypatch):
        wired.run.live = [session("wrap:100")]
        dictate.RECWAV.write_bytes(b"old take")
        monkeypatch.setattr(dictate.subprocess, "Popen", lambda cmd, **kw: SimpleNamespace(pid=1))
        dictate.start()
        assert not dictate.RECWAV.exists()


class TestStoppingAndSending:
    """stop_and_send(): what is transcribed, and what is not worth transcribing."""

    def test_a_missing_recording_is_not_transcribed(self, wired, whisper):
        dictate.stop_and_send()
        assert whisper.built == []
        assert "nothing captured" in dictate.LOG.read_text()

    def test_a_recording_too_small_to_hold_audio_is_not_transcribed(self, wired, whisper):
        dictate.RECWAV.write_bytes(b"RIFF" + b"\0" * 100)
        dictate.stop_and_send()
        assert whisper.built == []
        assert "nothing captured" in dictate.LOG.read_text()

    def test_the_recorder_is_signalled_and_its_pidfile_removed(self, wired, whisper, monkeypatch):
        signalled = []
        monkeypatch.setattr(dictate.os, "kill", lambda pid, sig: signalled.append((pid, sig)))
        dictate.RECPID.write_text("4242")
        dictate.stop_and_send()
        assert signalled == [(4242, 15)]
        assert not dictate.RECPID.exists()
        # The wait is for arecord to close the WAV header, and nothing else.
        assert dictate.time.slept == [0.35]

    def test_a_pidfile_that_is_not_a_pid_is_no_reason_to_stop(self, wired, whisper, captured):
        dictate.RECPID.write_text("arecord")
        wired.run.live = [session("wrap:100")]
        dictate.stop_and_send()
        assert wired.run.typed == [("wrap:100", "run the tests")]

    def test_the_transcription_is_delivered(self, wired, whisper, captured):
        wired.run.live = [session("wrap:100")]
        dictate.stop_and_send()
        name, kw = whisper.built[0]
        assert name == dictate.MODEL
        assert kw["device"] == "cpu"
        assert kw["compute_type"] == "int8"
        audio, kw = whisper.calls[0]
        assert audio == str(dictate.RECWAV)
        assert kw["language"] == dictate.LANGUAGE
        assert kw["initial_prompt"] == dictate.GLOSSARY
        assert kw["vad_filter"] is True  # without it the glossary hallucinates
        assert wired.run.typed == [("wrap:100", "run the tests")]

    def test_two_segments_are_joined_into_one_line(self, wired, whisper, captured):
        whisper.texts = ["run the tests,", "then push"]
        wired.run.live = [session("wrap:100")]
        dictate.stop_and_send()
        assert wired.run.typed == [("wrap:100", "run the tests, then push")]

    def test_silence_is_logged_and_delivered_to_nobody(self, wired, whisper, captured):
        whisper.texts = []
        wired.run.live = [session("wrap:100")]
        dictate.stop_and_send()
        assert wired.run.typed == []
        assert "(silence)" in dictate.LOG.read_text()

    def test_an_empty_glossary_is_sent_as_no_prompt_at_all(
        self, wired, whisper, captured, monkeypatch
    ):
        monkeypatch.setattr(dictate, "GLOSSARY", "")
        dictate.stop_and_send()
        assert whisper.calls[0][1]["initial_prompt"] is None


class TestCommandLine:
    """Every way the HUD and the key bindings ask this module something."""

    def _argv(self, monkeypatch, *args):
        monkeypatch.setattr(dictate.sys, "argv", ["dictate.py", *args])

    def test_lists_the_sessions_and_marks_the_target(self, wired, monkeypatch, capsys):
        wired.run.live = [session("wrap:100")]
        self._argv(monkeypatch, "--panes")
        assert dictate.main() == 0
        out = capsys.readouterr().out
        assert "wrap:100" in out
        assert "<- target" in out

    def test_says_so_when_there_is_no_session_to_list(self, wired, monkeypatch, capsys):
        self._argv(monkeypatch, "--panes")
        dictate.main()
        assert "none —" in capsys.readouterr().out

    def test_the_target_comes_out_as_json_for_the_hud(self, wired, monkeypatch, capsys):
        import json

        wired.run.live = [session("wrap:100")]
        self._argv(monkeypatch, "--target")
        dictate.main()
        info = json.loads(capsys.readouterr().out)
        assert info["id"] == "wrap:100"
        assert info["ok"] is True
        assert info["why"] == ""

    def test_the_target_session_is_a_bare_uuid(self, wired, monkeypatch, capsys):
        wired.run.live = [session("wrap:100")]
        self._argv(monkeypatch, "--target-session")
        dictate.main()
        assert capsys.readouterr().out.strip() == "uuid-of-wrap:100"

    def test_can_send_exits_zero_with_a_session(self, wired, monkeypatch, capsys):
        wired.run.live = [session("wrap:100")]
        self._argv(monkeypatch, "--can-send")
        assert dictate.main() == 0
        assert "claude-voice · fix the ear" in capsys.readouterr().out

    def test_can_send_exits_one_with_the_reason(self, wired, monkeypatch, capsys):
        self._argv(monkeypatch, "--can-send")
        assert dictate.main() == 1
        assert dictate.NO_SESSION in capsys.readouterr().out

    def test_next_moves_the_target(self, wired, monkeypatch, capsys, home):
        wired.run.live = [session("wrap:100"), session("wrap:200", title="write the tests")]
        (home / "pane.json").write_text('{"pane": "wrap:100"}')
        self._argv(monkeypatch, "--next")
        dictate.main()
        assert "write the tests" in capsys.readouterr().out

    def test_a_target_can_be_set_by_hand(self, wired, monkeypatch, capsys):
        wired.run.live = [session("wrap:100")]
        self._argv(monkeypatch, "--pane", "wrap:100")
        assert dictate.main() == 0
        assert "careful" not in capsys.readouterr().out
        assert dictate.cfg()["pane"] == "wrap:100"

    def test_setting_a_target_that_is_not_there_is_allowed_but_said_out_loud(
        self, wired, monkeypatch, capsys
    ):
        self._argv(monkeypatch, "--pane", "wrap:404")
        dictate.main()
        assert "careful: no claude there" in capsys.readouterr().out

    def test_pane_without_an_id_falls_through_to_the_status(self, wired, monkeypatch, capsys):
        self._argv(monkeypatch, "--pane")
        assert dictate.main() == 0
        assert "recording : no" in capsys.readouterr().out

    def test_the_status_names_the_target_and_the_device(self, wired, monkeypatch, capsys):
        wired.run.live = [session("wrap:100")]
        self._argv(monkeypatch)
        assert dictate.main() == 0
        out = capsys.readouterr().out
        assert "target    : claude-voice · fix the ear" in out
        assert f"device    : {dictate.DEVICE}" in out
        assert "dictation disabled" not in out

    def test_the_status_says_why_dictation_is_off(self, wired, monkeypatch, capsys):
        self._argv(monkeypatch, "--status")
        dictate.main()
        assert "dictation disabled" in capsys.readouterr().out

    def test_the_first_toggle_opens_the_microphone(self, wired, monkeypatch):
        wired.run.live = [session("wrap:100")]
        monkeypatch.setattr(dictate.subprocess, "Popen", lambda cmd, **kw: SimpleNamespace(pid=7))
        self._argv(monkeypatch, "--toggle")
        assert dictate.main() == 0
        assert dictate.RECPID.read_text() == "7"

    def test_the_second_toggle_transcribes_and_sends(self, wired, whisper, captured, monkeypatch):
        wired.run.live = [session("wrap:100")]
        monkeypatch.setattr(dictate.os, "kill", lambda pid, sig: None)
        dictate.RECPID.write_text("4242")
        self._argv(monkeypatch, "--toggle")
        assert dictate.main() == 0
        assert wired.run.typed == [("wrap:100", "run the tests")]

    def test_a_toggle_with_nowhere_to_send_exits_non_zero(self, wired, monkeypatch, capsys):
        self._argv(monkeypatch, "--toggle")
        assert dictate.main() == 1
        assert f"{dictate.NO_SESSION}: dictation disabled" in capsys.readouterr().out

    def test_a_transcription_that_raises_is_reported_rather_than_thrown(
        self, wired, whisper, captured, monkeypatch
    ):
        wired.run.live = [session("wrap:100")]
        whisper.error = RuntimeError("ct2 failed")
        monkeypatch.setattr(dictate.os, "kill", lambda pid, sig: None)
        dictate.RECPID.write_text("4242")
        self._argv(monkeypatch, "--toggle")
        assert dictate.main() == 1
        assert "ERROR: RuntimeError: ct2 failed" in dictate.LOG.read_text()


class TestConfiguration:
    """The knobs are read once, at import. These check they are read at all."""

    def test_the_language_and_the_glossary_follow_the_active_preset(self, home, reloaded):
        (home / "preset").write_text("es")
        reloaded()
        assert dictate.LANGUAGE == "es"
        assert "Terminos:" in dictate.GLOSSARY

    def test_the_recording_settings_come_from_the_config_file(self, home, reloaded):
        (home / "config.toml").write_text(
            '[stt]\ndevice = "hw:CARD=Blue"\nmodel = "medium"\nmax_secs = 45\n'
        )
        reloaded()
        assert dictate.DEVICE == "hw:CARD=Blue"
        assert dictate.MODEL == "medium"
        assert dictate.MAX_SECS == 45
