#!/usr/bin/env python3
"""Pronunciation workbench.

  pron.py diag <word>...     compare primary vs foreign phonemes, suggest a fix
  pron.py say  "<phrase>"    synthesize, sanity-check, and play at 60% volume
  pron.py list               show what is currently overridden

The workflow is deliberately ear-first. An automated phoneme diff cannot tell
you that "main" came out as two syllables -- both renderings are five phonemes.
You have to hear it. So: `say` it, and if it is wrong, `diag` it and paste the
suggested line into your config.
"""

import importlib.util
import struct
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import config as _config                              # noqa: E402

CFG = _config.load()

spec = importlib.util.spec_from_file_location("speak", HERE / "speak.py")
speak = importlib.util.module_from_spec(spec)
spec.loader.exec_module(speak)

from piper.phonemize_espeak import EspeakPhonemizer  # noqa: E402

ph = EspeakPhonemizer()


def phon(voice: str, text: str) -> str:
    return "".join("".join(s) for s in ph.phonemize(voice, text))


def diag(words) -> None:
    primary, foreign = CFG.primary_voice, CFG.foreign_voice
    terms, overrides = CFG.foreign_terms, CFG.overrides

    if not foreign:
        print(f"  tts.foreign_voice is unset — only {primary} is in play.")
        print("  Set it in your config if you want a second language spliced in.\n")

    print(f"  {'word':16} {primary:18} {foreign or '-':18} state")
    print("  " + "-" * 72)
    suggest_foreign, suggest_override = [], []
    for w in words:
        bare = w.lower().strip(speak.STRIP)
        es = phon(primary, bare)
        en = phon(foreign, bare) if foreign else "-"
        if bare in overrides:
            state = f"OVERRIDE -> {overrides[bare]}"
        elif bare in terms:
            state = f"already {foreign}"
        elif foreign and es == en:
            state = "identical, nothing to do"
        else:
            state = f"spoken as {primary} (candidate)"
            suggest_foreign.append(bare)
            suggest_override.append((bare, es))
        print(f"  {bare:16} {es:18} {en:18} {state}")

    print()
    print("  How to fix it:")
    print(f"   - if the '{foreign or 'foreign'}' column sounds right -> add to "
          "pronunciation.foreign_terms")
    print("   - if NEITHER sounds right                 -> add to "
          "pronunciation.overrides with hand-written IPA")
    print("   - if the primary language already says it the way you would, leave it")

    if suggest_foreign:
        print(f"\n  Paste into {_config.CONFIG}:\n")
        print("    [pronunciation]")
        print(f"    foreign_terms = {suggest_foreign!r}".replace("'", '"'))
        print("\n    [pronunciation.overrides]")
        for bare, es in suggest_override:
            print(f'    {bare} = "{es}"    # <- edit this IPA by ear')


def say(text: str) -> None:
    tmp = Path(tempfile.gettempdir()) / "cv-pron-test.wav"
    ipa = "".join(speak.mixed_phonemes(ph, text))
    print(f"  phonemes: {ipa}")
    if not speak.synthesize(text, tmp):
        print("  synthesis failed — is tts.voice_model present?")
        return

    # Sanity check the audio itself. A silent or full-scale-noise file means
    # the phoneme stream went wrong, and you want to know that before you
    # blame your ears.
    with wave.open(str(tmp)) as w:
        frames = w.readframes(w.getnframes())
        n = len(frames) // 2
        peak = max(abs(s) for s in struct.unpack(f"<{n}h", frames)) if n else 0
        secs = w.getnframes() / w.getframerate()
    print(f"  {secs:.2f}s, peak {peak}/32767")
    if peak < 1000:
        print("  (nearly silent — the phonemes probably did not resolve)")

    subprocess.run(["aplay", "-q", str(tmp)], check=False)


def show() -> None:
    terms, overrides = CFG.foreign_terms, CFG.overrides
    print(f"  primary voice : {CFG.primary_voice}")
    print(f"  foreign voice : {CFG.foreign_voice or '(none)'}")
    print(f"\n  {len(terms)} foreign terms")
    for t in sorted(terms):
        print(f"    {t}")
    print(f"\n  {len(overrides)} explicit overrides")
    for k, v in sorted(overrides.items()):
        print(f"    {k:16} {v}")


def main() -> int:
    cmd = sys.argv[1] if sys.argv[1:] else ""
    if cmd == "diag" and sys.argv[2:]:
        diag(sys.argv[2:])
    elif cmd == "say" and sys.argv[2:]:
        say(" ".join(sys.argv[2:]))
    elif cmd == "list":
        show()
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
