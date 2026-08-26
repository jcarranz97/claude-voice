#!/usr/bin/env python3
"""The switch.

  voice.py on        turn the voice on (every session)
  voice.py off       turn it off
  voice.py           show status
  voice.py solo      mute just THIS session (uses $CLAUDE_SESSION_ID)
  voice.py silence   cut all sound right now -- the panic button

  voice.py --hook-context   internal: UserPromptSubmit
  voice.py --build-acks     re-synthesize the cached acknowledgements

The voice is OFF by default, on purpose.

The switch controls both ends, and that is what makes it cheap: while it is
off, the UserPromptSubmit hook injects nothing, so the model never writes the
<!-- TTS: --> marker and you spend no tokens on spoken summaries nobody will
hear. Turning it on is what makes the instruction appear.
"""

import json
import os
import random
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import config as _config                              # noqa: E402

CFG = _config.load()
BASE = _config.BASE
STATE = BASE / "enabled"
ACK_DIR = BASE / "acks"
LAST_ACK = BASE / "last-ack"


def _mod(name: str):
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, HERE / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def ack_phrase(name: str) -> str:
    """The phrase a cached acknowledgement says, from its filename.

    build_acks() names them by their index in ack.phrases, so the index is the
    only link back to the words -- and without it the spoken log would record
    an empty line every time the cached fallback plays.
    """
    phrases = CFG.get("ack.phrases", []) or []
    try:
        i = int(Path(name).stem.removeprefix("ack"))
        return phrases[i]
    except (ValueError, IndexError):
        return ""


def play_ack(session: str = "") -> None:
    """Play a random cached acknowledgement, never the same one twice running."""
    files = sorted(ACK_DIR.glob("*.wav"))
    if not files:
        return
    prev = LAST_ACK.read_text().strip() if LAST_ACK.exists() else ""
    pool = [f for f in files if f.name != prev] or files
    pick = random.choice(pool)
    try:
        LAST_ACK.write_text(pick.name)
    except Exception:
        pass
    import shutil, tempfile
    audioq = _mod("audioq")
    # Copy: the cache original must survive, the queue consumes the file.
    tmp = Path(tempfile.gettempdir()) / f"cv-ack-{os.getpid()}.wav"
    shutil.copy(pick, tmp)
    audioq.enqueue(tmp, ack_phrase(pick.name), session=session)


def start_thinking(session: str = "") -> None:
    """Background heartbeat while it works. speak.py kills it on answering.

    The session is passed down twice over. Once to thinking.py, so the agent
    tick only fires for THIS window's agents; and once to the pidfile name, so
    that starting a turn here does not kill the heartbeat of a window that is
    still working. With one shared pidfile, whoever moved last owned it."""
    pidfile = _mod("turn").pidfile("thinking", session)
    # If one survived from an earlier turn, kill it before opening another.
    stop_thinking(session)
    try:
        cmd = [sys.executable, str(HERE / "thinking.py")]
        if session:
            cmd += ["--session", session]
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True)
        pidfile.parent.mkdir(parents=True, exist_ok=True)
        pidfile.write_text(str(proc.pid))
    except Exception:
        pass


def stop_thinking(session: str = "") -> int:
    """Returns how many processes it killed, so the counter does not lie."""
    pidfile = _mod("turn").pidfile("thinking", session)
    try:
        if pidfile.exists():
            # The loop spawns aplay as a child, so the whole group has to go or
            # the last tick keeps playing.
            os.killpg(int(pidfile.read_text().strip()), 15)
            pidfile.unlink(missing_ok=True)
            return 1
    except (ProcessLookupError, ValueError, PermissionError, OSError):
        try:
            pidfile.unlink(missing_ok=True)
        except Exception:
            pass
    return 0


def silence_all() -> int:
    """Cut ALL sound now. The panic button.

    It exists because if the session dies mid-answer (out of tokens, a hang,
    Ctrl-C) the Stop hook never fires and the tick is left alone in the room.
    thinking.py's own time cap bounds it, but this kills it instantly.
    """
    audioq = _mod("audioq")
    turn = _mod("turn")
    killed = audioq.drain()

    # Every session's, not just the caller's: this is the button you press when
    # something is making noise and you do not care which window started it.
    for f in turn.pidfiles("thinking") + turn.pidfiles("ack"):
        group = f.name.startswith("thinking")
        try:
            if f.exists():
                pid = int(f.read_text().strip())
                os.killpg(pid, 15) if group else os.kill(pid, 15)
                killed += 1
        except (ProcessLookupError, ValueError, PermissionError, OSError):
            pass
        finally:
            f.unlink(missing_ok=True)

    # Sweep by process, not by pidfile. A loop whose pidfile was lost (dead
    # session, overwritten pidfile) is an orphan nobody else can kill -- which
    # is exactly the case that leaves the tick going. We walk /proc instead of
    # pkill: `pkill -f` matches itself, and we must not touch other apps' audio.
    me = os.getpid()
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit() or int(proc.name) == me:
            continue
        try:
            cmd = (proc / "cmdline").read_bytes().decode("utf-8", "ignore")
        except Exception:
            continue
        mine = ("thinking.py" in cmd or
                ("aplay" in cmd and ("/cv-" in cmd or str(BASE) in cmd)))
        if not mine:
            continue
        try:
            os.kill(int(proc.name), 15)
            killed += 1
        except (ProcessLookupError, PermissionError, OSError):
            pass

    try:
        import time
        BASE.mkdir(parents=True, exist_ok=True)
        (BASE / "state.json").write_text(json.dumps(
            {"state": "idle", "text": "", "until": 0, "ts": time.time(),
             "session": ""}))
    except Exception:
        pass
    # Silence means silence everywhere, so no window is left claiming to think.
    for sid in turn.sessions():
        turn.write(sid, "idle")
    return killed


def build_acks() -> None:
    """Synthesize the acknowledgement cache. Run once, or when changing voice."""
    speak = _mod("speak")
    phrases = CFG.get("ack.phrases", []) or []
    if not phrases:
        print("  no ack.phrases configured -- nothing to build")
        return

    ACK_DIR.mkdir(parents=True, exist_ok=True)
    for old in ACK_DIR.glob("*.wav"):
        old.unlink()
    for i, phrase in enumerate(phrases):
        out = ACK_DIR / f"ack{i:02d}.wav"
        if speak.synthesize(phrase, out):
            print(f"  {out.name}  {phrase}")
    print(f"\n  {len(list(ACK_DIR.glob('*.wav')))} acknowledgements cached")


def session_mute(session_id: str) -> Path:
    import tempfile
    return Path(tempfile.gettempdir()) / f"cv-mute-{session_id or 'default'}"


def enabled(session_id: str = "") -> bool:
    if not STATE.exists():
        return False
    return not session_mute(session_id).exists()


def main() -> int:
    arg = sys.argv[1] if sys.argv[1:] else ""

    if arg == "--hook-context":
        # UserPromptSubmit: inject the instruction only while the voice is on.
        try:
            data = json.load(sys.stdin)
        except Exception:
            return 0
        if not enabled(data.get("session_id", "")):
            return 0
        # Sound immediately: without this there is a long silence between
        # hitting enter and the answer landing, and it feels ignored.
        # Detached, because the model call takes ~0.7 s and a hook must return.
        sid = data.get("session_id", "")
        if CFG.get("ack.enabled", True):
            try:
                subprocess.Popen(
                    [sys.executable, str(HERE / "ack.py"),
                     "--session", sid, data.get("prompt", "")],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    start_new_session=True)
            except Exception:
                play_ack(sid)          # if even that won't start, the cached one
        if CFG.get("thinking.enabled", True):
            start_thinking(sid)
        # This session is thinking. Only this one -- the others are whatever
        # they already were.
        _mod("turn").write(sid, "thinking")
        if CFG.get("instruction.enabled", True) and CFG.instruction:
            print(json.dumps({"hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": CFG.instruction,
            }}))
        return 0

    if arg == "--build-acks":
        build_acks()
        return 0

    if arg in ("silence", "--silence", "shut"):
        n = silence_all()
        print(f"  silence ({n} process{'es' if n != 1 else ''} cut)")
        return 0

    sid = os.environ.get("CLAUDE_SESSION_ID", "")

    if arg == "on":
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.touch()
        session_mute(sid).unlink(missing_ok=True)
        print("  voice ON — it will speak at the end of every response")
    elif arg == "off":
        STATE.unlink(missing_ok=True)
        n = silence_all()          # off means shut up NOW, same as in the HUD
        print(f"  voice off{f' ({n} cut)' if n else ''}")
    elif arg == "solo":
        session_mute(sid).touch()
        print(f"  muted in this session only ({sid[:8] or 'default'})")
    else:
        on = STATE.exists()
        muted = session_mute(sid).exists()
        print(f"  global  : {'ON' if on else 'off'}")
        print(f"  session : {'muted' if muted else 'normal'}")
        print(f"  effect  : {'SPEAKS' if on and not muted else 'silent'}")
        print("\n  voice on | voice off | voice solo | voice silence")
    return 0


if __name__ == "__main__":
    sys.exit(main())
