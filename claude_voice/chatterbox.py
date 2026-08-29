"""Chatterbox Turbo: the voice that can sigh.

Piper cannot be made expressive. It is a VITS model -- phonemes in, one learned
delivery out -- and there is no input where "laugh" could go. Upstream's own
answer to emotion is to train a multi-speaker model where each speaker *is* an
emotion, which is not a thing we can do for every voice on Hugging Face.

Chatterbox Turbo takes emotion as square-bracket tags inline in the text, which
is exactly the shape the model already writes for us. It is MIT for both the
code and the weights, it runs offline on the CPU, and it needs no account.

Three decisions worth knowing, because none of them is obvious:

**We clone our own Piper voice.** Chatterbox is a cloning model, so the timbre
is whatever reference clip it is given. Generating that clip with the user's
existing Piper voice means the timbre does not change when the provider does --
the voice people are used to simply gains the ability to sigh. It also means no
voice asset ships, no attribution is owed, and a user on the `es` preset gets a
Spanish-sounding Chatterbox for free, because the reference came from their own
Spanish Piper voice. Nothing about presets needs to know this module exists.

**Sampling, never greedy.** Resemble's own ONNX example decodes with argmax,
and with argmax the tags largely no-op -- `[chuckle]` and `[clear throat]`
produce silence. Temperature sampling takes the same weights from mostly-broken
to firing four or five times in six. Anyone who evaluates this model by copying
the README concludes the feature does not work.

**q4, not int8.** The `_quantized` int8 export is five times slower than the q4
one on CPU. The obvious-looking choice is the wrong one.

The speech encoder is 229 MB and runs once per voice, not once per line, so it
is loaded only by `build_voice()` and never at synthesis time.
"""

import sys
import time
import wave
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import config as _config  # noqa: E402

CFG = _config.load()
BASE = _config.BASE

REPO = "ResembleAI/chatterbox-turbo-ONNX"
SR = 24000

# Token ids from the model's own vocabulary. START/STOP bracket the generated
# speech tokens; SIL is padded onto the end so the decoder does not clip the
# final phoneme.
START, STOP, SIL = 6561, 6562, 4299
N_KV, HEAD_DIM = 16, 64
MAX_TOKENS = 1024

# The three graphs synthesis needs. The speech encoder is deliberately absent:
# it is a one-time cost per voice and 229 MB we should not pay to say a
# sentence.
GRAPHS = ("embed_tokens_q4", "language_model_q4", "conditional_decoder_q4")
ENCODER = "speech_encoder_q4"

# What the model was trained to hear. The canonical list lives in config,
# because the providers that cannot hear these need it more than this one does
# -- see the note there.
TAGS = _config.TAG_VOCABULARY

SUPPORTS_TAGS = True

# The reference phrase Piper speaks to seed the clone. Long enough to carry the
# timbre, short enough that the prompt tokens it becomes do not tax every
# subsequent generation -- reference length sets that cost permanently.
REFERENCE_TEXT = (
    "This is the voice you already have. It reads the branch, the tests and "
    "the disk, and it says what happened when the work is done."
)


def voice_file(preset: str = "") -> Path:
    """Where a preset's cloned conditioning lives.

    Per preset, because the reference was generated from that preset's Piper
    voice -- one shared file would speak Spanish lines in an English timbre.
    """
    preset = preset or CFG.preset
    return BASE / "chatterbox" / f"voice-{preset}.npz"


def _snapshot() -> Path:
    """The downloaded weights, or an empty path if they were never fetched."""
    from huggingface_hub import snapshot_download

    return Path(snapshot_download(REPO, allow_patterns=["*.json", "onnx/*q4*"]))


def available(preset: str = "") -> tuple:
    """Can this speak right now? Returns (ok, why-not).

    Two things have to be true and they fail differently: the weights are a
    download, and the voice is a build. Saying which is missing is the whole
    point of returning a reason rather than a bool.
    """
    if not voice_file(preset).exists():
        return False, "no cloned voice yet -- run: claude-voice voice --build"
    try:
        from huggingface_hub import snapshot_download

        snapshot_download(REPO, allow_patterns=["*.json", "onnx/*q4*"], local_files_only=True)
    except Exception:
        return False, "weights not downloaded -- run: claude-voice voice --fetch"
    return True, ""


class _Session:
    """The three graphs plus a tokenizer, loaded once.

    Cold load is about 1.8 s, which is why this is a class and not a function:
    a resident process amortizes it, and a fresh process per line does not.
    """

    def __init__(self, threads: int = 8):
        import onnxruntime
        from tokenizers import Tokenizer

        snap = _snapshot()
        # Eight is measured, not guessed: 4 threads is slower, and 32 thrashes
        # badly enough to be worse than 4.
        opts = onnxruntime.SessionOptions()
        opts.intra_op_num_threads = threads
        opts.inter_op_num_threads = 1

        self.embed, self.lm, self.decoder = [
            onnxruntime.InferenceSession(str(snap / "onnx" / f"{g}.onnx"), opts) for g in GRAPHS
        ]
        self.tokenizer = Tokenizer.from_file(str(snap / "tokenizer.json"))


_session = None


def session(threads: int = 8):
    """The process-wide session. Built on first use, reused after."""
    global _session
    if _session is None:
        _session = _Session(threads)
    return _session


def _sample(scores, rng, temperature: float, top_p: float):
    """Nucleus sampling.

    Greedy argmax is what the upstream ONNX example does and it is why people
    report that the tags do not work -- an emotion token is rarely the single
    most likely next token, so argmax never picks one.
    """
    scores = scores / temperature
    scores = scores - scores.max()
    probs = _np().exp(scores)
    probs /= probs.sum()

    order = _np().argsort(-probs)
    ranked = probs[order]
    keep = _np().cumsum(ranked) <= top_p
    keep[0] = True  # never empty, however peaked the distribution
    order, ranked = order[keep], ranked[keep]
    return int(rng.choice(order, p=ranked / ranked.sum()))


def _np():
    import numpy as np

    return np


def generate(text: str, preset: str = "", seed: int = 0, threads: int = 8):
    """Speech tokens for `text`, decoded to a waveform. Returns a float array."""
    np = _np()
    s = session(threads)
    cond = np.load(voice_file(preset))
    ce, prompt_tokens, speaker_emb, speaker_feat = (cond[k] for k in ("ce", "pt", "se", "sf"))

    rng = np.random.default_rng(seed)
    input_ids = np.array([s.tokenizer.encode(text).ids], dtype=np.int64)
    out = np.array([[START]], dtype=np.int64)
    past = attn = pos = None

    for step in range(MAX_TOKENS):
        embedded = s.embed.run(None, {"input_ids": input_ids})[0]
        if step == 0:
            # The voice conditioning is prepended once, as embeddings -- it is
            # the reference clip's contribution to every token that follows.
            embedded = np.concatenate((ce, embedded), axis=1)
            batch, length, _ = embedded.shape
            past = {
                i.name: np.zeros([batch, N_KV, 0, HEAD_DIM], dtype=np.float32)
                for i in s.lm.get_inputs()
                if "past_key_values" in i.name
            }
            attn = np.ones((batch, length), dtype=np.int64)
            pos = np.arange(length, dtype=np.int64).reshape(1, -1)

        logits, *present = s.lm.run(
            None, dict(inputs_embeds=embedded, attention_mask=attn, position_ids=pos, **past)
        )
        scores = logits[0, -1, :].astype(np.float64).copy()

        # Repetition penalty, applied to what has already been emitted. Without
        # it the model loops a token and runs to MAX_TOKENS on a short line.
        seen = np.unique(out)
        scores[seen] = np.where(scores[seen] < 0, scores[seen] * 1.2, scores[seen] / 1.2)

        nxt = _sample(scores, rng, temperature=0.8, top_p=0.95)
        input_ids = np.array([[nxt]], dtype=np.int64)
        out = np.concatenate((out, input_ids), axis=-1)
        if nxt == STOP:
            break

        attn = np.concatenate([attn, np.ones((attn.shape[0], 1), dtype=np.int64)], axis=1)
        pos = pos[:, -1:] + 1
        for i, key in enumerate(past):
            past[key] = present[i]

    tokens = np.concatenate(
        [prompt_tokens, out[:, 1:-1], np.full((1, 3), SIL, dtype=np.int64)], axis=1
    )
    audio = s.decoder.run(
        None,
        {
            "speech_tokens": tokens,
            "speaker_embeddings": speaker_emb,
            "speaker_features": speaker_feat,
        },
    )[0]
    return audio.reshape(-1)


def synthesize(text: str, path: Path, cfg=None) -> bool:
    """Speak `text` into `path`. Same contract as the Piper path.

    Returns False rather than raising for anything recoverable: this is reached
    from a hook, and a hook that raises breaks somebody's editor.
    """
    cfg = cfg or CFG
    preset = cfg.get("general.preset", "en")
    ok, _why = available(preset)
    if not ok:
        return False
    try:
        audio = generate(text, preset=preset, threads=int(cfg.get("tts.threads", 8)))
    except Exception as exc:  # noqa: BLE001 -- a hook must not raise
        print(f"chatterbox: {exc}", file=sys.stderr)
        return False

    np = _np()
    pcm = (np.clip(audio, -1.0, 1.0) * 32767.0).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())
    return True


def fetch() -> bool:
    """Download the weights. About 490 MB, once."""
    try:
        from huggingface_hub import snapshot_download

        snapshot_download(REPO, allow_patterns=["*.json", "onnx/*q4*", f"onnx/{ENCODER}*"])
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"could not fetch {REPO}: {exc}", file=sys.stderr)
        return False


def build_voice(preset: str = "", text: str = "") -> bool:
    """Clone the user's own Piper voice into conditioning Chatterbox can use.

    This is the step that keeps the timbre. Piper speaks a reference phrase,
    the speech encoder turns that into four arrays, and those arrays are what
    every later generation is conditioned on. About a second, once per voice.
    """
    import speak as _speak

    np = _np()
    preset = preset or CFG.preset
    cfg = _config.resolve(preset)

    out = voice_file(preset)
    out.parent.mkdir(parents=True, exist_ok=True)

    ref = out.with_suffix(".ref.wav")
    if not _speak.synthesize(text or REFERENCE_TEXT, ref, cfg=cfg, provider="piper"):
        print("could not synthesize a reference with Piper", file=sys.stderr)
        return False

    with wave.open(str(ref)) as w:
        rate = w.getframerate()
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")
    audio = (pcm.astype(np.float32) / 32768.0)[np.newaxis, :]

    if rate != SR:
        # Piper voices ship at several rates; the encoder wants one. Linear
        # resampling is enough for a conditioning clip -- this is not the
        # audio anyone hears.
        n = int(round(audio.shape[1] * SR / rate))
        idx = np.linspace(0, audio.shape[1] - 1, n)
        audio = np.interp(idx, np.arange(audio.shape[1]), audio[0]).astype(np.float32)[np.newaxis]

    import onnxruntime

    opts = onnxruntime.SessionOptions()
    opts.intra_op_num_threads = 8
    enc = onnxruntime.InferenceSession(str(_snapshot() / "onnx" / f"{ENCODER}.onnx"), opts)
    ce, pt, se, sf = enc.run(None, {"audio_values": audio})
    np.savez(out, ce=ce, pt=pt, se=se, sf=sf)
    ref.unlink(missing_ok=True)
    return True


def main(argv=None) -> int:
    """`claude-voice voice` -- fetch the weights, build the clone, test it."""
    argv = list(sys.argv[1:] if argv is None else argv)
    preset = CFG.preset

    if "--fetch" in argv:
        print(f"fetching {REPO} (about 490 MB, once)...")
        return 0 if fetch() else 1

    if "--build" in argv:
        print(f"cloning your {preset} Piper voice...")
        t = time.time()
        if not build_voice(preset):
            return 1
        print(f"  {voice_file(preset)}  ({time.time() - t:.1f}s)")
        return 0

    if "--say" in argv:
        i = argv.index("--say")
        text = argv[i + 1] if len(argv) > i + 1 else "Done, the tests pass. [chuckle]"
        out = BASE / "chatterbox-test.wav"
        t = time.time()
        if not synthesize(text, out):
            ok, why = available(preset)
            print(why or "synthesis failed", file=sys.stderr)
            return 1
        print(f"{time.time() - t:.2f}s  {out}")
        import speak as _speak

        _speak.play_detached(out)
        return 0

    ok, why = available(preset)
    print(f"provider   {CFG.get('tts.provider', 'piper')}")
    print(f"preset     {preset}")
    print(f"voice      {voice_file(preset) if voice_file(preset).exists() else '(not built)'}")
    print(f"status     {'ready' if ok else why}")
    print(f"tags       {', '.join(TAGS)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
