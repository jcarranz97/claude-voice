#!/usr/bin/env python3
"""Which pane owns the voice, when several sessions are open at once.

The switch is all-or-nothing, and with three windows open that is the wrong
grain: turning it off to stop two of them also silences the one you were
listening to. Focus is the other half of it. While a pane is focused, only the
session living in that pane speaks, and every other session behaves exactly as
though the voice were off -- without the voice being off, and without touching
anything in those windows.

It is filed under the TERMINAL, not under the session, and that is the whole
design. A session closed and reopened is a NEW uuid in the same terminal, so a
focus filed under the uuid would point at a conversation that no longer exists
the moment the window is restarted -- and every other session would start
talking again, which is the surprise this exists to prevent. The terminal
outlives the conversation. It is the same handle dictation targets and the
same one thinking.py binds sessions to, so the three agree by construction.

"The terminal" is the controlling pty, written `pts:/dev/pts/3`. It is what a
session started by the wrapper is known by everywhere else, tmux or not: a
wrapper inside a pane is still a wrapper, and its pty is inside that pane. A
tmux pane id (`%12`) is still read and still honoured, for a focus written
before this and for a session the wrapper never started.

It lives next to the other durable state rather than in /tmp with the
per-session mute markers, for the same reason: a mute is meant to die with the
boot and a focus is meant to survive it. What it does not survive is the tmux
server that issued the pane id -- see _stale().
"""

import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import config as _config                              # noqa: E402

BASE = _config.BASE
FOCUS = BASE / "focus.json"


def read() -> dict:
    try:
        d = json.loads(FOCUS.read_text())
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _server(env: str) -> str:
    """The tmux server a `$TMUX` value names: socket and pid, without the
    session number, which differs between two clients of the same server."""
    return ",".join(str(env or "").split(",")[:2])


def _stale(d: dict) -> bool:
    """Was this focus set by a tmux server that is no longer running?

    Pane ids are unique per server, not for all time: a server that restarts
    starts issuing `%0` again, so a focus outliving its server can end up
    naming a completely different window -- silently, which is the worst way.
    A focus is dropped when the server it was written under is provably not the
    one asking. Both sides have to be known: something reading this from
    outside tmux cannot tell, and guessing there would drop a live focus.
    """
    if not str(d.get("pane") or "").startswith("%"):
        return False           # a pty key was never issued by a tmux server
    was, now = _server(d.get("tmux", "")), _server(os.environ.get("TMUX", ""))
    return bool(was and now and was != now)


def pane() -> str:
    """The focused pane (`%12`), "" when nothing is focused.

    A focus whose tmux server is gone reads as no focus at all, here rather
    than in each caller, so that every question about it -- may this speak,
    what does the HUD draw, what does doctor say -- gets the same answer.
    """
    d = read()
    return "" if _stale(d) else str(d.get("pane") or "")


def label() -> str:
    """How that pane read when it was focused: `dir · title`.

    Stored rather than looked up, because the reader of a focus is usually a
    hook with no reason to talk to tmux, and because a focused pane whose
    conversation has since been closed still has a name worth showing.
    """
    return str(read().get("label") or "")


def here() -> str:
    """The terminal THIS process is running in, "" when it cannot be known.

    Hooks are children of the claude process, so `$TMUX_PANE` names the pane
    the session lives in -- the exact join, needing no lookup at all.

    Outside tmux the answer is the session's controlling pty, and it has to be
    asked for indirectly: a hook has no controlling terminal of its own, its
    stdin is /dev/null and `/dev/tty` gives ENXIO, so ttyname() cannot be the
    route. `$CLAUDE_PID` is in the environment though, and that process does
    have one. Reading it back through /proc is the whole trick.
    """
    pid = os.environ.get("CLAUDE_PID", "")
    if pid:
        try:
            return "pts:" + os.readlink(f"/proc/{int(pid)}/fd/0")
        except (OSError, ValueError):
            pass
    try:
        return "pts:" + os.ttyname(0)      # run straight from the session
    except OSError:
        pass
    # Last, not first, and that ordering is load-bearing. A session inside
    # tmux has BOTH a pane id and a pty, and the pty is the one dictation
    # knows it by -- answering `%12` there would name a terminal no target
    # matches, and the focus would be refused in the one window it was
    # typed in. The pane id survives only for a session in a pane the
    # wrapper never started, which cannot be dictated into anyway.
    return os.environ.get("TMUX_PANE", "")


def set_pane(pane_id: str, name: str = "") -> bool:
    """Focus that pane. Written and renamed, like every other state file here:
    a hook reading it mid-write must never see half a focus and go silent."""
    if not pane_id:
        return False
    try:
        BASE.mkdir(parents=True, exist_ok=True)
        tmp = FOCUS.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"pane": pane_id, "label": name,
                                   "tmux": os.environ.get("TMUX", ""),
                                   "ts": time.time()}))
        os.replace(tmp, FOCUS)
        return True
    except Exception:
        return False


def clear() -> None:
    try:
        FOCUS.unlink(missing_ok=True)
    except Exception:
        pass


def _bound_session(pane_id: str) -> str:
    """The session thinking.py last saw in that pane, "" if unknown."""
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "thinking", HERE / "thinking.py")
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        return m.bound_session(pane_id)
    except Exception:
        return ""


def allows(session_id: str = "") -> bool:
    """May this session make a sound?

    Two ways to be the focused session, and either is enough. `$TMUX_PANE` is
    the direct one and costs nothing, which is why it is asked first: whatever
    runs inside the focused pane is the focused session by definition. The
    pane -> session binding is the fallback, for anything speaking on a
    session's behalf from outside its terminal.

    A focused pane with nothing bound and nobody running in it answers False
    everywhere, so a focus left on a closed window is silence rather than a
    free-for-all. That is the honest reading of "only that pane talks", and
    the HUD says so plainly rather than letting it look like a broken voice.
    """
    p = pane()
    if not p:
        return True
    if here() == p:
        return True
    return bool(session_id) and _bound_session(p) == session_id


def describe(session_id: str = "") -> str:
    """One line for `status`: where the focus is, seen from this session."""
    p = pane()
    if not p:
        return "none (every session speaks)"
    who = label() or p
    return f"this session ({who})" if allows(session_id) else f"another pane ({who})"
