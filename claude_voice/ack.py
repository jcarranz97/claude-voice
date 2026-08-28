#!/usr/bin/env python3
"""Contextual acknowledgement: say what was asked for, not a canned phrase.

  ack.py [--session <id>] [--dry-run] "<the prompt text>"

voice.py launches this from the UserPromptSubmit hook, in the background,
because a model call takes ~0.7 s and a slow hook stalls the whole session.

The trade: the pre-recorded acknowledgement played in 22 ms but always said the
same thing ("One moment"), which feels like a deaf robot. A small model takes
~0.7 s and says "Checking the disk space." That second buys an acknowledgement
that has something to do with what you asked.

If the model fails or takes too long, it falls back to the cached phrase:
something always sounds. A generic "one moment" beats an unexplained silence.

The call is shown the last few turns of the spoken log as well as the prompt
(ack.context). Handed one sentence and nothing else, it can only hand that
sentence back -- "try it again with the flag" becomes "Retrying with the flag",
which says nothing -- and a dictated word Whisper got wrong is repeated with
total confidence, because there is nothing to notice it against.

That history is not free: it is tokens sent on every prompt, in the one call
whose whole job is to arrive before the answer does. ack.context = 0 sends the
prompt alone, which is what this did before, and ack.timeout stays the backstop
either way. --dry-run prints the line and what it cost instead of speaking it,
which is how to compare the two.

The same call also decides whether to speak at all. "Hello, how are you" is
answered in the time an acknowledgement of it takes to play, so acknowledging
it means hearing two voices about one nothing -- the hook's and then the
answer's. Asked for it (ack.skip_quick), the model replies SILENT for anything
it could simply answer, and nothing is played: not the line, not the cached
phrase. The acknowledgement is for the turn that would otherwise open with a
minute of silence, and that is the only turn that now gets one.

A failure is not a decline. If the model errors or times out the cached phrase
still plays, because the reason it exists -- an unexplained silence is worse
than a vague line -- holds exactly when we could not ask.

Set ack.contextual = false to skip the model entirely and always use the cache.
"""

import json
import os
import random
import shutil
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import config as _config  # noqa: E402

CFG = _config.load()
BASE = _config.BASE

# A spoken line is short by construction, but a dictated one is bounded only by
# how long you held the key. History is here for the gist of the turn, so a
# monologue contributes its opening and no more.
MAX_LINE = 300

# What the model says when the prompt does not need acknowledging. It has to be
# distinguishable from a failure -- an empty string -- because the two want
# opposite things: a failure falls back to the cached phrase, a decline plays
# nothing. The word is the one the TTS instruction already uses for the same
# idea, and the Spanish for it is accepted because a preset writing its own
# instruction in Spanish will get it back in Spanish sooner or later.
SILENT = "SILENT"
_SILENT_WORDS = {"silent", "silencio"}


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


def _bare(text: str) -> str:
    """A line reduced to what was said, so punctuation is not a difference."""
    return "".join(c for c in text.lower() if c.isalnum())


def _drop_prompt(entries: list, prompt: str) -> list:
    """The history, minus the prompt being acknowledged.

    Whether it is in there at all depends on how the prompt arrived: dictation
    writes the spoken line in deliver(), before this hook fires, while a typed
    prompt is never written to the log at all. So it is removed if it is there
    rather than assumed to be absent -- otherwise a dictated prompt is put to
    the model twice and the turn that gives it its meaning falls off the end.

    Only the trailing `in` lines are candidates: anything said out loud since
    is not this prompt.
    """
    out = list(entries)
    for i in range(len(out) - 1, -1, -1):
        if out[i]["side"] != "in":
            break
        if _bare(out[i]["text"]) and _bare(out[i]["text"]) == _bare(prompt):
            del out[i]
            break
    return out


def history(prompt: str, session: str) -> list:
    """The last ack.context turns of this conversation, as messages.

    From the spoken log rather than the transcript, for the reason spokenlog.py
    exists: most of what is heard never reaches the transcript. The price is
    that the assistant's side is the line it SPOKE, a summary of an answer
    rather than the answer -- thin, but it is what the ear got, and this call
    cannot afford to read anything longer.

    A turn is one side speaking, so the narration lines of a single answer
    collapse into one message; the API is read the conversation, not the log.
    """
    try:
        n = int(CFG.get("ack.context", 6))
    except (TypeError, ValueError):
        n = 6
    if n <= 0 or not session:
        return []
    try:
        # Generously more lines than turns: an answer often speaks several
        # times, and the count that matters is taken after collapsing.
        entries = _mod("spokenlog").tail(n * 8, session)
    except Exception:
        return []  # history is a bonus, never a dependency
    msgs = []
    for e in _drop_prompt(entries, prompt):
        role = "user" if e["side"] == "in" else "assistant"
        text = e["text"].strip()[:MAX_LINE]
        if not text:
            continue
        if msgs and msgs[-1]["role"] == role:
            msgs[-1]["content"] = f"{msgs[-1]['content']} {text}"[: MAX_LINE * 3]
        else:
            msgs.append({"role": role, "content": text})
    msgs = msgs[-n:]
    while msgs and msgs[0]["role"] != "user":
        msgs.pop(0)  # the conversation starts on your side
    return msgs


def contextual(prompt: str, session: str = "") -> str:
    """An acknowledgement related to what was asked.

    SILENT if the prompt is one the answer will beat, and nothing should be
    said at all. Empty string on failure, which is a different thing: that
    falls back to the cached phrase, this plays nothing.
    """
    if not CFG.get("ack.contextual", True):
        return ""
    try:
        client = _client()
        if client is None:
            return ""
        max_words = int(CFG.get("ack.max_words", 9))
        past = history(prompt, session)
        messages = past + [{"role": "user", "content": prompt[:1500]}]
        if len(messages) > 1 and messages[-2]["role"] == "user":
            # The turn before drew no spoken line of its own -- the voice was
            # off, or it was still talking. One side, one message.
            messages[-1]["content"] = messages.pop(-2)["content"] + "\n" + messages[-1]["content"]
        system = CFG.get("ack.system", "") or ""
        if CFG.get("ack.skip_quick", True):
            # Appended before the history note, so the last thing said is
            # still how to read the messages that follow.
            quick = (CFG.get("ack.quick_system", "") or "").strip()
            system = f"{system.strip()}\n\n{quick}".strip() if quick else system
        if past:
            # What that history IS has to be said, or the spoken lines read as
            # full answers and the mis-transcriptions read as intended.
            note = (CFG.get("ack.context_system", "") or "").strip()
            system = f"{system.strip()}\n\n{note}".strip() if note else system
        r = client.messages.create(
            model=CFG.get("ack.model", "claude-haiku-4-5"),
            max_tokens=40,
            system=system,
            messages=messages,
        )
        text = "".join(b.text for b in r.content if b.type == "text").strip()
        text = text.strip('"“” ').replace("\n", " ")
        if _bare(text) in _SILENT_WORDS:
            return SILENT
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
    # Guarded, not merely existence-checked. This is the fallback that speaks
    # when the model could not be reached, so it is the one function here with
    # nothing underneath it to catch a stray IsADirectoryError.
    try:
        prev = last.read_text().strip()
    except Exception:
        prev = ""
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
    # Say the line instead of speaking it. This is the latency path, and the
    # only honest way to choose ack.context is to hear what each setting costs.
    dry = "--dry-run" in argv
    argv = [a for a in argv if a != "--dry-run"]
    # Whose acknowledgement this is. The queue is shared between windows, so
    # without it the HUD cannot tell whether the voice it hears is the session
    # it is watching. It is also whose history gets read.
    session = ""
    if len(argv) >= 2 and argv[0] == "--session":
        session, argv = argv[1], argv[2:]
    prompt = " ".join(argv).strip()

    if dry:
        log = _mod("spokenlog")
        # Run from a terminal, there is no hook to name the session, so the
        # reader's own policy answers it: the same conversation that
        # `claude-voice history` would print.
        session = session or log.follow(*log.target())
        turns = len(history(prompt, session))
        start = time.perf_counter()
        text = contextual(prompt, session) if prompt else ""
        ms = (time.perf_counter() - start) * 1000
        if text == SILENT:
            said = "(silent -- the answer beats an acknowledgement of it)"
        else:
            said = text or "(nothing said -- the cached phrase would play)"
        print(f"  {said}")
        print(
            f"  {ms:.0f} ms, {turns} turns of history"
            + (f", session {session[:8]}" if session else " -- no conversation to read")
        )
        return 0

    audioq = _mod("audioq")

    wav = None
    text = contextual(prompt, session) if prompt else ""
    if text == SILENT:
        return 0  # asked and answered: say nothing at all
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
        sys.exit(0)  # never break the session over an acknowledgement
