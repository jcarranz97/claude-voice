#!/usr/bin/env python3
"""The voice: speak one short line through Piper on the desktop.

Two ways in:
  Stop hook  -- reads the hook JSON on stdin, speaks only the <!-- TTS: --> line
  CLI        -- speak.py "text to say"

Design decisions worth keeping:

* We speak ONLY the model's own <!-- TTS: --> summary, never the response body.
  Every project in this space started by narrating everything and walked it
  back: markdown, diffs and file paths are unlistenable, and nobody says "slash
  home". No marker in the response means silence. That is the correct default.

* Foreign technical terms can be phonemized with a second espeak voice and
  spliced into the primary phoneme stream. espeak takes one language per
  utterance, so without this "merge" becomes MER-je in Spanish and "queue"
  becomes KE-u-e. Piper voices share one IPA alphabet, so the foreign phonemes
  are all in vocabulary; the acoustic model never trained on them, so they come
  out accented -- which is exactly how a bilingual developer says them.
  Configure it with tts.foreign_voice; blank disables the whole pass.

* Fails silent, always. A broken voice must never break coding.
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import config as _config  # noqa: E402
import focus as _focus  # noqa: E402
import presence as _presence  # noqa: E402

CFG = _config.load()
BASE = _config.BASE

STRIP = ".,;:¿?¡!()\"'…"


def audio_available() -> bool:
    """False on a headless box: a systemd service has no PipeWire session."""
    rt = os.environ.get("XDG_RUNTIME_DIR")
    if not rt:
        return False
    return any((Path(rt) / s).exists() for s in ("pipewire-0", "pulse/native"))


def _turn():
    import importlib.util

    spec = importlib.util.spec_from_file_location("turn", HERE / "turn.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def set_state(session: str, state: str, text: str = "", secs: float = 0.0) -> None:
    """Publish this session's state for the HUD. Best effort.

    Per session, because it used to be one file for all of them and the last
    window to finish spoke for everybody: three sessions open, one answers, and
    the HUD went calm over two that were still working.
    """
    try:
        _turn().write(session, state, text, secs)
    except Exception:
        pass


def stop_background(session: str = "") -> None:
    """Kill the heartbeat and the acknowledgement. Called at the END of every
    turn, even when there is nothing to say -- otherwise the tick keeps
    ticking into an empty room.

    Only this session's. The pidfiles used to be shared, so a Stop hook here
    cut the tick of a window that was still thinking -- and with a bot
    answering messages on the same machine, that fired all day.
    """
    turn = _turn()
    for kind, group in (("thinking", True), ("ack", False)):
        for pidfile in (
            {turn.pidfile(kind, session), turn.pidfile(kind, "")}
            if session
            else {turn.pidfile(kind, "")}
        ):
            try:
                if not pidfile.exists():
                    continue
                pid = int(pidfile.read_text().strip())
                os.killpg(pid, 15) if group else os.kill(pid, 15)
            except (ProcessLookupError, ValueError, PermissionError, OSError):
                pass
            finally:
                try:
                    pidfile.unlink(missing_ok=True)
                except Exception:
                    pass


def session_mute(session_id: str) -> Path:
    return Path(tempfile.gettempdir()) / f"cv-mute-{session_id or 'default'}"


def enabled(session_id: str) -> bool:
    """Off by default, and four things have to agree before a sound is made.

    An open HUD comes first, and it is a different kind of question from the
    others: the switch, the mute and the focus decide what you want to hear,
    while this one decides whether anything of ours is running at all.

    `voice on` flips the switch for the machine; a per-session file mutes one
    window; a focus, when there is one, silences every window except the one
    it names. The last is the answer to several sessions talking over each
    other -- see focus.py for why it is filed under the pane.
    """
    if not _presence.open_now():
        # Before the switch, because this one is not about what you want to
        # hear: with no window open, nothing of ours runs at all.
        return False
    if not (BASE / "enabled").exists():
        return False
    if session_mute(session_id).exists():
        return False
    return _focus.allows(session_id)


def extract_spoken(text: str) -> str:
    """Pull the model's own spoken line out of the response. Silence if absent."""
    if not text:
        return ""
    # LAST match, not the first. When the response talks *about* the voice --
    # documentation, a bug report, this very file -- it quotes the marker in
    # prose, and re.search would speak the example instead of the summary. An
    # inline `<!-- TTS: -->` used as a noun even extracts to empty, so the turn
    # goes silent with no clue why. The real marker is the last one by
    # construction: the injected instruction says to end the response with it.
    found = re.findall(r"<!--\s*TTS:\s*(.*?)\s*-->", text, re.DOTALL | re.IGNORECASE)
    if not found:
        return ""
    line = " ".join(found[-1].split())
    if line.upper() in ("SILENT", "NONE", "SKIP"):
        return ""
    return line[: int(CFG.get("tts.max_chars", 400))]


def mixed_phonemes(ph, text: str, cfg=None) -> list:
    """Primary-language phonemes for the whole line, foreign words swapped in.

    Phonemizing span-by-span looks obvious and is wrong twice over: the space
    between spans is lost so words run together, and espeak stresses the last
    word of every fragment, so articles come out accented.

    Instead phonemize the whole line once in the primary voice -- correct
    prosody, correct word boundaries -- then replace only the individual words
    that G2P mangles. espeak emits ' ' between words, so the segments align 1:1
    with text.split(), which is what makes the substitution safe.
    """
    cfg = cfg or CFG
    primary = cfg.primary_voice
    # espeak splits on sentence boundaries and emits no ' ' between them, so
    # join with one or the segment count drifts from the word count and the
    # whole substitution silently falls back to the plain primary language.
    sentences = ph.phonemize(primary, text)
    flat = []
    for i, s in enumerate(sentences):
        if i:
            flat.append(" ")
        flat.extend(s)

    foreign = cfg.foreign_voice
    terms, overrides = cfg.foreign_terms, cfg.overrides
    if not overrides and not (foreign and terms):
        return flat  # single-language setup: nothing to do

    segments, cur = [], []
    for p in flat:
        if p == " ":
            segments.append(cur)
            cur = []
        else:
            cur.append(p)
    segments.append(cur)

    words = text.split()
    if len(words) != len(segments):
        return flat  # alignment broke -- fall back to plain, never garble

    for i, word in enumerate(words):
        bare = word.lower().strip(STRIP)

        # Tier 1: explicit override wins over everything.
        if bare in overrides:
            trailing = [c for c in word if c in STRIP]
            segments[i] = list(overrides[bare]) + trailing
            continue

        # Tier 2: pronounce it in the foreign voice.
        if not (foreign and terms):
            continue
        if bare not in terms:
            # "logs", "hooks", "branches" -- try the singular before giving up
            # removesuffix, not rstrip: rstrip takes EVERY trailing s, so a
            # term like "css" was looked up as "c".
            singular = bare[:-2] if bare.endswith("es") else bare.removesuffix("s")
            if singular in terms and singular != bare:
                bare = singular + ("es" if word.lower().strip(STRIP).endswith("es") else "s")
            else:
                continue
        en = [p for s in ph.phonemize(foreign, bare) for p in s if p != " "]
        if not en:
            continue
        trailing = [c for c in word if c in STRIP]  # keep the comma/period
        segments[i] = en + trailing

    out = []
    for i, seg in enumerate(segments):
        if i:
            out.append(" ")
        out.extend(seg)
    return out


def synthesize(text: str, path: Path, cfg=None) -> bool:
    """Speak `text` into `path`. A cfg other than the active one is how the
    language switch warms the other preset's cache without adopting it."""
    from piper import PiperVoice, SynthesisConfig
    from piper.phonemize_espeak import EspeakPhonemizer

    cfg = cfg or CFG
    model = cfg.voice_model
    if not model.exists():
        return False

    voice = PiperVoice.load(str(model))
    ph = EspeakPhonemizer()

    phonemes = mixed_phonemes(ph, text, cfg=cfg)
    if not phonemes:
        return False

    ids = voice.phonemes_to_ids(phonemes)
    audio = voice.phoneme_ids_to_audio(ids, SynthesisConfig(length_scale=cfg.length_scale))

    # phoneme_ids_to_audio returns float32 normalized to -1..1, NOT int16.
    # Writing those bytes straight into a 16-bit WAV yields full-scale white
    # noise. Convert explicitly, and clip so a hot sample can't wrap around
    # into a click.
    import numpy as np

    pcm = np.clip(audio, -1.0, 1.0)
    pcm = (pcm * 32767.0).astype("<i2")

    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(voice.config.sample_rate)
        w.writeframes(pcm.tobytes())
    return True


def play_detached(path: Path) -> None:
    """Detach so the hook returns immediately -- a blocking hook stalls Claude.

    No -f/-r flags: the WAV header carries the real format, and overriding it
    is how you end up playing a header as audio at the wrong rate.
    """
    subprocess.Popen(
        ["aplay", "-q", str(path)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def main() -> int:
    session_id = ""

    if sys.argv[1:]:
        text = " ".join(sys.argv[1:])
    else:
        try:
            data = json.load(sys.stdin)
        except Exception:
            return 0
        session_id = data.get("session_id", "")
        # First thing, whatever happens next -- but only for this session.
        stop_background(session_id)
        if data.get("stop_hook_active"):
            return 0
        # Docs are explicit: use last_assistant_message, not the transcript file.
        raw = data.get("last_assistant_message", "")
        text = extract_spoken(raw)
        # One line per Stop, so "why didn't it talk?" is answerable after the
        # fact instead of guessed at.
        try:
            log = BASE / "speak.log"
            log.parent.mkdir(parents=True, exist_ok=True)
            with log.open("a") as f:
                f.write(
                    f"{__import__('datetime').datetime.now():%F %T} "
                    f"fields={sorted(data)} len={len(raw)} "
                    f"marker={'yes' if text else 'NO'} "
                    f"on={enabled(session_id)} audio={audio_available()}\n"
                )
        except Exception:
            pass

    # CLI use bypasses the switch on purpose -- `speak.py "..."` is for testing.
    if sys.argv[1:]:
        pass
    elif not enabled(session_id):
        return 0

    if not text or not audio_available():
        set_state(session_id, "idle")
        return 0

    tmp = Path(tempfile.gettempdir()) / f"cv-speak-{os.getpid()}.wav"
    if synthesize(text, tmp):
        import importlib.util

        spec = importlib.util.spec_from_file_location("audioq", HERE / "audioq.py")
        audioq = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(audioq)
        # flush_pending: progress notes that never played are worthless now --
        # the work is done. The conclusion replaces them.
        audioq.enqueue(tmp, text, flush_pending=True, session=session_id)
        # The turn is over even while the answer is still being read out: the
        # speaker has its own state, and the HUD lays it over this one.
        set_state(session_id, "ready")
    else:
        set_state(session_id, "idle")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)  # never break the session
