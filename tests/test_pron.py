"""The pronunciation workbench: diagnose a word, then hear it."""

import subprocess
import sys
import tempfile
import wave
from array import array
from types import SimpleNamespace

import pytest

import claude_voice.pron as pron


def _cfg(primary="es", foreign="en-us", terms=(), overrides=None):
    """A configuration holding only what the workbench reads off it."""
    return SimpleNamespace(
        primary_voice=primary,
        foreign_voice=foreign,
        foreign_terms=set(terms),
        overrides=dict(overrides or {}),
    )


class FakePhonemizer:
    """espeak, spelled out: one phoneme per letter, upper case for the foreign
    voice. Readable in a printed table, which is what these tests read."""

    def __init__(self, foreign="en-us"):
        self.foreign = foreign

    def phonemize(self, voice, text):
        words = text.split()
        if voice == self.foreign:
            words = [w.upper() for w in words]
        out = []
        for i, w in enumerate(words):
            if i:
                out.append(" ")
            out.extend(w)
        return [out]


class IdenticalPhonemizer:
    """Both voices agree, which is the one case with nothing to fix."""

    def phonemize(self, voice, text):
        return [list(text)]


@pytest.fixture(autouse=True)
def bare_config(home):
    """Reset the OTHER config module.

    The package imports its siblings by bare name, so `config` and
    `claude_voice.config` are two module objects over one file and the harness
    only resets the one it imported. This one is what `pron` and its private
    copy of `speak` actually read.
    """
    bare = sys.modules["config"]
    bare.load(reload=True)
    yield bare
    bare.load(reload=True)


@pytest.fixture
def phonemizer(monkeypatch):
    """Put a predictable espeak in front of the module-level one."""
    fake = FakePhonemizer()
    monkeypatch.setattr(pron, "ph", fake)
    return fake


@pytest.fixture
def configured(monkeypatch):
    """Swap the module's configuration, which is read at import time."""

    def _set(**kw):
        cfg = _cfg(**kw)
        monkeypatch.setattr(pron, "CFG", cfg)
        return cfg

    return _set


class TestPhon:
    """One string of phonemes, sentence boundaries flattened away."""

    def test_it_joins_every_sentence(self, monkeypatch):
        monkeypatch.setattr(pron, "ph", IdenticalPhonemizer())
        assert pron.phon("es", "hola") == "hola"


class TestDiag:
    """The diagnosis: what each voice says, and what to paste if it is wrong."""

    def test_a_word_neither_list_knows_is_a_candidate(self, phonemizer, configured, capsys):
        configured()
        pron.diag(["merge"])
        out = capsys.readouterr().out
        assert "spoken as es (candidate)" in out
        # The point of the command: a line ready to paste, not a verdict.
        assert "[pronunciation]" in out
        assert 'foreign_terms = ["merge"]' in out
        assert 'merge = "merge"' in out

    def test_it_strips_punctuation_and_case_before_asking(self, phonemizer, configured, capsys):
        configured()
        pron.diag(["Merge,"])
        assert "  merge  " in capsys.readouterr().out

    def test_a_term_already_listed_says_so(self, phonemizer, configured, capsys):
        configured(terms=["merge"])
        pron.diag(["merge"])
        printed = capsys.readouterr().out
        assert "already en-us" in printed
        # Nothing to suggest, so no paste block at all.
        assert "[pronunciation]" not in printed

    def test_an_override_is_shown_verbatim(self, phonemizer, configured, capsys):
        configured(overrides={"nginx": "endʒɪn eks"})
        pron.diag(["nginx"])
        assert "OVERRIDE -> endʒɪn eks" in capsys.readouterr().out

    def test_two_voices_that_agree_have_nothing_to_fix(self, monkeypatch, configured, capsys):
        monkeypatch.setattr(pron, "ph", IdenticalPhonemizer())
        configured()
        pron.diag(["error"])
        assert "identical, nothing to do" in capsys.readouterr().out

    def test_without_a_foreign_voice_it_says_so_first(self, phonemizer, configured, capsys):
        configured(foreign="")
        pron.diag(["merge"])
        out = capsys.readouterr().out
        assert "tts.foreign_voice is unset" in out
        # Still worth diagnosing: the override table does not need a second
        # voice, and the suggestion names the generic column instead.
        assert "'foreign' column" in out


class TestSay:
    """Synthesize the phrase, check the audio, then play it."""

    @pytest.fixture
    def synthesizes(self, home, monkeypatch):
        """Piper, replaced by a WAV writer. Nothing is ever synthesized.

        The scratch file goes into the test home, and the private copy of
        `speak` this module carries is put on a single language: `say` prints
        the phonemes through that copy's own configuration, not through the
        one the diagnosis reads.
        """
        monkeypatch.setattr(tempfile, "gettempdir", lambda: str(home))
        monkeypatch.setattr(pron.speak, "CFG", _cfg(foreign=""))

        def _install(samples, rate=1000):
            def _synth(text, path, cfg=None):
                with wave.open(str(path), "wb") as w:
                    w.setnchannels(1)
                    w.setsampwidth(2)
                    w.setframerate(rate)
                    w.writeframes(array("h", samples).tobytes())
                return True

            monkeypatch.setattr(pron.speak, "synthesize", _synth)

        return _install

    def test_it_prints_the_phonemes_it_is_about_to_speak(
        self, phonemizer, synthesizes, monkeypatch, capsys
    ):
        synthesizes([20000] * 100)
        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: None)
        pron.say("hola")
        assert "phonemes: hola" in capsys.readouterr().out

    def test_it_reports_the_duration_and_the_peak(
        self, phonemizer, synthesizes, monkeypatch, capsys
    ):
        synthesizes([20000] * 500)
        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: None)
        pron.say("hola")
        assert "0.50s, peak 20000/32767" in capsys.readouterr().out

    def test_a_nearly_silent_file_is_called_out(self, phonemizer, synthesizes, monkeypatch, capsys):
        # Silence means the phoneme stream went wrong, and you want to know
        # that before you start blaming your ears.
        synthesizes([5] * 100)
        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: None)
        pron.say("hola")
        assert "nearly silent" in capsys.readouterr().out

    def test_an_empty_file_peaks_at_zero(self, phonemizer, synthesizes, monkeypatch, capsys):
        synthesizes([])
        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: None)
        pron.say("hola")
        assert "peak 0/32767" in capsys.readouterr().out

    def test_it_plays_the_file_it_wrote(self, phonemizer, synthesizes, monkeypatch, home):
        synthesizes([20000] * 100)
        played = []
        monkeypatch.setattr(subprocess, "run", lambda argv, **kw: played.append(argv))
        pron.say("hola")
        assert played == [["aplay", "-q", str(home / "cv-pron-test.wav")]]

    def test_a_missing_model_names_the_setting(self, phonemizer, synthesizes, monkeypatch, capsys):
        monkeypatch.setattr(pron.speak, "synthesize", lambda *a, **kw: False)
        pron.say("hola")
        assert "is tts.voice_model present?" in capsys.readouterr().out

    def test_it_does_not_play_what_it_could_not_synthesize(
        self, phonemizer, synthesizes, monkeypatch, no_subprocess
    ):
        monkeypatch.setattr(pron.speak, "synthesize", lambda *a, **kw: False)
        pron.say("hola")


class TestShow:
    """`pron.py list` -- what is overridden right now."""

    def test_it_lists_both_voices_and_both_tables(self, configured, capsys):
        configured(terms=["merge"], overrides={"nginx": "endʒɪn eks"})
        pron.show()
        out = capsys.readouterr().out
        assert "primary voice : es" in out
        assert "foreign voice : en-us" in out
        assert "1 foreign terms" in out
        assert "1 explicit overrides" in out
        assert "nginx" in out

    def test_no_foreign_voice_is_named_rather_than_left_blank(self, configured, capsys):
        configured(foreign="")
        pron.show()
        assert "foreign voice : (none)" in capsys.readouterr().out


class TestMain:
    """The three subcommands, and the usage text for everything else."""

    @pytest.fixture
    def dispatch(self, monkeypatch):
        """Record which subcommand ran, without running it."""
        seen = []
        for name in ("diag", "say", "show"):
            monkeypatch.setattr(pron, name, lambda *a, _n=name: seen.append((_n, a)))
        return seen

    def test_diag_takes_the_rest_of_the_line_as_words(self, dispatch, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["pron.py", "diag", "merge", "queue"])
        assert pron.main() == 0
        assert dispatch == [("diag", (["merge", "queue"],))]

    def test_say_takes_the_rest_of_the_line_as_one_phrase(self, dispatch, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["pron.py", "say", "hago", "merge"])
        assert pron.main() == 0
        assert dispatch == [("say", ("hago merge",))]

    def test_list_takes_nothing(self, dispatch, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["pron.py", "list"])
        assert pron.main() == 0
        assert dispatch == [("show", ())]

    def test_no_arguments_prints_the_usage_and_fails(self, dispatch, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["pron.py"])
        assert pron.main() == 2
        assert "Pronunciation workbench" in capsys.readouterr().out
        assert dispatch == []

    def test_a_subcommand_with_no_word_is_the_usage_too(self, dispatch, monkeypatch, capsys):
        # `diag` alone has nothing to diagnose, and guessing a word would be
        # worse than saying how the command is spelled.
        monkeypatch.setattr(sys, "argv", ["pron.py", "diag"])
        assert pron.main() == 2
        assert "pron.py diag" in capsys.readouterr().out
        assert dispatch == []
