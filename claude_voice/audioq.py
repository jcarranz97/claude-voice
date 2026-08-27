#!/usr/bin/env python3
"""Audio queue: one sound at a time, in order.

Before this, every producer (acknowledgement, narration, final answer, tick)
spawned its own `aplay` and trusted a timer not to overlap. That fails three
ways: the Stop hook has no idea what is currently playing, two notices firing
at once read the same state before either writes it (a race), and the tick does
whatever it likes.

Here producers only ENQUEUE and return immediately -- which matters, because a
slow hook stalls the session. A single player process, guarded by a lock, pulls
from the queue and plays one item at a time.

  audioq.py --play        player loop (internal)
  audioq.py --status      what is queued
  audioq.py --drain       empty the queue and cut whatever is playing
"""

import fcntl
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config as _config                              # noqa: E402

BASE = _config.BASE
QUEUE = BASE / "queue"
LOCK = BASE / "player.lock"
SEQ = BASE / "seq"
NOWFILE = BASE / "playing.pid"


def _next_seq() -> int:
    """Monotonic sequence, so arrival order survives."""
    QUEUE.mkdir(parents=True, exist_ok=True)
    try:
        with open(SEQ, "r+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            n = int(f.read().strip() or 0) + 1
            f.seek(0); f.truncate(); f.write(str(n))
            return n
    except FileNotFoundError:
        with open(SEQ, "w") as f:
            f.write("1")
        return 1


def enqueue(wav: Path, text: str = "", flush_pending: bool = False,
            session: str = "") -> None:
    """Append to the queue. flush_pending drops whatever has not played yet.

    The final answer uses flush_pending: hearing "let me check the config file"
    after the work is already done helps nobody. Whatever is playing right now
    is allowed to finish -- cutting mid-word sounds worse than waiting a second.

    It drops only THIS session's pending notices. The queue is shared -- one
    pair of speakers -- but "the work is done" is only true for the session
    that finished, and dropping everyone's notices meant another window's
    narration vanished without ever being heard.

    `session` is carried in the meta so the HUD can tell whose voice is coming
    out of the speaker, and stay quiet about a session it is not watching.
    """
    QUEUE.mkdir(parents=True, exist_ok=True)
    if flush_pending:
        for old in QUEUE.glob("*.json"):
            try:
                meta = json.loads(old.read_text())
                if session and meta.get("session", "") not in (session, ""):
                    continue
                Path(meta["wav"]).unlink(missing_ok=True)
                old.unlink(missing_ok=True)
            except Exception:
                pass

    # The one place every spoken thing passes through, so the one place worth
    # writing the spoken log from: order here is playback order. The session
    # is carried through: history is one conversation's record, not the box's.
    _record(text, session)

    n = _next_seq()
    dest = QUEUE / f"{n:08d}.wav"
    try:
        os.replace(wav, dest)             # atomic: the player never sees half a file
    except OSError:
        import shutil
        shutil.copy(wav, dest)
    (QUEUE / f"{n:08d}.json").write_text(json.dumps(
        {"wav": str(dest), "text": text, "session": session or ""}))
    ensure_player()


def _record(text: str, session: str = "") -> None:
    """Log the line for the HUD's history pane. Never let it cost a hook."""
    if not text:
        return
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "spokenlog", Path(__file__).resolve().parent / "spokenlog.py")
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        m.record("out", text, session=session)
    except Exception:
        pass


def is_busy() -> bool:
    """True if a player is alive (holding the lock)."""
    try:
        with open(LOCK, "a+") as f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(f, fcntl.LOCK_UN)
                return False
            except BlockingIOError:
                return True
    except Exception:
        return False


def ensure_player() -> None:
    if is_busy():
        return
    try:
        subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--play"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    except Exception:
        pass


def _set_state(state: str, text: str = "", secs: float = 0.0,
               session: str = "", env=None, t0: float = 0.0) -> None:
    """The state of the SPEAKER, which is global on purpose: there is one pair
    of them. `session` says whose line it is, so the HUD can decide whether the
    window it is watching is the one talking. What each session is DOING lives
    in turn.py, one file each.

    `env` and `t0` are the shape of the line and the moment it started: the
    HUDs read them off the clock, so the reactor moves with the syllables
    instead of on a timer of its own. They are written once per utterance --
    a level streamed twenty-five times a second through a state file would be
    twenty-five chances to catch a half-written one.
    """
    try:
        BASE.mkdir(parents=True, exist_ok=True)
        (BASE / "state.json").write_text(json.dumps({
            "state": state, "text": text,
            "until": time.time() + secs if secs else 0, "ts": time.time(),
            "session": session or "",
            "env": env or [], "t0": t0, "step": _level().STEP}))
    except Exception:
        pass


def _level():
    """level.py, loaded the way every module here loads its neighbours."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "level", Path(__file__).resolve().parent / "level.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def play_loop() -> int:
    BASE.mkdir(parents=True, exist_ok=True)
    with open(LOCK, "a+") as lockf:
        try:
            fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0                      # a player already runs: this one is spare

        idle_rounds = 0
        while True:
            metas = sorted(QUEUE.glob("*.json"))
            if not metas:
                # Short grace window in case something enqueues as it empties.
                idle_rounds += 1
                if idle_rounds > 3:
                    break
                time.sleep(0.25)
                continue
            idle_rounds = 0

            meta_path = metas[0]
            try:
                meta = json.loads(meta_path.read_text())
                wav = Path(meta["wav"])
                text = meta.get("text", "")
                owner = meta.get("session", "")
            except Exception:
                meta_path.unlink(missing_ok=True)
                continue

            secs, env = 0.0, []
            try:
                # The duration and the shape come off the same read. It costs
                # about half a millisecond per second of audio, which is paid
                # once, here, rather than by every window on every frame.
                secs, env = _level().envelope(wav)
            except Exception:
                pass

            try:
                proc = subprocess.Popen(["aplay", "-q", str(wav)],
                                        stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL)
                # After the spawn, not before it: t0 is meant to be the moment
                # sound starts, and forking is the slowest part of getting there.
                _set_state("speaking", text, secs, owner, env, time.time())
                NOWFILE.write_text(str(proc.pid))
                proc.wait()
            except Exception:
                pass
            finally:
                NOWFILE.unlink(missing_ok=True)
                wav.unlink(missing_ok=True)
                meta_path.unlink(missing_ok=True)

        _set_state("ready")
    return 0


def drain() -> int:
    """Empty the queue and cut what is playing. For the panic button."""
    n = 0
    for f in list(QUEUE.glob("*")) if QUEUE.exists() else []:
        try:
            f.unlink(missing_ok=True)
            n += 1
        except Exception:
            pass
    try:
        if NOWFILE.exists():
            os.kill(int(NOWFILE.read_text().strip()), 15)
            NOWFILE.unlink(missing_ok=True)
            n += 1
    except (ProcessLookupError, ValueError, PermissionError, OSError):
        NOWFILE.unlink(missing_ok=True)
    return n


if __name__ == "__main__":
    arg = sys.argv[1] if sys.argv[1:] else "--status"
    if arg == "--play":
        sys.exit(play_loop())
    if arg == "--drain":
        print(f"  queue drained ({drain()} items)")
    else:
        pend = sorted(QUEUE.glob("*.json")) if QUEUE.exists() else []
        print(f"  playing: {'yes' if is_busy() else 'no'}   ·   queued: {len(pend)}")
        for p in pend:
            try:
                print(f"    {p.stem}  {json.loads(p.read_text()).get('text','')[:60]}")
            except Exception:
                pass
