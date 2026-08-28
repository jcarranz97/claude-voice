#!/usr/bin/env python3
"""Live narration: speak the progress, not just the final answer.

Hooks into MessageDisplay, which fires for every assistant text block --
including the "committed that", "now checking the tests" lines that land
between tool calls. The Stop hook only speaks at the end; this fills the
silence in the middle.

The rules that make it bearable instead of insufferable:
  - up to `narrate.word_limit` words is spoken WHOLE; longer gets a lead-in
  - no code, tables or hashes; paths collapse to the filename
  - the block carrying the TTS marker is left to the Stop hook
  - at most `narrate.max_per_turn`, spaced from when the previous one ENDS
  - never repeats something already said this turn

Tunable without touching code:  narrate.py --tune <words> <max_per_turn>
"""

import hashlib
import json
import os
import re
import sys
import tempfile
import time
import wave
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import config as _config  # noqa: E402

CFG = _config.load()
BASE = _config.BASE
TUNE = BASE / "narrate.json"


def cfg() -> tuple:
    """Runtime tuning wins over the config file, which wins over defaults.

    A short notice is spoken WHOLE: cutting at the first sentence drops exactly
    the useful part ("...now I'll check the tests"). Only above the limit is it
    trimmed to a lead-in, which is when hearing all of it would be a chore.
    """
    limit = int(CFG.get("narrate.word_limit", 50))
    per_turn = int(CFG.get("narrate.max_per_turn", 12))
    try:
        d = json.loads(TUNE.read_text())
        return int(d.get("word_limit", limit)), int(d.get("max_per_turn", per_turn))
    except Exception:
        return limit, per_turn


MIN_WORDS = int(CFG.get("narrate.min_words", 3))


def _mod(name: str):
    import importlib.util

    spec = importlib.util.spec_from_file_location(name, HERE / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def find_text(data: dict) -> str:
    """The MessageDisplay payload is undocumented: try several names, and fall
    back to the last text block in the transcript."""
    for key in (
        "message",
        "text",
        "content",
        "assistant_message",
        "display_text",
        "last_assistant_message",
    ):
        v = data.get(key)
        if isinstance(v, str) and v.strip():
            return v
        if isinstance(v, dict):
            c = v.get("content") or v.get("text")
            if isinstance(c, str) and c.strip():
                return c

    tp = data.get("transcript_path")
    if not tp or not Path(tp).exists():
        return ""
    try:
        # only the tail: the transcript can be megabytes
        with open(tp, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 200_000))
            lines = f.read().decode("utf-8", "ignore").splitlines()
        for line in reversed(lines):
            try:
                e = json.loads(line)
            except Exception:
                continue
            msg = e.get("message") or {}
            if msg.get("role") != "assistant":
                continue
            content = msg.get("content")
            if isinstance(content, list):
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "text":
                        return b.get("text", "")
            elif isinstance(content, str):
                return content
    except Exception:
        pass
    return ""


def speakable(raw: str) -> str:
    """Clean, sayable text. Empty string if it is not worth saying."""
    if not raw or "<!-- TTS:" in raw:
        return ""  # that one belongs to the Stop hook
    t = re.sub(r"```.*?```", " ", raw, flags=re.DOTALL)  # code blocks
    t = re.sub(r"`[^`]+`", " ", t)  # inline code
    t = re.sub(r"<!--.*?-->", " ", t, flags=re.DOTALL)
    # Whole table rows: reading them aloud is incomprehensible.
    t = "\n".join(ln for ln in t.splitlines() if ln.count("|") < 2)
    t = re.sub(r"^\s*[|>#\-*\d.]+\s*", "", t, flags=re.MULTILINE)  # lists, quotes
    t = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", t)  # links
    t = re.sub(r"[*_~]", "", t)
    # Paths -> just the filename. Deleting them outright left broken sentences
    # ("The commit touched and landed on main"); the basename informs and reads
    # out loud fine.
    t = re.sub(r"(?:[\w.~-]*/)+([\w.-]+)", r"\1", t)
    t = re.sub(r"\b[0-9a-f]{7,}\b", "", t)  # hashes: unpronounceable
    t = re.sub(r"\s+([,.])", r"\1", t)  # tidy the gap they leave
    t = " ".join(t.split())
    # "Let me check the config:" introduces something we are not going to read.
    # Dropping the colon leaves a useful sentence instead of discarding it.
    t = t.rstrip(":").strip()

    words = t.split()
    if len(words) < MIN_WORDS:
        return ""  # "Ok." / "Done:" -- noise, not information

    limit, _ = cfg()
    if len(words) <= limit:
        return t  # short: spoken WHOLE

    # Long: the first two sentences as a lead-in. Hearing 300 words in one go
    # does not help, and the Stop hook already delivers the conclusion.
    sentences = re.findall(r"[^.!?]+[.!?]", t) or [t]
    lead = " ".join(s.strip() for s in sentences[:2]).strip()
    return lead[:400] if len(lead.split()) >= MIN_WORDS else " ".join(words[:40])


def turn_state(prompt_id: str) -> Path:
    return BASE / f"narr-{hashlib.sha1((prompt_id or 'x').encode()).hexdigest()[:12]}.json"


def main() -> int:
    if not CFG.get("narrate.enabled", True):
        return 0
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0

    speak = _mod("speak")
    sid = data.get("session_id", "")
    if not speak.enabled(sid) or not speak.audio_available():
        return 0

    text = speakable(find_text(data))
    if not text:
        return 0

    st_path = turn_state(data.get("prompt_id", ""))
    try:
        st = json.loads(st_path.read_text())
    except Exception:
        st = {"n": 0, "last": 0, "said": []}

    _, max_turn = cfg()
    h = hashlib.sha1(text.encode()).hexdigest()[:10]
    # st["last"] records when the previous notice FINISHES playing.
    if st["n"] >= max_turn or h in st["said"]:
        return 0

    tmp = Path(tempfile.gettempdir()) / f"cv-narr-{os.getpid()}.wav"
    if not speak.synthesize(text, tmp):
        return 0

    with wave.open(str(tmp)) as w:
        secs = w.getnframes() / w.getframerate()
    # the queue guarantees order and turn-taking; the session says whose turn
    _mod("audioq").enqueue(tmp, text, session=sid)

    st.update(n=st["n"] + 1, last=time.time() + secs)
    st["said"] = (st["said"] + [h])[-20:]
    try:
        BASE.mkdir(parents=True, exist_ok=True)
        st_path.write_text(json.dumps(st))
    except Exception:
        pass
    return 0


def tune(limit: int, per_turn: int) -> None:
    TUNE.parent.mkdir(parents=True, exist_ok=True)
    TUNE.write_text(json.dumps({"word_limit": limit, "max_per_turn": per_turn}))
    print(
        f"  whole up to {limit} words (~{limit * 0.5:.0f}s of audio), "
        f"at most {per_turn} notices per turn"
    )


if __name__ == "__main__":
    try:
        if sys.argv[1:2] == ["--tune"] and len(sys.argv) > 2:
            tune(int(sys.argv[2]), int(sys.argv[3]) if len(sys.argv) > 3 else 12)
            sys.exit(0)
        if sys.argv[1:2] == ["--show"]:
            l, m = cfg()
            print(f"  whole up to {l} words   ·   at most {m} per turn")
            sys.exit(0)
        sys.exit(main())
    except Exception:
        sys.exit(0)
