#!/usr/bin/env python3
"""What each session is doing -- one file per session, not one for everyone.

There are two different questions, and they used to share a single state.json:

  what is the SPEAKER doing   there is one pair of speakers, so this is global
                              and audioq.py owns it
  what is this SESSION doing  thinking, or done -- and with three windows open
                              plus a bot answering Telegram, three answers can
                              be true at once

Merging them meant any session finishing its turn wrote "ready" over a window
that was still working, and the HUD went calm while the work carried on. Worse,
the heartbeat and the acknowledgement shared one pidfile each, so a Stop hook
anywhere killed the tick of whoever was still thinking.

So: turn state lives here, keyed by session id, and the pidfiles are keyed the
same way. The HUD reads the session it is pointed at (the one `t` switches, the
one dictation goes to) and lays the global speaking state on top when the audio
belongs to that session.

  turn.py                 what every session is doing right now
  turn.py <session-id>    just that one
"""

import json
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import config as _config                              # noqa: E402

BASE = _config.BASE
PREFIX = "turn-"

# A turn file older than this is a session that died mid-thought: the Stop hook
# never fired and nobody cleared it. Swept on write, which is often enough and
# costs nothing anybody is waiting on.
STALE = 6 * 3600

IDLE = {"state": "idle", "text": "", "until": 0, "ts": 0, "session": ""}


def _safe(session: str) -> str:
    """Session ids are uuids, but never trust one straight into a filename."""
    return re.sub(r"[^A-Za-z0-9._-]", "", str(session or ""))[:64] or "default"


def path(session: str) -> Path:
    return BASE / f"{PREFIX}{_safe(session)}.json"


def write(session: str, state: str, text: str = "", secs: float = 0.0) -> None:
    """Publish one session's state. Best effort: never worth breaking a hook."""
    try:
        BASE.mkdir(parents=True, exist_ok=True)
        path(session).write_text(json.dumps({
            "state": state, "text": text,
            "until": time.time() + secs if secs else 0,
            "ts": time.time(), "session": session or "",
        }))
    except Exception:
        return
    sweep()


def read(session: str) -> dict:
    try:
        d = json.loads(path(session).read_text())
    except Exception:
        return dict(IDLE)
    d.setdefault("session", session or "")
    return d


def newest() -> dict:
    """The most recently touched session.

    The fallback for when the HUD cannot tell which session it is looking at --
    outside tmux, or before a pane title has been set. Showing the liveliest
    session is a guess, but it is the guess the HUD made for everything before
    this file existed, so nothing gets worse when the session is unknown.
    """
    best, best_ts = dict(IDLE), -1.0
    for p in files():
        try:
            d = json.loads(p.read_text())
        except Exception:
            continue
        if float(d.get("ts", 0)) > best_ts:
            best, best_ts = d, float(d.get("ts", 0))
    return best


def files() -> list:
    try:
        return sorted(BASE.glob(f"{PREFIX}*.json"))
    except Exception:
        return []


def sessions() -> list:
    """Every session id we have ever written state for."""
    return [p.stem[len(PREFIX):] for p in files()]


def sweep(max_age: float = STALE) -> None:
    now = time.time()
    for p in files():
        try:
            if now - p.stat().st_mtime > max_age:
                p.unlink(missing_ok=True)
        except OSError:
            pass


def pidfile(kind: str, session: str) -> Path:
    """`thinking` and `ack` pidfiles, one per session.

    kind is the old filename stem, so the legacy global name is still what you
    get when there is no session to key on -- a session id is not guaranteed
    (the CLI has none), and orphan sweeping has to be able to find those too.
    """
    return BASE / f"{kind}-{_safe(session)}.pid" if session else BASE / f"{kind}.pid"


def pidfiles(kind: str) -> list:
    """Every pidfile of that kind, legacy name included. For the panic button."""
    out = []
    try:
        out = sorted(BASE.glob(f"{kind}-*.pid"))
    except Exception:
        pass
    legacy = BASE / f"{kind}.pid"
    if legacy.exists():
        out.append(legacy)
    return out


def main() -> int:
    if sys.argv[1:]:
        d = read(sys.argv[1])
        print(f"  {sys.argv[1][:8]}  {d['state']}")
        return 0
    rows = [(s, read(s)) for s in sessions()]
    if not rows:
        print("  no session has reported anything yet")
        return 0
    now = time.time()
    for sid, d in sorted(rows, key=lambda r: -r[1].get("ts", 0)):
        ago = now - d.get("ts", 0)
        print(f"  {sid[:8]}  {d['state']:<10} {ago:6.0f}s ago  {d.get('text','')[:40]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
