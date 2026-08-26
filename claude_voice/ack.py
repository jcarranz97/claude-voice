#!/usr/bin/env python3
"""Contextual acknowledgement: say what was asked for, not a canned phrase.

  ack.py [--session <id>] "<the prompt text>"

voice.py launches this from the UserPromptSubmit hook, in the background,
because a model call takes ~0.7 s and a slow hook stalls the whole session.

The trade: the pre-recorded acknowledgement played in 22 ms but always said the
same thing ("One moment"), which feels like a deaf robot. A small model takes
~0.7 s and says "Checking the disk space." That second buys an acknowledgement
that has something to do with what you asked.

If the model fails or takes too long, it falls back to the cached phrase:
something always sounds. A generic "one moment" beats an unexplained silence.

Set ack.contextual = false to skip the model entirely and always use the cache.
"""

import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import config as _config                              # noqa: E402

CFG = _config.load()
BASE = _config.BASE


def _mod(name: str):
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, HERE / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _client():
    """Whatever credential this machine has. API key first, then the Claude
    Code OAuth token that is already on disk if you are logged in."""
    import anthropic
    timeout = float(CFG.get("ack.timeout", 3.0))
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        return anthropic.Anthropic(api_key=key, timeout=timeout, max_retries=0)
    creds = json.loads((Path.home() / ".claude/.credentials.json").read_text())
    token = creds.get("claudeAiOauth", {}).get("accessToken")
    if not token:
        return None
    return anthropic.Anthropic(auth_token=token, timeout=timeout, max_retries=0)


def contextual(prompt: str) -> str:
    """An acknowledgement related to what was asked. Empty string on failure."""
    if not CFG.get("ack.contextual", True):
        return ""
    try:
        client = _client()
        if client is None:
            return ""
        max_words = int(CFG.get("ack.max_words", 9))
        r = client.messages.create(
            model=CFG.get("ack.model", "claude-haiku-4-5"),
            max_tokens=40,
            system=CFG.get("ack.system", "") or "",
            messages=[{"role": "user", "content": prompt[:1500]}],
        )
        text = "".join(b.text for b in r.content if b.type == "text").strip()
        text = text.strip('"“” ').replace("\n", " ")
        # A long acknowledgement stops being an acknowledgement.
        if not text or len(text.split()) > max_words + 4:
            return ""
        return text
    except Exception:
        return ""


def generic() -> tuple:
    """The cached phrase, as a safety net: (wav, what it says) or (None, "")."""
    voice = _mod("voice")
    # Per preset: the cache is indexed by position, so the other language's
    # wavs here would say one thing and log another.
    files = sorted(voice.ack_dir().glob("*.wav"))
    if not files:
        return None, ""
    last = BASE / "last-ack"
    prev = last.read_text().strip() if last.exists() else ""
    pick = random.choice([f for f in files if f.name != prev] or files)
    try:
        last.write_text(pick.name)
    except Exception:
        pass
    tmp = Path(tempfile.gettempdir()) / f"cv-ack-{id(pick)}.wav"
    shutil.copy(pick, tmp)
    # The words matter as much as the sound: the spoken log records what was
    # heard, and a cached phrase is heard like any other.
    return tmp, voice.ack_phrase(pick.name)


def main() -> int:
    argv = sys.argv[1:]
    # Whose acknowledgement this is. The queue is shared between windows, so
    # without it the HUD cannot tell whether the voice it hears is the session
    # it is watching.
    session = ""
    if len(argv) >= 2 and argv[0] == "--session":
        session, argv = argv[1], argv[2:]
    prompt = " ".join(argv).strip()
    audioq = _mod("audioq")

    wav = None
    text = contextual(prompt) if prompt else ""
    if text:
        speak = _mod("speak")
        cand = Path(tempfile.gettempdir()) / f"cv-ack-ctx-{abs(hash(text)) % 10**8}.wav"
        if speak.synthesize(text, cand):
            wav = cand

    if wav is None:
        wav, text = generic()
    if wav is None:
        return 0

    audioq.enqueue(wav, text, session=session)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)          # never break the session over an acknowledgement
