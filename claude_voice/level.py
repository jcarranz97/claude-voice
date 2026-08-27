#!/usr/bin/env python3
"""How loud the voice is right now -- for the mouth and for the ear.

The reactor moved the same way whether a line was two words or forty: the
state picked an animation and the animation ran on a clock. It read as a
screensaver that had been told a colour. This is the number it was missing.

Two directions, and they are not the same problem.

  the mouth  The whole utterance is a finished file before a sample of it is
             played, so its shape is known in advance. The player publishes a
             coarse envelope with the moment playback started, and every HUD
             indexes it by the clock. Nothing is streamed, nothing can drift,
             and a window opened mid-sentence catches up at the right syllable.

  the ear    Nothing is known in advance, so the level is published as it is
             heard: one small file holding one number, rewritten about
             twenty-five times a second. A reader that catches a torn write
             fails to parse it and shows the previous value one frame longer,
             which is invisible at this rate. That is why it is a bare float
             and not JSON -- there is nothing to half-read but the digits.

Both are advisory. A HUD that cannot read either still animates on its clock;
it just animates blind, which is where this started.
"""

import sys
import time
import wave
from array import array
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config as _config                              # noqa: E402

BASE = _config.BASE
LIVE = BASE / "mic-level"

# One value per 40 ms, for both directions. Slow enough that a ten-second
# answer is still a couple of hundred numbers in a state file, fast enough
# that a syllable is two or three of them rather than one.
STEP = 0.04

# The ear stops publishing when it stops listening, and a file is not a
# heartbeat: anything older than this is read as silence rather than as the
# last thing that happened to be true.
STALE = 0.5

# aplay has to start and open the device before the first sample leaves it.
# The envelope is therefore played slightly behind the clock -- a mouth that
# moves a moment late reads as a mouth; one that moves early reads as a fault.
LEAD = 0.09


def envelope(path: Path) -> tuple:
    """(seconds, levels) for a WAV, as one 0-100 integer per STEP.

    Peak per bucket rather than RMS: speech is mostly gaps, and the peak is
    what the eye reads as a syllable. The curve afterwards lifts the quiet
    parts, because a consonant that is genuinely a tenth of a vowel still
    has to be visible next to one.

    Normalised per utterance. Piper's output level barely varies, so this
    costs nothing real, and it means a line synthesised quietly still fills
    the reactor rather than looking like a fault.
    """
    with wave.open(str(path)) as w:
        rate, width, chans = w.getframerate(), w.getsampwidth(), w.getnchannels()
        secs = w.getnframes() / rate if rate else 0.0
        if width != 2 or not rate:
            return secs, []                  # 16-bit is what we synthesise
        pcm = array("h")
        pcm.frombytes(w.readframes(w.getnframes()))
    if sys.byteorder == "big":
        pcm.byteswap()                       # the WAV is little-endian

    per = max(1, int(rate * STEP)) * chans
    peaks = []
    for i in range(0, len(pcm), per):
        chunk = pcm[i:i + per]
        if not chunk:
            break
        # max() and min() over an array run in C; abs(min) catches the
        # negative half, which for speech is usually the louder one.
        peaks.append(max(max(chunk), -min(chunk)) / 32768.0)

    top = max(peaks, default=0.0)
    if top <= 0:
        return secs, []
    return secs, [round(100 * (p / top) ** 0.6) for p in peaks]


def at(env, t0: float, step: float = STEP, now: float = 0.0) -> float:
    """The envelope's value, 0..1, at the wall clock -- linearly between
    samples, so 25 numbers a second do not come out as 25 steps a second."""
    if not env or not t0:
        return 0.0
    n = len(env)
    x = ((now or time.time()) - t0 - LEAD) / (step or STEP)
    # Outside the line there is silence, not the nearest value held: a mouth
    # left open is worse than one that is late. The step of slack at the
    # front is the lead itself -- ask a shade too early and you get the
    # first sample rather than a dropped syllable.
    if x <= -1 or x >= n:
        return 0.0
    x = max(0.0, min(n - 1.0, x))
    i = int(x)
    if i >= n - 1:
        return env[n - 1] / 100.0
    a, b = env[i] / 100.0, env[i + 1] / 100.0
    return a + (b - a) * (x - i)


def publish(v: float) -> None:
    """The ear's level, right now. Never allowed to cost a frame of audio."""
    try:
        BASE.mkdir(parents=True, exist_ok=True)
        LIVE.write_text(f"{max(0.0, min(1.0, v)):.3f}")
    except Exception:
        pass


def clear() -> None:
    try:
        LIVE.unlink(missing_ok=True)
    except Exception:
        pass


def publishing() -> bool:
    """Is anything writing a level at all? Distinct from a level of zero:
    silence in an open room is a number, and a closed room is not. A front
    end asks this to decide whether to bother watching closely."""
    try:
        return time.time() - LIVE.stat().st_mtime <= STALE
    except Exception:
        return False


def live() -> float:
    """What the microphone is hearing, or 0.0 if nobody is publishing."""
    try:
        if time.time() - LIVE.stat().st_mtime > STALE:
            return 0.0
        return max(0.0, min(1.0, float(LIVE.read_text())))
    except Exception:
        return 0.0
