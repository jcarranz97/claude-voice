#!/usr/bin/env python3
"""Is a window open? Nothing of ours runs while the answer is no.

The voice lives in hooks, so for a long time closing the HUD closed nothing:
the window went away, everything looked shut, and a session opened an hour
later spoke out of a terminal nobody expected to hear from. Worse, the daemon
that holds the microphone outlived the window that started it -- a capture
running with nothing on screen to say so.

So the HUD is the application, and this module is the answer to "is it open".
Two rules follow from it, and both are needed:

  nothing STARTS while it is closed -- the hooks consult this before they
  spawn anything at all, which is also why a machine with no window open
  spends no tokens on spoken lines nobody would hear; and

  nothing KEEPS RUNNING once it closes -- the microphone daemon and the
  heartbeat loop ask on the timer they already have, and stop themselves.

Liveness, never a marker. A HUD that is killed, whose terminal is closed, or
whose machine loses power gets no chance to clean up after itself, and those
are exactly the cases where a microphone left open matters most. So a window
records itself under its own pid and is only believed while that pid is alive.
One file per window, because two HUDs in two terminals are both open, and the
first one quitting must not take the second one's voice with it.
"""

import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import config as _config  # noqa: E402

BASE = _config.BASE
PREFIX = "hud-"
SUFFIX = ".pid"


def _files() -> list:
    try:
        return sorted(BASE.glob(f"{PREFIX}*{SUFFIX}"))
    except Exception:
        return []


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, ValueError):
        return False
    except PermissionError:
        return True  # somebody else's process, but a process


def windows() -> list:
    """The pids of the HUDs that are actually running, sweeping the rest.

    The sweep is here rather than in a cleanup path on purpose: the file left
    behind by a HUD that died badly is removed by whoever next asks the
    question, which is the only moment it is known to be stale.
    """
    live = []
    for f in _files():
        try:
            pid = int(f.name[len(PREFIX) : -len(SUFFIX)])
        except ValueError:
            continue
        if _alive(pid):
            live.append(pid)
        else:
            try:
                f.unlink(missing_ok=True)
            except Exception:
                pass
    return live


def required() -> bool:
    """Is the voice gated on a window at all? A setting, because running
    headless is a real use and was the documented behaviour first."""
    try:
        return bool(_config.load().get("hud.required", True))
    except Exception:
        return True


def open_now() -> bool:
    """May anything of ours run right now?"""
    return bool(windows()) if required() else True


def enter() -> Path:
    """Record this process as an open window. Called by the HUD at startup."""
    f = BASE / f"{PREFIX}{os.getpid()}{SUFFIX}"
    try:
        BASE.mkdir(parents=True, exist_ok=True)
        f.write_text(str(os.getpid()))
    except Exception:
        pass
    return f


def leave() -> None:
    """This window is gone. The pid is dead either way, so this is tidiness
    rather than correctness -- windows() would have swept it."""
    try:
        (BASE / f"{PREFIX}{os.getpid()}{SUFFIX}").unlink(missing_ok=True)
    except Exception:
        pass


def last_one_out() -> bool:
    """True when this process is the only window left.

    What it is for: a HUD closing while another is still up must not shut the
    voice down for it. Asked after this one's own file is gone.
    """
    return not windows()
