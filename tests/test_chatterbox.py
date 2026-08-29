"""The expressive provider: what it does with a tag, and how it declines.

Nothing here loads an ONNX graph, reaches Hugging Face, or runs the model. The
three graphs and the tokenizer are doubles that return shapes of the right
rank, which is all the generation loop actually reasons about -- everything
past that is the decoder's business and not ours to re-implement in a test.
"""

import sys
import wave
from types import SimpleNamespace

import numpy as np
import pytest

import claude_voice.chatterbox as cb


@pytest.fixture(autouse=True)
def _no_session(monkeypatch):
    """A test that builds a real session would download half a gigabyte."""
    monkeypatch.setattr(cb, "_session", None)
    yield
    cb._session = None


def _cond(path, prompt_len=8):
    """The four conditioning arrays, at the ranks the loop expects."""
    np.savez(
        path,
        ce=np.zeros((1, 3, 1024), dtype=np.float32),
        pt=np.zeros((1, prompt_len), dtype=np.int64),
        se=np.zeros((1, 192), dtype=np.float32),
        sf=np.zeros((1, 10, 80), dtype=np.float32),
    )


class FakeGraph:
    """One ONNX session. `run` returns whatever the recipe says."""

    def __init__(self, fn, inputs=()):
        self.fn = fn
        self._inputs = [SimpleNamespace(name=n) for n in inputs]

    def run(self, _out, feed):
        return self.fn(feed)

    def get_inputs(self):
        return self._inputs


def _fake_session(stop_after=2, vocab=7000):
    """A session that emits `stop_after` tokens and then STOP."""
    state = {"n": 0}

    def embed(feed):
        n = feed["input_ids"].shape[1]
        return [np.zeros((1, n, 1024), dtype=np.float32)]

    def lm(feed):
        state["n"] += 1
        scores = np.full((1, 1, vocab), -20.0, dtype=np.float32)
        # Make the intended token overwhelmingly likely without being the only
        # candidate -- sampling has to be able to reach it, and top-p has to
        # have something to trim.
        want = cb.STOP if state["n"] > stop_after else 100 + state["n"]
        scores[0, -1, want] = 20.0
        return [scores, np.zeros((1, cb.N_KV, 1, cb.HEAD_DIM), dtype=np.float32)]

    def decoder(feed):
        return [np.linspace(-0.5, 0.5, 240, dtype=np.float32).reshape(1, -1)]

    return SimpleNamespace(
        embed=FakeGraph(embed),
        lm=FakeGraph(lm, inputs=["inputs_embeds", "past_key_values.0.key"]),
        decoder=FakeGraph(decoder),
        tokenizer=SimpleNamespace(encode=lambda t: SimpleNamespace(ids=[1, 2, 3])),
    )


# --------------------------------------------------------------- vocabulary --


def test_tags_are_the_models_own_and_include_the_undocumented_ones():
    assert cb.SUPPORTS_TAGS is True
    assert "laugh" in cb.TAGS and "sigh" in cb.TAGS
    # These two are real trained tokens absent from every README; losing them
    # to a well-meaning tidy-up would be a silent capability regression.
    assert "sarcastic" in cb.TAGS
    assert "dramatic" in cb.TAGS
    assert len(set(cb.TAGS)) == len(cb.TAGS)


# ------------------------------------------------------------------ paths ----


def test_voice_file_is_per_preset(home):
    assert cb.voice_file("en") != cb.voice_file("es")
    assert cb.voice_file("es").name == "voice-es.npz"


def test_voice_file_defaults_to_the_active_preset(home, monkeypatch):
    monkeypatch.setattr(cb.CFG, "_d", {**cb.CFG.as_dict(), "general": {"preset": "es"}})
    assert cb.voice_file().name.endswith("-es.npz")


# -------------------------------------------------------------- available ----


def test_available_names_the_missing_voice_first(home):
    ok, why = cb.available("en")
    assert ok is False
    assert "--build" in why


def test_available_names_the_missing_weights(home, monkeypatch):
    path = cb.voice_file("en")
    path.parent.mkdir(parents=True, exist_ok=True)
    _cond(path)

    def boom(*a, **k):
        raise OSError("not cached")

    monkeypatch.setitem(sys.modules, "huggingface_hub", SimpleNamespace(snapshot_download=boom))
    ok, why = cb.available("en")
    assert ok is False
    assert "--fetch" in why


def test_available_when_both_are_there(home, monkeypatch):
    path = cb.voice_file("en")
    path.parent.mkdir(parents=True, exist_ok=True)
    _cond(path)
    monkeypatch.setitem(
        sys.modules, "huggingface_hub", SimpleNamespace(snapshot_download=lambda *a, **k: "/w")
    )
    assert cb.available("en") == (True, "")


# --------------------------------------------------------------- sampling ----


def test_sampling_never_returns_a_token_outside_the_distribution():
    rng = np.random.default_rng(0)
    scores = np.array([1.0, 5.0, 2.0, 0.5])
    for _ in range(50):
        picked = cb._sample(scores.copy(), rng, temperature=0.8, top_p=0.95)
        assert 0 <= picked < len(scores)


def test_sampling_keeps_at_least_one_candidate_when_the_peak_exceeds_top_p():
    """A distribution where one token already holds more than top_p mass.

    Without the `keep[0] = True` guard the nucleus is empty and the choice
    raises -- which as a hook is a silent turn.
    """
    rng = np.random.default_rng(0)
    scores = np.array([50.0, -50.0, -50.0])
    assert cb._sample(scores, rng, temperature=1.0, top_p=0.5) == 0


def test_sampling_is_not_argmax():
    """The whole reason the tags work. Greedy decoding no-ops them."""
    rng = np.random.default_rng(1)
    scores = np.array([2.0, 2.05, 2.0, 2.0, 2.0])
    picked = {cb._sample(scores.copy(), rng, 1.5, 0.99) for _ in range(60)}
    assert len(picked) > 1


# --------------------------------------------------------------- generate ----


def test_generate_stops_at_the_stop_token(home, monkeypatch):
    _cond(_prepare(home))
    monkeypatch.setattr(cb, "session", lambda threads=8: _fake_session(stop_after=3))
    audio = cb.generate("Done. [sigh]", preset="en")
    assert audio.ndim == 1 and audio.size == 240


def test_generate_passes_the_text_through_the_tokenizer(home, monkeypatch):
    _cond(_prepare(home))
    seen = {}
    s = _fake_session()
    s.tokenizer = SimpleNamespace(
        encode=lambda t: (seen.setdefault("text", t), SimpleNamespace(ids=[1]))[1]
    )
    monkeypatch.setattr(cb, "session", lambda threads=8: s)
    cb.generate("Four failures. [groan]", preset="en")
    assert seen["text"] == "Four failures. [groan]"


def _prepare(home):
    path = cb.voice_file("en")
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


# -------------------------------------------------------------- synthesize ---


def test_synthesize_declines_when_nothing_is_built(home, tmp_path):
    assert cb.synthesize("Done.", tmp_path / "o.wav") is False


def test_synthesize_writes_a_16bit_mono_wav(home, tmp_path, monkeypatch):
    _cond(_prepare(home))
    monkeypatch.setattr(cb, "available", lambda preset="": (True, ""))
    monkeypatch.setattr(cb, "session", lambda threads=8: _fake_session())
    out = tmp_path / "o.wav"
    assert cb.synthesize("Done, the tests pass. [chuckle]", out) is True
    with wave.open(str(out)) as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate()) == (1, 2, cb.SR)
        assert w.getnframes() == 240


def test_synthesize_returns_false_rather_than_raising(home, tmp_path, monkeypatch, capsys):
    """A hook that raises breaks somebody's editor."""
    monkeypatch.setattr(cb, "available", lambda preset="": (True, ""))

    def boom(*a, **k):
        raise RuntimeError("graph exploded")

    monkeypatch.setattr(cb, "generate", boom)
    assert cb.synthesize("Done.", tmp_path / "o.wav") is False
    assert "graph exploded" in capsys.readouterr().err


def test_synthesize_clips_rather_than_wrapping(home, tmp_path, monkeypatch):
    """A hot sample that wraps is a click; clipping is merely loud."""
    monkeypatch.setattr(cb, "available", lambda preset="": (True, ""))
    monkeypatch.setattr(cb, "generate", lambda *a, **k: np.array([9.0, -9.0], dtype=np.float32))
    out = tmp_path / "o.wav"
    assert cb.synthesize("x", out) is True
    with wave.open(str(out)) as w:
        pcm = np.frombuffer(w.readframes(2), dtype="<i2")
    assert pcm.max() <= 32767 and pcm.min() >= -32768


# ------------------------------------------------------------------ fetch ----


def test_fetch_reports_failure_without_raising(home, monkeypatch, capsys):
    def boom(*a, **k):
        raise OSError("no network")

    monkeypatch.setitem(sys.modules, "huggingface_hub", SimpleNamespace(snapshot_download=boom))
    assert cb.fetch() is False
    assert "no network" in capsys.readouterr().err


def test_fetch_asks_for_the_encoder_too(home, monkeypatch):
    """build_voice needs it, and a second download trip is a bad surprise."""
    seen = {}

    def grab(repo, allow_patterns=None, **k):
        seen["patterns"] = allow_patterns
        return "/weights"

    monkeypatch.setitem(sys.modules, "huggingface_hub", SimpleNamespace(snapshot_download=grab))
    assert cb.fetch() is True
    assert any(cb.ENCODER in p for p in seen["patterns"])


# ------------------------------------------------------------- build_voice ---


def test_build_voice_clones_what_piper_just_said(home, monkeypatch):
    """The timbre has to come from the user's own voice, not a shipped asset."""
    spoken = {}

    def fake_piper(text, path, cfg=None, provider=""):
        spoken["text"] = text
        spoken["provider"] = provider
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(cb.SR)
            w.writeframes(np.zeros(cb.SR, dtype="<i2").tobytes())
        return True

    monkeypatch.setitem(sys.modules, "speak", SimpleNamespace(synthesize=fake_piper))
    monkeypatch.setattr(cb, "_snapshot", lambda: home)

    encoded = {}

    class Enc:
        def run(self, _o, feed):
            encoded["samples"] = feed["audio_values"].shape[1]
            return [
                np.zeros((1, 3, 1024), dtype=np.float32),
                np.zeros((1, 8), dtype=np.int64),
                np.zeros((1, 192), dtype=np.float32),
                np.zeros((1, 10, 80), dtype=np.float32),
            ]

    monkeypatch.setitem(
        sys.modules,
        "onnxruntime",
        SimpleNamespace(
            SessionOptions=lambda: SimpleNamespace(), InferenceSession=lambda *a: Enc()
        ),
    )
    assert cb.build_voice("en") is True
    # Piper, explicitly -- asking for the configured provider here would
    # recurse straight back into Chatterbox.
    assert spoken["provider"] == "piper"
    assert cb.voice_file("en").exists()
    assert encoded["samples"] == cb.SR


def test_build_voice_gives_up_when_piper_cannot_speak(home, monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "speak", SimpleNamespace(synthesize=lambda *a, **k: False))
    assert cb.build_voice("en") is False
    assert "reference" in capsys.readouterr().err


def test_build_voice_resamples_a_voice_that_is_not_24k(home, monkeypatch):
    """Piper voices ship at several rates; the encoder accepts exactly one."""

    def fake_piper(text, path, cfg=None, provider=""):
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(22050)
            w.writeframes(np.zeros(22050, dtype="<i2").tobytes())
        return True

    monkeypatch.setitem(sys.modules, "speak", SimpleNamespace(synthesize=fake_piper))
    monkeypatch.setattr(cb, "_snapshot", lambda: home)

    seen = {}

    class Enc:
        def run(self, _o, feed):
            seen["n"] = feed["audio_values"].shape[1]
            return [
                np.zeros((1, 3, 1024), dtype=np.float32),
                np.zeros((1, 8), dtype=np.int64),
                np.zeros((1, 192), dtype=np.float32),
                np.zeros((1, 10, 80), dtype=np.float32),
            ]

    monkeypatch.setitem(
        sys.modules,
        "onnxruntime",
        SimpleNamespace(
            SessionOptions=lambda: SimpleNamespace(), InferenceSession=lambda *a: Enc()
        ),
    )
    assert cb.build_voice("en") is True
    assert seen["n"] == cb.SR  # resampled up from 22050


# ------------------------------------------------------------------- main ----


def test_main_reports_status_and_names_the_tags(home, capsys):
    assert cb.main([]) == 0
    out = capsys.readouterr().out
    assert "not built" in out
    assert "sarcastic" in out


def test_main_fetch_returns_the_failure(home, monkeypatch):
    monkeypatch.setattr(cb, "fetch", lambda: False)
    assert cb.main(["--fetch"]) == 1


def test_main_build_reports_where_it_landed(home, monkeypatch, capsys):
    monkeypatch.setattr(cb, "build_voice", lambda preset: True)
    assert cb.main(["--build"]) == 0
    assert "voice-" in capsys.readouterr().out


def test_main_say_explains_itself_when_it_cannot(home, monkeypatch, capsys):
    monkeypatch.setattr(cb, "synthesize", lambda *a, **k: False)
    assert cb.main(["--say", "Done."]) == 1
    assert "--build" in capsys.readouterr().err


def test_main_say_plays_what_it_made(home, monkeypatch, capsys):
    played = {}
    monkeypatch.setattr(cb, "synthesize", lambda *a, **k: True)
    monkeypatch.setitem(
        sys.modules,
        "speak",
        SimpleNamespace(play_detached=lambda p: played.setdefault("path", p)),
    )
    assert cb.main(["--say", "Done. [chuckle]"]) == 0
    assert played["path"].name == "chatterbox-test.wav"
