"""The TTS marker, and the text that reaches the synthesizer."""

import json
import os
import subprocess
import sys
import tempfile
import wave
from array import array
from pathlib import Path
from types import SimpleNamespace

import pytest

import claude_voice.speak as speak
import claude_voice.turn as turn


class TestSpokenLine:
    """`spoken_line` picks the marker the injected instruction asks for."""

    def test_extracts_the_marker(self):
        assert speak.extract_spoken("Done.\n<!-- TTS: The tests pass. -->") == "The tests pass."

    def test_absent_marker_is_empty(self):
        assert speak.extract_spoken("Done, no marker here.") == ""

    def test_an_empty_response_is_empty(self):
        assert speak.extract_spoken("") == ""

    def test_last_marker_wins(self):
        # A response that documents the marker quotes it in prose; the real one
        # is last by construction.
        text = "Write it as <!-- TTS: example -->, like so.\n<!-- TTS: the real line -->"
        assert speak.extract_spoken(text) == "the real line"

    def test_silent_is_empty(self):
        for word in ("SILENT", "silent", "NONE", "SKIP"):
            assert speak.extract_spoken(f"<!-- TTS: {word} -->") == ""

    def test_newlines_collapse(self):
        assert speak.extract_spoken("<!-- TTS: two\n   lines -->") == "two lines"


def _write_wav(path, samples, rate=22050, chans=1, width=2):
    """A real WAV on disk, because every reader here goes through `wave`."""
    with wave.open(str(path), "wb") as w:
        w.setnchannels(chans)
        w.setsampwidth(width)
        w.setframerate(rate)
        w.writeframes(array("h", samples).tobytes())
    return path


class FakePhonemizer:
    """Stands in for espeak: one phoneme per letter, `' '` between words.

    A letter per phoneme keeps the assertions readable -- a word the foreign
    voice said comes back as the word in upper case, so a substitution is
    visible without knowing any IPA. `separate=False` drops the word
    separator, which is how the alignment guard is reached.
    """

    def __init__(self, foreign="en-us", separate=True):
        self.foreign = foreign
        self.separate = separate
        self.calls = []

    def phonemize(self, voice, text):
        self.calls.append((voice, text))
        words = text.split()
        if voice == self.foreign:
            words = [w.upper() for w in words]
        out = []
        for i, w in enumerate(words):
            if i and self.separate:
                out.append(" ")
            out.extend(w)
        return [out]


class FixedPhonemizer:
    """Answers the same phoneme sentences whatever it is asked."""

    def __init__(self, sentences):
        self.sentences = sentences

    def phonemize(self, voice, text):
        return self.sentences


@pytest.fixture(autouse=True)
def bare_config(home):
    """Reset the OTHER config module.

    The package puts its own directory on `sys.path` and imports its siblings
    by bare name, so `config` and `claude_voice.config` are two module objects
    with two caches over one file. The harness resets the one it imported;
    this resets the one `speak`, `presence` and `focus` actually read, which
    is otherwise carried between tests.
    """
    bare = sys.modules["config"]
    bare.load(reload=True)
    yield bare
    bare.load(reload=True)


@pytest.fixture
def tmp_is_home(home, monkeypatch):
    """Send `tempfile.gettempdir()` into the test home.

    The mute markers and the scratch WAV are the only things this package
    writes outside its own directory, and a test suite has no business
    leaving either in the real /tmp.
    """
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(home))
    return home


@pytest.fixture
def window(home):
    """A HUD open, in the only way presence.py believes: a live pid."""
    f = home / f"hud-{os.getpid()}.pid"
    f.write_text(str(os.getpid()))
    return f


@pytest.fixture
def switch_on(home):
    """`voice on` -- the machine-wide switch."""
    (home / "enabled").touch()
    return home / "enabled"


@pytest.fixture
def sound_server(home, monkeypatch):
    """A PipeWire socket where `audio_available` looks for one."""
    rt = home / "runtime"
    rt.mkdir()
    (rt / "pipewire-0").touch()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(rt))
    return rt


@pytest.fixture
def audible(window, switch_on, sound_server, tmp_is_home):
    """Everything `main` insists on before it makes a sound."""
    return tmp_is_home


class TestSpokenLineLength:
    """The marker is capped, because a runaway line is a runaway minute."""

    def test_the_line_is_cut_at_max_chars(self, write_config, monkeypatch):
        monkeypatch.setattr(speak, "CFG", write_config("[tts]\nmax_chars = 5\n"))
        assert speak.extract_spoken("<!-- TTS: abcdefghij -->") == "abcde"


class TestAudioAvailable:
    """A sound server, or nothing. This is the headless check."""

    def test_no_runtime_dir_means_no_audio(self, monkeypatch):
        monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
        assert speak.audio_available() is False

    def test_a_runtime_dir_without_a_socket_is_not_enough(self, home, monkeypatch):
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(home))
        assert speak.audio_available() is False

    def test_pipewire_counts(self, home, monkeypatch):
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(home))
        (home / "pipewire-0").touch()
        assert speak.audio_available() is True

    def test_pulse_counts_too(self, home, monkeypatch):
        # Two sound servers, one question: either socket answers it.
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(home))
        (home / "pulse").mkdir()
        (home / "pulse" / "native").touch()
        assert speak.audio_available() is True


class TestSetState:
    """The HUD's view of this session, published per session."""

    def test_the_state_lands_under_this_session(self, home):
        speak.set_state("s1", "ready", "the tests pass", 2.0)
        assert turn.read("s1")["state"] == "ready"
        assert turn.read("s1")["text"] == "the tests pass"
        # Another session's file is untouched, which is the whole point of
        # keying them: one window finishing must not calm the others.
        assert turn.read("s2")["state"] == "idle"

    def test_a_broken_state_write_is_swallowed(self, monkeypatch):
        def _boom():
            raise RuntimeError("no turn module")

        monkeypatch.setattr(speak, "_turn", _boom)
        assert speak.set_state("s1", "ready") is None


class TestStopBackground:
    """The end of every turn kills this session's noise, and only its own."""

    def test_it_signals_and_removes_both_pidfiles(self, home, monkeypatch):
        signalled = []
        monkeypatch.setattr(os, "killpg", lambda pid, sig: signalled.append(("group", pid, sig)))
        monkeypatch.setattr(os, "kill", lambda pid, sig: signalled.append(("proc", pid, sig)))
        (home / "thinking-s1.pid").write_text("111")
        (home / "ack-s1.pid").write_text("222")

        speak.stop_background("s1")

        # The heartbeat is a process group (it spawns players); the
        # acknowledgement is a single process.
        assert ("group", 111, 15) in signalled
        assert ("proc", 222, 15) in signalled
        assert not (home / "thinking-s1.pid").exists()
        assert not (home / "ack-s1.pid").exists()

    def test_the_legacy_unkeyed_pidfiles_are_swept_as_well(self, home, monkeypatch):
        signalled = []
        monkeypatch.setattr(os, "killpg", lambda pid, sig: signalled.append(pid))
        monkeypatch.setattr(os, "kill", lambda pid, sig: signalled.append(pid))
        (home / "thinking.pid").write_text("333")

        speak.stop_background("s1")

        assert signalled == [333]
        assert not (home / "thinking.pid").exists()

    def test_without_a_session_only_the_unkeyed_pidfile_is_touched(self, home, monkeypatch):
        monkeypatch.setattr(os, "killpg", lambda pid, sig: None)
        monkeypatch.setattr(os, "kill", lambda pid, sig: None)
        (home / "thinking.pid").write_text("444")
        (home / "thinking-s1.pid").write_text("555")

        speak.stop_background()

        assert not (home / "thinking.pid").exists()
        assert (home / "thinking-s1.pid").exists()

    def test_a_garbage_pidfile_is_removed_rather_than_raising(self, home, no_subprocess):
        (home / "ack-s1.pid").write_text("not a pid")
        speak.stop_background("s1")
        assert not (home / "ack-s1.pid").exists()

    def test_an_unremovable_pidfile_does_not_break_the_turn(self, home, no_subprocess):
        # Nothing about the end of a turn is worth an exception in a hook, so
        # even a pidfile that cannot be read or removed is absorbed.
        (home / "ack-s1.pid").mkdir()
        (home / "ack-s1.pid" / "keeps-it-from-being-removed").touch()
        speak.stop_background("s1")

    def test_a_dead_pid_is_not_an_error(self, home, monkeypatch):
        def _gone(pid, sig):
            raise ProcessLookupError

        monkeypatch.setattr(os, "killpg", _gone)
        monkeypatch.setattr(os, "kill", _gone)
        (home / "thinking-s1.pid").write_text("999999")
        speak.stop_background("s1")
        assert not (home / "thinking-s1.pid").exists()


class TestSessionMute:
    """One window silenced, without touching the machine-wide switch."""

    def test_each_session_gets_its_own_marker(self, tmp_is_home):
        assert speak.session_mute("s1") != speak.session_mute("s2")

    def test_an_empty_session_id_falls_back_to_a_shared_name(self, tmp_is_home):
        # The CLI has no session id, and a mute typed there still has to land
        # somewhere findable.
        assert speak.session_mute("").name.endswith("default")


class TestEnabled:
    """Four things have to agree, and each one alone can say no."""

    def test_a_closed_hud_wins_over_the_switch(self, switch_on):
        assert speak.enabled("s1") is False

    def test_the_switch_off_is_silence(self, window):
        assert speak.enabled("s1") is False

    def test_everything_agreeing_is_a_yes(self, window, switch_on, tmp_is_home):
        assert speak.enabled("s1") is True

    def test_a_muted_session_is_silent(self, window, switch_on, tmp_is_home):
        speak.session_mute("s1").touch()
        assert speak.enabled("s1") is False
        # And only that one: the mute is per window by construction.
        assert speak.enabled("s2") is True

    def test_a_focus_elsewhere_silences_this_session(self, window, switch_on, tmp_is_home, home):
        (home / "focus.json").write_text(json.dumps({"pane": "%99", "label": "other", "tmux": ""}))
        assert speak.enabled("s1") is False

    def test_the_focused_pane_still_speaks(self, window, switch_on, tmp_is_home, home):
        # The pane -> session binding is the join: a hook speaking for a
        # session outside its own terminal is still that session.
        import claude_voice.thinking as thinking

        (home / "focus.json").write_text(json.dumps({"pane": "%99", "label": "mine", "tmux": ""}))
        thinking.bind("s1", pane="%99")
        assert speak.enabled("s1") is True
        assert speak.enabled("s2") is False

    def test_a_headless_machine_can_opt_out_of_the_window_gate(
        self, write_config, bare_config, switch_on
    ):
        # Running with no window was the documented behaviour first, and a
        # box you never sit in front of still wants a voice.
        write_config("[hud]\nrequired = false\n")
        bare_config.load(reload=True)
        assert speak.enabled("s1") is True


def _cfg(primary="es", foreign="", terms=(), overrides=None):
    """A configuration carrying only the four values the phoneme pass reads.

    Written out rather than composed from a config file on purpose: the
    bundled presets ship an override table of their own, and a test about the
    single-language shortcut has to be able to say there is nothing in it.
    """
    return SimpleNamespace(
        primary_voice=primary,
        foreign_voice=foreign,
        foreign_terms=set(terms),
        overrides=dict(overrides or {}),
    )


class TestMixedPhonemes:
    """The two-voice phoneme pass: whole line primary, foreign words swapped."""

    def test_a_single_language_setup_is_left_alone(self):
        out = speak.mixed_phonemes(FakePhonemizer(), "hola mundo", cfg=_cfg())
        assert out == list("hola") + [" "] + list("mundo")

    def test_a_blank_foreign_voice_disables_the_pass_even_with_terms(self):
        # The documented kill switch: terms listed, no voice to say them in.
        cfg = _cfg(foreign="", terms=["merge"])
        assert speak.mixed_phonemes(FakePhonemizer(), "merge", cfg=cfg) == list("merge")

    def test_a_foreign_term_is_said_in_the_foreign_voice(self):
        cfg = _cfg(foreign="en-us", terms=["merge"])
        out = speak.mixed_phonemes(FakePhonemizer(), "hago merge", cfg=cfg)
        assert out == list("hago") + [" "] + list("MERGE")

    def test_the_rest_of_the_line_keeps_the_primary_prosody(self):
        ph = FakePhonemizer()
        speak.mixed_phonemes(ph, "la queue esta", cfg=_cfg(foreign="en-us", terms=["queue"]))
        # The whole line goes through the primary voice once, and only the one
        # word is asked of the second -- never span by span, which would lose
        # the word boundaries and stress every fragment's last word.
        assert ph.calls == [("es", "la queue esta"), ("en-us", "queue")]

    def test_punctuation_survives_the_substitution(self):
        cfg = _cfg(foreign="en-us", terms=["merge"])
        assert speak.mixed_phonemes(FakePhonemizer(), "merge,", cfg=cfg) == list("MERGE") + [","]

    def test_a_plural_falls_back_to_the_singular_term(self):
        cfg = _cfg(foreign="en-us", terms=["hook"])
        assert speak.mixed_phonemes(FakePhonemizer(), "hooks", cfg=cfg) == list("HOOKS")

    def test_an_es_plural_is_reassembled_as_an_es_plural(self):
        cfg = _cfg(foreign="en-us", terms=["branch"])
        assert speak.mixed_phonemes(FakePhonemizer(), "branches", cfg=cfg) == list("BRANCHES")

    def test_a_word_in_neither_list_is_untouched(self):
        cfg = _cfg(foreign="en-us", terms=["merge"])
        assert speak.mixed_phonemes(FakePhonemizer(), "arbol", cfg=cfg) == list("arbol")

    def test_an_override_beats_the_foreign_voice(self):
        cfg = _cfg(foreign="en-us", terms=["merge"], overrides={"merge": "XY"})
        assert speak.mixed_phonemes(FakePhonemizer(), "merge.", cfg=cfg) == ["X", "Y", "."]

    def test_an_override_works_without_any_foreign_voice(self):
        cfg = _cfg(foreign="", overrides={"merge": "XY"})
        assert speak.mixed_phonemes(FakePhonemizer(), "merge", cfg=cfg) == ["X", "Y"]

    def test_an_empty_foreign_rendering_leaves_the_word_alone(self):
        class _Mute(FakePhonemizer):
            def phonemize(self, voice, text):
                return [[]] if voice == self.foreign else super().phonemize(voice, text)

        cfg = _cfg(foreign="en-us", terms=["merge"])
        assert speak.mixed_phonemes(_Mute(), "merge", cfg=cfg) == list("merge")

    def test_sentences_are_rejoined_with_a_separator(self):
        # espeak emits no ' ' between sentences, so without this the segment
        # count drifts from the word count and the whole pass falls back.
        ph = FixedPhonemizer([["a"], ["b"]])
        cfg = _cfg(overrides={"uno": "U"})
        assert speak.mixed_phonemes(ph, "uno dos", cfg=cfg) == ["U", " ", "b"]

    def test_broken_alignment_falls_back_to_the_plain_stream(self):
        # Two words, one segment: never garble, just say it in one language.
        cfg = _cfg(foreign="en-us", terms=["merge"])
        out = speak.mixed_phonemes(FakePhonemizer(separate=False), "hago merge", cfg=cfg)
        assert out == list("hagomerge")

    def test_the_configured_terms_reach_it_through_the_real_layering(self, write_config):
        # One test end to end through config.toml, so the property names the
        # pass reads are the ones the file actually sets.
        cfg = write_config(
            '[tts]\nprimary_voice = "es"\nforeign_voice = "en-us"\n'
            '[pronunciation]\nforeign_terms = ["deploy"]\n'
        )
        out = speak.mixed_phonemes(FakePhonemizer(), "hago deploy", cfg=cfg)
        assert out == list("hago") + [" "] + list("DEPLOY")

    def test_it_falls_back_to_the_module_config_when_given_none(self, monkeypatch):
        monkeypatch.setattr(speak, "CFG", _cfg(primary="fr"))
        ph = FakePhonemizer()
        speak.mixed_phonemes(ph, "hola")
        assert ph.calls == [("fr", "hola")]


def _fake_piper(monkeypatch, audio, sample_rate=22050):
    """Piper, without a model and without a synthesis. Returns the ids seen."""
    import piper
    import piper.phonemize_espeak

    seen = {}

    class _Voice:
        config = SimpleNamespace(sample_rate=sample_rate)

        def phonemes_to_ids(self, phonemes):
            seen["phonemes"] = phonemes
            return [1, 2, 3]

        def phoneme_ids_to_audio(self, ids, synth_cfg):
            seen["ids"] = ids
            seen["synth"] = synth_cfg
            return audio

    class _PiperVoice:
        @staticmethod
        def load(path):
            seen["model"] = path
            return _Voice()

    monkeypatch.setattr(piper, "PiperVoice", _PiperVoice)
    monkeypatch.setattr(piper, "SynthesisConfig", lambda **kw: kw)
    monkeypatch.setattr(piper.phonemize_espeak, "EspeakPhonemizer", lambda: FakePhonemizer())
    return seen


class TestSynthesize:
    """Text to a WAV, and the two ways it declines."""

    def test_a_missing_model_is_a_quiet_false(self, home, write_config, no_subprocess):
        # A voice nobody downloaded must not raise inside a hook. The model
        # lives in the test home so the substitution search finds nothing.
        cfg = write_config(f'[tts]\nvoice_model = "{home}/absent.onnx"\n')
        assert speak.synthesize("hola", home / "out.wav", cfg=cfg) is False
        assert not (home / "out.wav").exists()

    def test_an_empty_phoneme_stream_is_a_quiet_false(self, home, write_config, monkeypatch):
        (home / "voice.onnx").touch()
        cfg = write_config(f'[tts]\nvoice_model = "{home}/voice.onnx"\n')
        _fake_piper(monkeypatch, [0.0])
        monkeypatch.setattr(speak, "mixed_phonemes", lambda *a, **kw: [])
        assert speak.synthesize("hola", home / "out.wav", cfg=cfg) is False

    def test_it_writes_a_16_bit_mono_wav_at_the_voice_rate(self, home, write_config, monkeypatch):
        import numpy as np

        (home / "voice.onnx").touch()
        cfg = write_config(f'[tts]\nvoice_model = "{home}/voice.onnx"\nlength_scale = 1.5\n')
        audio = np.array([0.0, 0.5, -0.5, 2.0, -2.0], dtype="float32")
        seen = _fake_piper(monkeypatch, audio, sample_rate=16000)
        out = home / "out.wav"

        assert speak.synthesize("hola", out, cfg=cfg) is True

        with wave.open(str(out)) as w:
            assert (w.getnchannels(), w.getsampwidth(), w.getframerate()) == (1, 2, 16000)
            pcm = array("h")
            pcm.frombytes(w.readframes(w.getnframes()))
        # Piper hands back float32 in -1..1; written raw into a 16-bit WAV
        # that is full-scale noise. Converted, and clipped so a hot sample
        # cannot wrap around into a click.
        assert list(pcm) == [0, 16383, -16383, 32767, -32767]
        assert seen["synth"] == {"length_scale": 1.5}


@pytest.fixture
def spawned(monkeypatch):
    """Record what would have been spawned, and spawn nothing."""
    calls = []

    def _popen(argv, **kw):
        calls.append((argv, kw))
        return SimpleNamespace(pid=4242, poll=lambda: 0, wait=lambda *a, **k: 0)

    monkeypatch.setattr(subprocess, "Popen", _popen)
    return calls


class TestPlayDetached:
    """The hook must return before the sound finishes."""

    def test_it_starts_aplay_in_its_own_session(self, home, spawned):
        speak.play_detached(home / "line.wav")
        argv, kw = spawned[0]
        assert argv == ["aplay", "-q", str(home / "line.wav")]
        # Detached, or a blocking hook stalls the session behind the audio.
        assert kw["start_new_session"] is True

    def test_it_passes_no_format_flags(self, home, spawned):
        # The WAV header carries the real rate; overriding it is how you end
        # up playing the header as audio.
        speak.play_detached(home / "line.wav")
        assert not {"-f", "-r"} & set(spawned[0][0])


class TestMain:
    """The Stop hook, and the CLI that bypasses it."""

    def test_unreadable_stdin_is_a_quiet_zero(self, feed_stdin, monkeypatch, no_subprocess):
        monkeypatch.setattr(sys, "argv", ["speak.py"])
        feed_stdin("not json at all")
        assert speak.main() == 0

    def test_it_stops_the_heartbeat_before_anything_else(
        self, home, feed_stdin, hook_payload, monkeypatch, no_subprocess
    ):
        # Even a re-entrant Stop has to kill the tick: it is the end of the
        # turn either way, and a tick left running ticks into an empty room.
        monkeypatch.setattr(sys, "argv", ["speak.py"])
        (home / "thinking-s1.pid").write_text("not a pid")
        feed_stdin(hook_payload(session_id="s1", stop_hook_active=True))
        assert speak.main() == 0
        assert not (home / "thinking-s1.pid").exists()

    def test_a_response_without_a_marker_stays_silent(
        self, home, feed_stdin, hook_payload, monkeypatch, no_subprocess
    ):
        monkeypatch.setattr(sys, "argv", ["speak.py"])
        feed_stdin(hook_payload(session_id="s1", last_assistant_message="Done, no marker."))
        assert speak.main() == 0
        # One line per Stop, so "why didn't it talk?" is answerable after the
        # fact rather than guessed at.
        assert "marker=NO" in (home / "speak.log").read_text()

    def test_the_log_records_that_a_marker_was_found(
        self, home, feed_stdin, hook_payload, monkeypatch, no_subprocess
    ):
        monkeypatch.setattr(sys, "argv", ["speak.py"])
        feed_stdin(hook_payload(last_assistant_message="Done.\n<!-- TTS: the tests pass -->"))
        assert speak.main() == 0
        assert "marker=yes" in (home / "speak.log").read_text()

    def test_an_unwritable_log_does_not_break_the_hook(
        self, feed_stdin, hook_payload, monkeypatch, no_subprocess
    ):
        # The log is a diagnostic, and a diagnostic that can break the session
        # it is diagnosing is worse than no log.
        monkeypatch.setattr(speak, "BASE", Path("/proc/nowhere/claude-voice"))
        feed_stdin(hook_payload(last_assistant_message="<!-- TTS: hola -->"))
        monkeypatch.setattr(sys, "argv", ["speak.py"])
        assert speak.main() == 0

    def test_the_voice_off_returns_before_synthesizing(
        self, feed_stdin, hook_payload, monkeypatch, no_subprocess
    ):
        monkeypatch.setattr(sys, "argv", ["speak.py"])
        monkeypatch.setattr(speak, "synthesize", lambda *a, **kw: pytest.fail("synthesized"))
        feed_stdin(hook_payload(last_assistant_message="<!-- TTS: hola -->"))
        assert speak.main() == 0

    def test_no_audio_device_parks_the_session_as_idle(
        self, window, switch_on, tmp_is_home, feed_stdin, hook_payload, monkeypatch, no_subprocess
    ):
        monkeypatch.setattr(sys, "argv", ["speak.py"])
        monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
        feed_stdin(hook_payload(session_id="s1", last_assistant_message="<!-- TTS: hola -->"))
        assert speak.main() == 0
        assert turn.read("s1")["state"] == "idle"

    def test_a_failed_synthesis_parks_the_session_as_idle(
        self, audible, feed_stdin, hook_payload, monkeypatch, no_subprocess
    ):
        monkeypatch.setattr(sys, "argv", ["speak.py"])
        monkeypatch.setattr(speak, "synthesize", lambda *a, **kw: False)
        feed_stdin(hook_payload(session_id="s1", last_assistant_message="<!-- TTS: hola -->"))
        assert speak.main() == 0
        assert turn.read("s1")["state"] == "idle"

    def test_a_spoken_turn_reaches_the_queue_and_reads_ready(
        self, audible, feed_stdin, hook_payload, monkeypatch, spawned
    ):
        monkeypatch.setattr(sys, "argv", ["speak.py"])

        def _synth(text, path, cfg=None):
            _write_wav(path, [0, 1000, -1000])
            return True

        monkeypatch.setattr(speak, "synthesize", _synth)
        feed_stdin(hook_payload(session_id="s1", last_assistant_message="<!-- TTS: hola -->"))

        assert speak.main() == 0

        metas = sorted((audible / "queue").glob("*.json"))
        assert len(metas) == 1
        meta = json.loads(metas[0].read_text())
        assert meta["text"] == "hola"
        assert meta["session"] == "s1"
        # The turn is over while the answer is still being read out: the
        # speaker has its own state and the HUD lays it over this one.
        assert turn.read("s1")["state"] == "ready"

    def test_the_cli_bypasses_the_switch(self, sound_server, tmp_is_home, monkeypatch, spawned):
        # `speak.py "..."` is the way to test the voice with everything off.
        monkeypatch.setattr(sys, "argv", ["speak.py", "hola", "mundo"])
        said = []

        def _synth(text, path, cfg=None):
            said.append(text)
            _write_wav(path, [0, 1000])
            return True

        monkeypatch.setattr(speak, "synthesize", _synth)
        assert speak.main() == 0
        assert said == ["hola mundo"]

    def test_the_cli_still_needs_a_sound_server(self, tmp_is_home, monkeypatch, no_subprocess):
        monkeypatch.setattr(sys, "argv", ["speak.py", "hola"])
        monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
        assert speak.main() == 0
        assert turn.read("")["state"] == "idle"
