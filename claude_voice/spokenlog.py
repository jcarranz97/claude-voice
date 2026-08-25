#!/usr/bin/env python3
"""The spoken log: what was actually said out loud, in the order it was heard.

  spokenlog.py [n]     print the last n spoken lines (default 40)

Why this exists
---------------
The transcript is the wrong place to read the conversation from, because most
of what you HEAR never reaches it: narration is derived on the fly, the
contextual acknowledgement is a separate model call, and a dictated sentence
arrives as an ordinary user message, indistinguishable from something typed.
Only the final `<!-- TTS: -->` line survives. A reader built on the transcript
would show the endings and silently drop everything in between.

So both sides write here instead, at the point where a line becomes sound:

  out   audioq.enqueue()   -- every spoken thing passes through it, in the
                              exact order it is played
  in    dictate.deliver()  -- the one place that knows a sentence was SPOKEN
                              rather than typed (conversation mode goes
                              through it too)

Rules
-----
* Appending must not slow a hook: one line, opened in append mode, never a
  rewrite. Trimming happens only when the file has grown past twice its cap,
  which is once every few hundred lines.
* Fails silent, in both directions. A log that cannot be written must not
  break the voice, and one that cannot be read must not break the HUD.
"""

import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import config as _config                              # noqa: E402

CFG = _config.load()
BASE = _config.BASE
LOG = BASE / "spoken.jsonl"

# Rough bytes per entry, used to decide when trimming is worth the read. Only
# the trigger depends on it being right; the trim itself counts real lines.
_AVG = 160


def cap() -> int:
    try:
        return max(20, int(CFG.get("history.cap", 400)))
    except Exception:
        return 400


def record(side: str, text: str) -> None:
    """Append one spoken line. `side` is "out" (heard) or "in" (said)."""
    text = (text or "").strip()
    if not text or not CFG.get("history.enabled", True):
        return
    try:
        BASE.mkdir(parents=True, exist_ok=True)
        line = json.dumps({"t": time.time(),
                           "side": "in" if side == "in" else "out",
                           "text": text}, ensure_ascii=False)
        with LOG.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        if LOG.stat().st_size > cap() * _AVG * 2:
            _trim()
    except Exception:
        pass


def _trim() -> None:
    """Keep the last `cap` entries. Written aside and renamed, so a reader
    never sees a half-written log."""
    try:
        lines = LOG.read_text(encoding="utf-8").splitlines()[-cap():]
        tmp = LOG.with_suffix(".jsonl.tmp")
        tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.replace(tmp, LOG)
    except Exception:
        pass


def tail(n: int = 200) -> list:
    """The last n entries, oldest first. Empty list if anything is wrong."""
    try:
        raw = LOG.read_text(encoding="utf-8").splitlines()
    except Exception:
        return []
    out = []
    for line in raw[-max(1, n):]:
        try:
            d = json.loads(line)
            if d.get("text"):
                out.append({"t": float(d.get("t", 0)),
                            "side": "in" if d.get("side") == "in" else "out",
                            "text": str(d["text"])})
        except Exception:
            continue          # one malformed line must not lose the rest
    return out


def mtime() -> float:
    """When the log last changed, so readers can cache until it does.

    ctime counts as a change: a log that becomes readable again after its
    permissions were fixed is new to a reader, even though its contents are
    not, and a stamp that ignored that would leave the pane empty until the
    next spoken line.
    """
    try:
        st = LOG.stat()
        return max(st.st_mtime, st.st_ctime)
    except Exception:
        return 0.0


def main() -> int:
    try:
        n = int(sys.argv[1])
    except (IndexError, ValueError):
        n = 40
    entries = tail(n)
    if not entries:
        print(f"  nothing spoken yet ({LOG})")
        return 0
    for e in entries:
        when = time.strftime("%H:%M", time.localtime(e["t"])) if e["t"] else "     "
        print(f'  {when}  {"you  ›" if e["side"] == "in" else "said ‹"} {e["text"]}')
    return 0


if __name__ == "__main__":
    sys.exit(main())
