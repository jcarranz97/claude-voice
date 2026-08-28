"""The ear: the capture loop, the end-of-turn decision, and what reaches Whisper.

Nothing here opens a microphone or loads a model. ``capture()`` is replaced by
a finite tape of frames, the two ONNX models by objects that answer in one
line, and ``WhisperModel`` by a fake with the real one's shape -- an iterator
of segments plus an info object -- so a test that transcribes never downloads
anything and a loop that records always ends on its own.

The thresholds are pulled in an order of magnitude for the same reason: at the
shipped values a single turn is eighty frames of nothing, and a test that has
to count to eighty says nothing about the decision it is testing.
"""

import importlib
import json
import signal
import time
from collections import deque
from types import SimpleNamespace

import faster_whisper
import numpy as np
import pytest

import claude_voice.listen as listen

# A frame the fake VAD calls speech, and one it calls silence. The amplitude is
# real: publish_level does arithmetic on it.
SPEECH = np.full(listen.FRAME, 0.02, dtype=np.float32)
QUIET = np.zeros(listen.FRAME, dtype=np.float32)


def tape(spec: str) -> list:
    """Frames from a compact script: ``"12s 7q"`` is twelve loud then seven quiet."""
    frames = []
    for token in spec.split():
        frames += [SPEECH if token[-1] == "s" else QUIET] * int(token[:-1])
    return frames


class FakeVad:
    """Silero's answer, without Silero: loud frames are speech, quiet ones are not."""

    def __init__(self):
        self.resets = 0

    def reset(self):
        self.resets += 1

    def __call__(self, frame):
        return 0.9 if frame.any() else 0.0


class FakeTurn:
    """smart-turn, pinned. ``prob`` is what it thinks of every silence."""

    def __init__(self, prob=0.9):
        self.prob = prob
        self.asked = 0

    def complete(self, audio):
        self.asked += 1
        return self.prob


class FakeDictate:
    """The delivery side of the loop, as run() sees it.

    ``status`` is consumed one poll at a time and the last entry stands, so a
    test can make the session go away and come back without a clock.
    """

    GLOSSARY = "Terms: pytest, kubectl"

    def __init__(self, status=(True, ""), takes=True):
        self.status = deque([status])
        self.takes = takes
        self.sent = []

    def target_status(self):
        return self.status.popleft() if len(self.status) > 1 else self.status[0]

    def deliver(self, text):
        self.sent.append(text)
        return self.takes


@pytest.fixture
def whisper(monkeypatch):
    """``faster_whisper.WhisperModel``, replaced where run() imports it from.

    The real class downloads a model on first use; this one records how it was
    built and hands back the segments the test asked for.
    """
    rec = SimpleNamespace(texts=["hello there"], error=None, built=[], calls=[])

    class FakeModel:
        def __init__(self, name, **kw):
            rec.built.append((name, kw))

        def transcribe(self, audio, **kw):
            rec.calls.append((audio, kw))
            if rec.error:
                raise rec.error
            # Segments come back as an iterator, not a list, and each carries
            # its own leading and trailing whitespace.
            segments = iter([SimpleNamespace(text=f" {t} ") for t in rec.texts])
            return segments, SimpleNamespace(language="en", duration=1.0)

    monkeypatch.setattr(faster_whisper, "WhisperModel", FakeModel)
    return rec


@pytest.fixture
def ear(monkeypatch, whisper):
    """The loop with every device removed, driven by a tape of frames.

    ``ear.play("12s 7q")`` runs one pass and returns when the tape ends.
    """
    fakes = SimpleNamespace(
        vad=FakeVad(),
        turn=FakeTurn(),
        dictate=FakeDictate(),
        whisper=whisper,
        open=True,
    )
    monkeypatch.setattr(listen, "Vad", lambda: fakes.vad)
    monkeypatch.setattr(listen, "SmartTurn", lambda: fakes.turn)
    monkeypatch.setattr(listen, "_mod", lambda name: {"dictate": fakes.dictate}[name])
    monkeypatch.setattr(listen._presence, "open_now", lambda: fakes.open)
    # The session poll is off unless a test asks for it: a negative interval
    # fires on every frame, a huge one never.
    monkeypatch.setattr(listen, "TARGET_CHECK_S", 10_000.0)
    monkeypatch.setattr(listen, "FLOOR_MS", 224.0)  # seven frames of silence
    monkeypatch.setattr(listen, "CEIL_MS", 480.0)  # fifteen
    monkeypatch.setattr(listen, "_level_next", 0.0)

    def _play(spec):
        frames = tape(spec)
        monkeypatch.setattr(listen, "capture", lambda: iter(frames))
        listen.run()

    fakes.play = _play
    return fakes


@pytest.fixture
def reloaded(home):
    """Re-import the module so a config the test wrote reaches its constants.

    Language, glossary and thresholds are read once, at import, into module
    globals. A test that writes a config file and does not do this is reading
    the configuration the session started with.
    """

    def _reload():
        listen._config.load(reload=True)
        importlib.reload(listen)

    yield _reload
    for leftover in ("config.toml", "preset"):
        (home / leftover).unlink(missing_ok=True)
    listen._config.load(reload=True)
    importlib.reload(listen)


class TestLog:
    """Everything the loop says goes to a file, because its stdout is /dev/null."""

    def test_writes_the_line_to_stdout_and_to_the_log(self, capsys):
        listen.log("listening")
        assert "listening" in capsys.readouterr().out
        assert "listening" in listen.LOG.read_text()

    def test_survives_a_state_directory_it_cannot_write(self, monkeypatch, home, capsys):
        (home / "wall").write_text("not a directory")
        monkeypatch.setattr(listen, "BASE", home / "wall" / "under")
        listen.log("still says it")  # must not raise
        assert "still says it" in capsys.readouterr().out


class TestGate:
    """Am I the one talking? Read off the shared speaker state, not per session."""

    def test_no_state_file_means_the_room_is_mine_to_hear(self):
        assert listen.gated() is False

    def test_a_state_that_is_not_speaking_does_not_gate(self, home):
        (home / "state.json").write_text('{"state": "idle", "until": 1e12}')
        assert listen.gated() is False

    def test_speaking_gates_until_the_end_of_the_utterance(self, home):
        (home / "state.json").write_text('{"state": "speaking", "until": 1e12}')
        assert listen.gated() is True

    def test_the_tail_keeps_the_gate_shut_just_past_the_end(self, home):
        state = home / "state.json"
        state.write_text(json.dumps({"state": "speaking", "until": time.time() - 0.05}))
        assert listen.gated() is True  # inside the 250 ms DAC tail
        state.write_text(json.dumps({"state": "speaking", "until": time.time() - 5}))
        assert listen.gated() is False


class TestPublishedSignals:
    """The three files the HUD reads: speaking, level, stranded."""

    def test_speaking_appears_and_disappears(self, home):
        listen.set_speaking(True)
        assert (home / "mic-active").exists()
        listen.set_speaking(False)
        assert not (home / "mic-active").exists()

    def test_speaking_survives_a_state_directory_it_cannot_write(self, monkeypatch, home):
        (home / "wall").write_text("not a directory")
        monkeypatch.setattr(listen, "BASE", home / "wall" / "under")
        listen.set_speaking(True)  # must not raise: this runs between syllables

    def test_stranded_holds_the_reason_and_clears_on_recovery(self, home):
        listen.set_stranded("target session is gone")
        assert (home / "listen-stranded").read_text() == "target session is gone"
        listen.set_stranded("")
        assert not (home / "listen-stranded").exists()

    def test_stranded_survives_a_state_directory_it_cannot_write(self, monkeypatch, home):
        (home / "wall").write_text("not a directory")
        monkeypatch.setattr(listen, "BASE", home / "wall" / "under")
        listen.set_stranded("nowhere")  # must not raise

    def test_a_loud_frame_moves_the_reactor(self, monkeypatch, home):
        monkeypatch.setattr(listen, "_level_next", 0.0)
        listen.publish_level(SPEECH, quiet=False)
        assert 0.0 < float((home / "mic-level").read_text()) < 1.0

    def test_the_level_is_throttled_to_the_envelope_resolution(self, monkeypatch, home):
        monkeypatch.setattr(listen, "_level_next", 0.0)
        listen.publish_level(SPEECH, quiet=False)
        first = (home / "mic-level").read_text()
        listen.publish_level(np.full(listen.FRAME, 0.9, dtype=np.float32), quiet=False)
        assert (home / "mic-level").read_text() == first

    def test_my_own_voice_publishes_a_flat_reactor(self, monkeypatch, home):
        monkeypatch.setattr(listen, "_level_next", 0.0)
        listen.publish_level(SPEECH, quiet=True)
        assert (home / "mic-level").read_text() == "0.000"

    def test_digital_silence_publishes_zero_rather_than_a_root_of_nothing(self, monkeypatch, home):
        monkeypatch.setattr(listen, "_level_next", 0.0)
        listen.publish_level(QUIET, quiet=False)
        assert (home / "mic-level").read_text() == "0.000"


class TestCapture:
    """What is asked of PipeWire, and what comes back as frames."""

    def _recorder(self, monkeypatch, chunks):
        seen = {}

        class FakeRecorder:
            def __init__(self):
                self._chunks = list(chunks)
                self.terminated = False
                self.stdout = self

            def read(self, n):
                return self._chunks.pop(0) if self._chunks else b""

            def terminate(self):
                self.terminated = True

        def fake_popen(cmd, **kw):
            seen["cmd"] = cmd
            seen["proc"] = FakeRecorder()
            return seen["proc"]

        monkeypatch.setattr(listen.subprocess, "Popen", fake_popen)
        return seen

    def test_records_unbuffered_from_the_default_source(self, monkeypatch):
        monkeypatch.setattr(listen, "MIC_NODE", "")
        seen = self._recorder(monkeypatch, [])
        assert list(listen.capture()) == []
        assert seen["cmd"][:3] == ["stdbuf", "-o0", "pw-record"]
        assert "--target" not in seen["cmd"]
        assert "--raw" in seen["cmd"]

    def test_names_the_configured_pipewire_node(self, monkeypatch):
        monkeypatch.setattr(listen, "MIC_NODE", "alsa_input.usb-Blue")
        seen = self._recorder(monkeypatch, [])
        list(listen.capture())
        assert seen["cmd"][3:5] == ["--target", "alsa_input.usb-Blue"]

    def test_yields_normalised_frames_and_stops_on_a_short_read(self, monkeypatch):
        full = (np.full(listen.FRAME, 16384, dtype="<i2")).tobytes()
        seen = self._recorder(monkeypatch, [full, b"\x00\x00"])
        frames = list(listen.capture())
        assert len(frames) == 1
        assert frames[0].shape == (listen.FRAME,)
        assert frames[0][0] == pytest.approx(0.5)
        # The generator owns the recorder: closing it is what frees the mic.
        assert seen["proc"].terminated


class TestVad:
    """Silero itself, loaded from the asset faster-whisper already ships."""

    def test_scores_a_frame_of_digital_silence_as_not_speech(self):
        vad = listen.Vad()
        assert vad(QUIET) < listen.ON

    def test_reset_clears_the_recurrent_state_between_turns(self):
        vad = listen.Vad()
        vad(SPEECH)
        vad.reset()
        assert not vad.h.any()
        assert not vad.c.any()
        assert not vad.ctx.any()


class TestSmartTurn:
    """The end-of-turn judge: what it is fed, and what its logit becomes."""

    @pytest.fixture
    def turn(self, monkeypatch):
        import huggingface_hub
        import onnxruntime
        from faster_whisper import feature_extractor

        seen = SimpleNamespace(asked=None, samples=None, logit=0.0)

        def fake_download(repo, filename, **kw):
            seen.asked = (repo, filename)
            # Whether the hub had it cached or fetched it just now, the call
            # returns a path on disk and nothing downstream can tell which.
            return "/nowhere/smart-turn.onnx"

        class FakeExtractor:
            def __init__(self, **kw):
                pass

            def __call__(self, audio):
                seen.samples = len(audio)
                return np.zeros((80, 1000), dtype=np.float32)

        class FakeSession:
            def __init__(self, path, **kw):
                seen.path = path

            def run(self, outputs, feed):
                seen.feats = feed["input_features"].shape
                return [np.array([[seen.logit]], dtype=np.float32)]

        monkeypatch.setattr(huggingface_hub, "hf_hub_download", fake_download)
        monkeypatch.setattr(feature_extractor, "FeatureExtractor", FakeExtractor)
        monkeypatch.setattr(onnxruntime, "InferenceSession", FakeSession)
        return seen

    def test_asks_the_hub_for_the_cpu_model_and_uses_the_path_it_returns(self, turn):
        listen.SmartTurn()
        assert turn.asked == ("pipecat-ai/smart-turn-v3", "smart-turn-v3.2-cpu.onnx")
        assert turn.path == "/nowhere/smart-turn.onnx"

    def test_pads_a_short_phrase_to_eight_seconds_at_the_front(self, turn):
        st = listen.SmartTurn()
        st.complete(np.zeros(listen.SR, dtype=np.float32))
        assert turn.samples == listen.SR * 8
        assert turn.feats == (1, 80, 800)

    def test_keeps_only_the_last_eight_seconds_of_a_long_one(self, turn):
        st = listen.SmartTurn()
        st.complete(np.zeros(listen.SR * 20, dtype=np.float32))
        assert turn.samples == listen.SR * 8

    def test_turns_the_logit_into_a_probability(self, turn):
        st = listen.SmartTurn()
        turn.logit = 0.0
        assert st.complete(np.zeros(listen.SR, dtype=np.float32)) == pytest.approx(0.5)
        turn.logit = 2.0
        assert st.complete(np.zeros(listen.SR, dtype=np.float32)) == pytest.approx(0.8808, abs=1e-3)


class TestSiblingModules:
    """The loop loads dictate by path: these files are scripts, not a package."""

    def test_loads_a_sibling_module_by_path(self):
        assert hasattr(listen._mod("dictate"), "deliver")


class TestRun:
    """One pass of the loop, frame by frame."""

    def test_a_finished_phrase_is_transcribed_and_delivered(self, ear):
        ear.play("12s 7q")
        assert ear.dictate.sent == ["hello there"]
        assert "finished p=" in listen.LOG.read_text()

    def test_the_transcriber_is_built_for_the_configured_model_on_cpu(self, ear):
        ear.play("12s 7q")
        name, kw = ear.whisper.built[0]
        assert name == listen.ASR_MODEL
        assert kw["device"] == "cpu"
        assert kw["compute_type"] == "int8"

    def test_the_language_and_the_glossary_reach_whisper(self, ear):
        ear.play("12s 7q")
        audio, kw = ear.whisper.calls[0]
        assert kw["language"] == listen.LANGUAGE
        assert kw["initial_prompt"] == FakeDictate.GLOSSARY
        assert kw["vad_filter"] is True
        assert audio.dtype == np.float32

    def test_an_empty_glossary_is_sent_as_no_prompt_at_all(self, ear, monkeypatch):
        monkeypatch.setattr(ear.dictate, "GLOSSARY", "", raising=False)
        ear.play("12s 7q")
        assert ear.whisper.calls[0][1]["initial_prompt"] is None

    def test_silence_alone_never_reaches_the_transcriber(self, ear):
        ear.play("40q")
        assert ear.whisper.calls == []
        assert ear.dictate.sent == []

    def test_a_phrase_smart_turn_keeps_calling_unfinished_is_sent_at_the_cap(self, ear):
        ear.turn.prob = 0.1
        ear.play("12s 15q")
        assert ear.dictate.sent == ["hello there"]
        assert "[cap]" in listen.LOG.read_text()
        assert ear.turn.asked >= 2  # asked again every 200 ms, not every frame

    def test_a_cough_is_too_short_to_be_a_sentence(self, ear):
        ear.turn.prob = 0.1
        ear.play("1s 15q")
        assert ear.whisper.calls == []

    def test_an_utterance_that_runs_long_is_sent_mid_speech(self, ear, monkeypatch):
        monkeypatch.setattr(listen, "MAX_UTT_S", 0.32)
        ear.play("12s")
        assert ear.dictate.sent == ["hello there"]
        assert "[too long]" in listen.LOG.read_text()

    def test_two_segments_are_joined_into_one_line(self, ear):
        ear.whisper.texts = ["hello there.", "how are you"]
        ear.play("12s 7q")
        assert ear.dictate.sent == ["hello there. how are you"]

    def test_a_transcription_of_nothing_is_not_delivered(self, ear):
        ear.whisper.texts = []
        ear.play("12s 7q")
        assert ear.dictate.sent == []

    def test_a_phrase_whisper_invented_over_silence_is_dropped(self, ear, monkeypatch):
        monkeypatch.setattr(listen, "HALLUCINATIONS", ("thanks for watching",))
        ear.whisper.texts = ["Thanks for watching!"]
        ear.play("12s 7q")
        assert ear.dictate.sent == []

    def test_my_own_voice_is_discarded_rather_than_transcribed(self, ear, monkeypatch, home):
        monkeypatch.setattr(listen, "gated", lambda: True)
        ear.play("12s 7q")
        assert ear.whisper.calls == []
        assert (home / "mic-level").read_text() == "0.000"
        assert ear.vad.resets  # the recurrent state does not survive my turn

    def test_the_loop_stops_when_the_last_window_closes(self, ear, monkeypatch):
        monkeypatch.setattr(listen, "TARGET_CHECK_S", -1.0)
        ear.open = False
        ear.play("12s 7q")
        assert "no window open" in listen.LOG.read_text()
        assert ear.whisper.calls == []

    def test_a_session_that_goes_away_strands_the_loop(self, ear, monkeypatch, home):
        monkeypatch.setattr(listen, "TARGET_CHECK_S", -1.0)
        ear.dictate.status = deque([(False, "no Claude Code session")])
        ear.play("2q 12s")
        assert (home / "listen-stranded").read_text() == "no Claude Code session"
        assert ear.whisper.calls == []
        # Still listening: voice activity is what makes the warning worth showing.
        assert (home / "mic-active").exists()

    def test_the_reason_for_being_stranded_is_said_once(self, ear, monkeypatch):
        monkeypatch.setattr(listen, "TARGET_CHECK_S", -1.0)
        ear.dictate.status = deque([(False, "target session is gone")])
        ear.play("6s 6q")
        assert listen.LOG.read_text().count("stranded: target session is gone") == 1

    def test_the_loop_picks_up_again_when_the_session_comes_back(self, ear, monkeypatch, home):
        monkeypatch.setattr(listen, "TARGET_CHECK_S", -1.0)
        ear.dictate.status = deque([(False, "target session is gone"), (True, "")])
        ear.play("1q 12s 7q")
        assert "session is back" in listen.LOG.read_text()
        assert not (home / "listen-stranded").exists()
        assert ear.dictate.sent == ["hello there"]

    def test_a_sentence_nobody_took_strands_the_loop(self, ear, home):
        ear.dictate.takes = False
        ear.dictate.status = deque([(False, "target session is gone")])
        ear.play("12s 7q")
        assert (home / "listen-stranded").read_text() == "target session is gone"

    def test_an_undelivered_sentence_with_no_reason_still_names_one(self, ear, home):
        # The race: the session closed while the phrase was being transcribed,
        # so the status poll still says everything is fine.
        ear.dictate.takes = False
        ear.play("12s 7q")
        assert (home / "listen-stranded").read_text() == "no Claude Code session"

    def test_a_transcriber_that_raises_ends_the_loop(self, ear):
        # Documented, not endorsed: there is no guard around transcribe(), so a
        # Whisper failure leaves run() and takes the daemon with it.
        ear.whisper.error = RuntimeError("ct2 failed")
        with pytest.raises(RuntimeError):
            ear.play("12s 7q")


class TestCheck:
    """`listen.py --check`: the models load and the latencies are measured."""

    def test_reports_what_each_model_costs(self, monkeypatch, capsys):
        monkeypatch.setattr(listen, "Vad", FakeVad)
        monkeypatch.setattr(listen, "SmartTurn", lambda: FakeTurn())
        listen.check()
        out = capsys.readouterr().out
        assert "Silero loaded in" in out
        assert "smart-turn loaded in" in out
        assert "ms per 32 ms frame" in out
        assert "ms per decision" in out


@pytest.fixture
def keep_sigterm():
    """main() installs a SIGTERM handler in this very process. Put it back."""
    old = signal.getsignal(signal.SIGTERM)
    yield
    signal.signal(signal.SIGTERM, old)


class TestMain:
    """Starting the daemon, and what it refuses to start for."""

    def test_check_is_a_command_of_its_own(self, monkeypatch):
        monkeypatch.setattr(listen.sys, "argv", ["listen.py", "--check"])
        monkeypatch.setattr(listen, "check", lambda: None)
        assert listen.main() == 0

    def test_refuses_to_open_the_microphone_with_no_window_open(self, monkeypatch, capsys):
        monkeypatch.setattr(listen.sys, "argv", ["listen.py"])
        monkeypatch.setattr(listen._presence, "open_now", lambda: False)
        assert listen.main() == 1
        assert "no HUD open" in capsys.readouterr().out
        assert not listen.PIDFILE.exists()

    def test_refuses_when_there_is_nowhere_to_deliver(self, monkeypatch, capsys):
        monkeypatch.setattr(listen.sys, "argv", ["listen.py"])
        monkeypatch.setattr(listen._presence, "open_now", lambda: True)
        monkeypatch.setattr(
            listen,
            "_mod",
            lambda name: SimpleNamespace(target_status=lambda: (False, "no session")),
        )
        assert listen.main() == 1
        assert "no session: conversation disabled" in capsys.readouterr().out
        assert not listen.PIDFILE.exists()

    def test_holds_a_pidfile_for_as_long_as_it_listens(self, monkeypatch, keep_sigterm, home):
        monkeypatch.setattr(listen.sys, "argv", ["listen.py"])
        monkeypatch.setattr(listen._presence, "open_now", lambda: True)
        monkeypatch.setattr(
            listen, "_mod", lambda name: SimpleNamespace(target_status=lambda: (True, ""))
        )
        seen = {}

        def fake_run():
            seen["pid"] = listen.PIDFILE.read_text()
            listen.set_speaking(True)

        monkeypatch.setattr(listen, "run", fake_run)
        assert listen.main() == 0
        assert seen["pid"] == str(listen.os.getpid())
        # Everything the HUD reads is taken down on the way out.
        assert not listen.PIDFILE.exists()
        assert not (home / "mic-active").exists()
        assert not (home / "mic-level").exists()

    def test_an_interrupt_is_how_it_ends_and_not_a_failure(self, monkeypatch, keep_sigterm):
        monkeypatch.setattr(listen.sys, "argv", ["listen.py"])
        monkeypatch.setattr(listen._presence, "open_now", lambda: True)
        monkeypatch.setattr(
            listen, "_mod", lambda name: SimpleNamespace(target_status=lambda: (True, ""))
        )

        def interrupted():
            raise KeyboardInterrupt

        monkeypatch.setattr(listen, "run", interrupted)
        assert listen.main() == 0
        assert not listen.PIDFILE.exists()

    def test_a_termination_signal_becomes_the_interrupt_that_closes_the_mic(
        self, monkeypatch, keep_sigterm
    ):
        monkeypatch.setattr(listen.sys, "argv", ["listen.py"])
        monkeypatch.setattr(listen._presence, "open_now", lambda: True)
        monkeypatch.setattr(
            listen, "_mod", lambda name: SimpleNamespace(target_status=lambda: (True, ""))
        )
        monkeypatch.setattr(listen, "run", lambda: None)
        listen.main()
        handler = signal.getsignal(signal.SIGTERM)
        with pytest.raises(KeyboardInterrupt):
            handler(signal.SIGTERM, None)


class TestConfiguration:
    """The knobs are read once, at import. These check they are read at all."""

    def test_the_language_and_the_hallucinations_follow_the_active_preset(self, home, reloaded):
        (home / "preset").write_text("es")
        reloaded()
        assert listen.LANGUAGE == "es"
        assert "gracias por ver" in listen.HALLUCINATIONS

    def test_the_end_of_turn_window_comes_from_the_config_file(self, home, reloaded):
        (home / "config.toml").write_text(
            "[listen]\nfloor_ms = 900\nceil_ms = 3000\ncomplete = 0.7\nmax_utterance_s = 12\n"
        )
        reloaded()
        assert (listen.FLOOR_MS, listen.CEIL_MS) == (900.0, 3000.0)
        assert listen.COMPLETE == 0.7
        assert listen.MAX_UTT_S == 12.0

    def test_the_microphone_node_is_a_name_and_defaults_to_the_system_source(self, home, reloaded):
        (home / "config.toml").write_text('[stt]\nnode = "alsa_input.usb-Blue"\n')
        reloaded()
        assert listen.MIC_NODE == "alsa_input.usb-Blue"
