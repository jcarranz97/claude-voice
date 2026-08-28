#!/usr/bin/env python3
"""Conversation mode: continuous listening with end-of-turn detection.

  listen.py            run the loop (the HUD launches it)
  listen.py --check    verify the models and measure latencies

Three layers, cheapest first
----------------------------
1. Silero VAD per 32 ms frame: is there speech? Costs 0.07 ms.
2. On detecting silence, smart-turn judges whether what was said SOUNDS
   FINISHED. Costs ~25 ms and only runs during silences.
3. If it says "incomplete", the wait is extended instead of sending.

Why silence alone is not enough
-------------------------------
A fixed threshold forces you to choose between cutting people off and being
slow: at 600 ms of silence, LiveKit's open benchmark measures 21.7% mid-sentence
cuts, and you need 1600 ms to get down to 5%. A model that judges whether the
phrase sounds finished reaches 5% at 543 ms. That is not moving along the
curve: it is a different curve.

Capture
-------
pw-record, not arecord: arecord opens the device exclusively and leaves the
rest of the system without a microphone for as long as the loop lives. And
stdbuf -o0 because without it libc batches the output into 128 ms bursts.
"""

import json
import os
import signal
import subprocess
import sys
import time
from collections import deque
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import config as _config  # noqa: E402
import level as LEVEL  # noqa: E402
import presence as _presence  # noqa: E402

CFG = _config.load()
BASE = _config.BASE
PIDFILE = BASE / "listen.pid"
LOG = BASE / "listen.log"

SR, FRAME = 16000, 512  # 512 samples is mandatory for Silero, not a choice
FRAME_MS = FRAME / SR * 1000  # 32 ms

ON, OFF = 0.60, 0.35  # hysteresis: entering costs more than leaving
PREROLL_MS = 500  # the trigger lands ~160 ms late; without this the first syllable is clipped
MIN_SPEECH_MS = int(CFG.get("listen.min_speech_ms", 300))  # coughs, key clicks
# 350 ms was too little: with a thinking pause, smart-turn had already ruled
# "finished" before you picked the sentence back up. Waiting longer before
# asking costs latency, but it is the only thing protecting the pause.
FLOOR_MS = float(CFG.get("listen.floor_ms", 700))  # min silence before asking smart-turn
CEIL_MS = float(
    CFG.get("listen.ceil_ms", 2500)
)  # cap: if smart-turn insists "incomplete", send anyway
COMPLETE = float(CFG.get("listen.complete", 0.55))  # probability above which it counts as finished
# Note: measured on real speech, a cut-off phrase scored 0.71 and a complete
# one 0.59. The model does NOT separate the two cases well in every language,
# so the silence floor does more work than the threshold. Do not raise
# COMPLETE blindly.
MAX_UTT_S = float(CFG.get("listen.max_utterance_s", 30))
GATE_TAIL_MS = 250  # stay deaf a little longer after speaking, for the DAC tail
TARGET_CHECK_S = 3.0  # how often to confirm there is still a session to talk to

# What the HUD's reactor moves to while you talk. Tuned by ear against a
# normal speaking voice at a normal distance: it is a picture of the room,
# not a measurement of it, so it is allowed to clip at the top -- shouting
# and speaking up should both look like the reactor is full.
LEVEL_GAIN = 8.0
LEVEL_CURVE = 0.7  # lifts the quiet half; consonants are quiet

# PipeWire node to capture from. Empty = the system default source.
MIC_NODE = CFG.get("stt.node", "") or ""
LANGUAGE = CFG.get("stt.language", "en")
ASR_MODEL = CFG.get("stt.model", "small")

# Whisper hallucinates fixed phrases over near-silent audio. Per language.
HALLUCINATIONS = tuple(s.lower() for s in (CFG.get("stt.hallucinations", []) or []))


def log(msg: str) -> None:
    print(f"  {msg}", flush=True)
    try:
        BASE.mkdir(parents=True, exist_ok=True)
        with LOG.open("a") as f:
            f.write(f"{time.strftime('%F %T')} {msg}\n")
    except Exception:
        pass


def _mod(name: str):
    import importlib.util

    spec = importlib.util.spec_from_file_location(name, HERE / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class Vad:
    """Silero v6, the one faster-whisper already ships. No torch."""

    def __init__(self):
        import faster_whisper
        import onnxruntime as ort

        path = Path(faster_whisper.__file__).parent / "assets" / "silero_vad_v6.onnx"
        opts = ort.SessionOptions()
        opts.inter_op_num_threads = opts.intra_op_num_threads = 1
        self.s = ort.InferenceSession(
            str(path), sess_options=opts, providers=["CPUExecutionProvider"]
        )
        self.reset()

    def reset(self) -> None:
        # The recurrent state MUST be cleared between turns: otherwise the
        # first frames of a phrase are conditioned by the previous one.
        self.h = np.zeros((1, 1, 128), dtype=np.float32)
        self.c = np.zeros((1, 1, 128), dtype=np.float32)
        self.ctx = np.zeros((1, 64), dtype=np.float32)

    def __call__(self, frame: np.ndarray) -> float:
        x = np.concatenate([self.ctx, frame.reshape(1, -1)], axis=1).astype(np.float32)
        out, self.h, self.c = self.s.run(None, {"input": x, "h": self.h, "c": self.c})
        self.ctx = x[:, -64:]
        # speech_probs comes back as a flat vector of 1, not a matrix.
        return float(np.asarray(out[0]).ravel()[0])


class SmartTurn:
    """Judges whether what was said sounds finished. Only runs during silences."""

    def __init__(self):
        import onnxruntime as ort
        from faster_whisper.feature_extractor import FeatureExtractor
        from huggingface_hub import hf_hub_download

        path = hf_hub_download("pipecat-ai/smart-turn-v3", "smart-turn-v3.2-cpu.onnx")
        opts = ort.SessionOptions()
        opts.inter_op_num_threads = opts.intra_op_num_threads = 2
        self.s = ort.InferenceSession(
            str(path), sess_options=opts, providers=["CPUExecutionProvider"]
        )
        # faster-whisper's extractor produces the same log-mel as HF's, which
        # saves us a dependency on transformers.
        self.fx = FeatureExtractor(feature_size=80, hop_length=160, chunk_length=8, n_fft=400)

    def complete(self, audio: np.ndarray) -> float:
        # Model contract: at most 8 s, aligned to the END, zero-padded in
        # front. The other way round does not work.
        want = SR * 8
        a = audio[-want:]
        if len(a) < want:
            a = np.concatenate([np.zeros(want - len(a), dtype=np.float32), a])
        feats = self.fx(a)[np.newaxis, :, :800].astype(np.float32)
        logits = self.s.run(None, {"input_features": feats})[0]
        return float(1.0 / (1.0 + np.exp(-logits[0][0])))


def capture():
    """512-sample frames from PipeWire, without hijacking the microphone."""
    cmd = ["stdbuf", "-o0", "pw-record"]
    if MIC_NODE:
        cmd += ["--target", MIC_NODE]
    cmd += [
        "--rate",
        str(SR),
        "--channels",
        "1",
        "--format",
        "s16",
        "--latency",
        str(FRAME),
        "--raw",
        "-",
    ]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        while True:
            buf = p.stdout.read(FRAME * 2)
            if not buf or len(buf) < FRAME * 2:
                break
            yield np.frombuffer(buf, dtype="<i2").astype(np.float32) / 32768.0
    finally:
        p.terminate()


def set_speaking(active: bool) -> None:
    """Publish whether the user is speaking RIGHT NOW.

    The HUD used to check whether the ALSA device was open, but in continuous
    mode it always is, so the indicator lived permanently lit and told you
    nothing. This is the real signal."""
    try:
        BASE.mkdir(parents=True, exist_ok=True)
        f = BASE / "mic-active"
        if active:
            f.touch()
        else:
            f.unlink(missing_ok=True)
    except Exception:
        pass


_level_next = 0.0


def publish_level(frame, quiet: bool) -> None:
    """Hand the HUDs a number to move to, about twenty-five times a second.

    Throttled to the envelope's own resolution rather than written per frame:
    thirty-one writes a second buys nothing the eye can see over twenty-five,
    and this runs between two syllables of somebody talking. The arithmetic
    sits behind the throttle for the same reason.
    """
    global _level_next
    now = time.time()
    if now < _level_next:
        return
    _level_next = now + LEVEL.STEP
    if quiet:
        LEVEL.publish(0.0)
        return
    rms = float(np.sqrt(np.mean(frame * frame)))
    LEVEL.publish(min(1.0, (rms * LEVEL_GAIN) ** LEVEL_CURVE) if rms > 0 else 0.0)


def set_stranded(why: str) -> None:
    """Publish "the microphone is on, but nothing you say is going anywhere".

    Conversation mode is not turned OFF when the session closes: you may open
    another one in a minute, and having to remember to switch listening back on
    is exactly the friction this mode exists to remove. So it holds, and this
    file is how the HUD knows to stop drawing a mode that looks like it works.
    """
    try:
        BASE.mkdir(parents=True, exist_ok=True)
        f = BASE / "listen-stranded"
        if why:
            f.write_text(why)
        else:
            f.unlink(missing_ok=True)
    except Exception:
        pass


def gated() -> bool:
    """Am I the one talking? With headphones there is no echo, but I would
    still hear myself.

    Reads the global speaker state on purpose: it does not matter WHICH session
    is talking, the room is the room."""
    try:
        st = json.loads((BASE / "state.json").read_text())
        if st.get("state") != "speaking":
            return False
        until = st.get("until", 0)
        return time.time() < until + GATE_TAIL_MS / 1000
    except Exception:
        return False


def run() -> None:
    dictate = _mod("dictate")
    from faster_whisper import WhisperModel

    log("loading models")
    try:
        vad, turn = Vad(), SmartTurn()
        asr = WhisperModel(
            ASR_MODEL, device="cpu", compute_type="int8", cpu_threads=os.cpu_count() or 4
        )
    except Exception as e:
        # An offline machine with nothing cached reaches here, and so does a
        # broken runtime. Saying so and stopping is a failure; a traceback
        # under a HUD that still reads "listening" is a mystery.
        log(f"cannot start: {e}")
        set_stranded("the speech model would not load")
        return
    log(f"listening (end of turn between {FLOOR_MS:.0f} and {CEIL_MS:.0f} ms)")
    next_check = time.time() + TARGET_CHECK_S
    stranded = ""
    set_stranded("")

    pre = deque(maxlen=int(PREROLL_MS / FRAME_MS))
    frames, speaking, silence_ms, asked = [], False, 0.0, 0.0

    for frame in capture():
        # The level goes out first, before any of the branches below can
        # `continue` past it. It is published on every path where the
        # microphone is genuinely open -- including stranded, where the
        # whole point is that talking to nothing must still LOOK like
        # talking -- and silenced only while I am the one making noise.
        mine = gated()
        publish_level(frame, mine)

        # Watch the session, not just the microphone. Waiting for an
        # undelivered sentence to notice the session is gone means talking to
        # nothing first -- and the HUD says "listening" the whole time, which
        # is the exact lie this is meant to stop. Polled on a clock rather
        # than per frame: it is a tmux query, not free.
        if time.time() >= next_check:
            next_check = time.time() + TARGET_CHECK_S
            if not _presence.open_now():
                # The window that started this is gone. Returning runs the
                # cleanup in main() and closes capture(), which is what
                # actually lets go of the microphone -- and it is asked here,
                # on a clock, because the HUD that dies badly is precisely the
                # one that never got to stop us on its way out.
                log("no window open: stopping, microphone closed")
                return
            ok, why = dictate.target_status()
            if ok and stranded:
                log("session is back: listening again")
                stranded = ""
                set_stranded("")
                # Start the sentence from here. The VAD is recurrent, and
                # half a phrase said while nobody was listening is not the
                # beginning of the first one that will be delivered.
                frames, speaking, silence_ms = [], False, 0.0
                pre.clear()
                vad.reset()
                set_speaking(False)
            elif not ok and why != stranded:
                log(f"stranded: {why} -- listening, but delivering nowhere")
                stranded = why
                set_stranded(why)

        if stranded:
            # Keep the VAD running and nothing else. Voice activity is what
            # tells the HUD you are mid-sentence, which is when "there is no
            # session" is worth saying; transcription is skipped, because the
            # result has nowhere to go.
            was, speaking = speaking, vad(frame) > ON
            if speaking != was:
                set_speaking(speaking)
            frames, silence_ms = [], 0.0
            pre.clear()
            continue

        if mine:
            # Discard anything half-captured and clear the recurrent state:
            # otherwise the first phrase after I speak comes out contaminated.
            frames, speaking, silence_ms = [], False, 0.0
            pre.clear()
            vad.reset()
            continue

        p = vad(frame)

        if not speaking:
            pre.append(frame)
            if p > ON:
                # `pre` already ends with this frame. Appending it again put
                # 32 ms of duplicated audio at the head of every utterance and
                # counted one frame too many toward the length cap.
                frames = list(pre)
                pre.clear()
                speaking, silence_ms, asked = True, 0.0, 0.0
                set_speaking(True)
            continue

        frames.append(frame)
        dur_s = len(frames) * FRAME_MS / 1000

        if p > OFF:
            silence_ms = 0.0
        else:
            silence_ms += FRAME_MS

        send = False
        why = ""
        if silence_ms >= CEIL_MS:
            send, why = True, "cap"
        elif silence_ms >= FLOOR_MS and silence_ms - asked >= 200:
            # Ask every 200 ms, not every frame: it costs 25 ms.
            asked = silence_ms
            audio = np.concatenate(frames)
            prob = turn.complete(audio)
            if prob >= COMPLETE:
                send, why = True, f"finished p={prob:.2f}"
        if dur_s >= MAX_UTT_S:
            send, why = True, "too long"

        if not send:
            continue

        audio = np.concatenate(frames)
        speech_ms = dur_s * 1000 - silence_ms
        frames, speaking, silence_ms = [], False, 0.0
        pre.clear()
        vad.reset()
        set_speaking(False)

        if speech_ms < MIN_SPEECH_MS:
            continue

        # One sentence that will not transcribe is one sentence lost, not the
        # end of the conversation. Unguarded, any ctranslate2 or ONNX error
        # left main() -- which catches only KeyboardInterrupt -- to end the
        # daemon with a traceback while the window still said "listening".
        # dictate.py already wraps the same work; this is that asymmetry
        # closed.
        try:
            segs, _ = asr.transcribe(
                audio,
                language=LANGUAGE,
                beam_size=5,
                initial_prompt=dictate.GLOSSARY or None,
                vad_filter=True,
                no_speech_threshold=0.6,
            )
            text = " ".join(s.text.strip() for s in segs).strip()
        except Exception as e:
            log(f"transcription failed: {e}")
            continue
        low = text.lower()
        if not text or any(h in low for h in HALLUCINATIONS):
            continue

        log(f"[{why}] {text}")
        if not dictate.deliver(text):
            # The poll above normally gets there first; this is the race where
            # the session closed while the sentence was being transcribed.
            stranded = dictate.target_status()[1] or "no Claude Code session"
            log(f"stranded: undelivered ({stranded})")
            set_stranded(stranded)


def check() -> None:
    log("checking")
    t0 = time.time()
    vad = Vad()
    log(f"Silero loaded in {time.time() - t0:.2f}s")
    t0 = time.time()
    turn = SmartTurn()
    log(f"smart-turn loaded in {time.time() - t0:.2f}s")

    frame = np.zeros(FRAME, dtype=np.float32)
    t0 = time.time()
    for _ in range(100):
        vad(frame)
    log(f"Silero: {(time.time() - t0) * 10:.3f} ms per 32 ms frame")

    audio = np.zeros(SR * 4, dtype=np.float32)
    t0 = time.time()
    for _ in range(10):
        turn.complete(audio)
    log(f"smart-turn: {(time.time() - t0) * 100:.1f} ms per decision")


def main() -> int:
    if sys.argv[1:2] == ["--check"]:
        check()
        return 0
    BASE.mkdir(parents=True, exist_ok=True)

    # Refuse before the microphone opens, not after the first sentence is
    # transcribed and thrown away: conversation mode would otherwise listen,
    # transcribe and discard indefinitely, looking exactly like a mode that is
    # working but never being answered.
    if not _presence.open_now():
        log("not starting: no window open")
        print("  no HUD open: conversation disabled")
        return 1

    ok, why = _mod("dictate").target_status()
    if not ok:
        log(f"not starting: {why}")
        print(f"  {why}: conversation disabled")
        return 1

    PIDFILE.write_text(str(os.getpid()))

    # Without this, SIGTERM kills the process instantly, capture()'s `finally`
    # never runs, and pw-record is orphaned with the microphone open -- which
    # is what left the HUD saying "listening" forever.
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))

    try:
        run()
    except KeyboardInterrupt:
        pass
    finally:
        PIDFILE.unlink(missing_ok=True)
        LEVEL.clear()
        set_speaking(False)
        set_stranded("")
        log("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
