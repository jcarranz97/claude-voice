#!/usr/bin/env python3
"""The switch.

  voice.py on        turn the voice on (every session)
  voice.py off       turn it off
  voice.py           show status
  voice.py mute      mute just THIS session (uses $CLAUDE_SESSION_ID)
  voice.py focus     only THIS session speaks; the other windows go quiet
  voice.py focus --clear   give every session its voice back
  voice.py silence   cut all sound right now -- the panic button

  voice.py --hook-context   internal: UserPromptSubmit
  voice.py --build-acks [preset]   re-synthesize the cached acknowledgements

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
import focus as _focus                                # noqa: E402
import presence as _presence                          # noqa: E402

CFG = _config.load()
BASE = _config.BASE
STATE = BASE / "enabled"
ACKS = BASE / "acks"
LAST_ACK = BASE / "last-ack"


def ack_dir(preset: str = "") -> Path:
    """Where the cached acknowledgements for a preset live.

    Per preset, and not one directory rebuilt on every switch, for two
    reasons. Rebuilding is seconds of synthesis, which is not what a keystroke
    should cost; and the cache is indexed by position -- ack03.wav is the
    fourth phrase in ack.phrases -- so a directory holding the other
    language's wavs speaks one phrase while the spoken log records another.
    Keeping them apart means switching back is instant and always honest.
    """
    _migrate_flat_acks()
    return ACKS / (preset or CFG.preset)


def _migrate_flat_acks() -> None:
    """Older installs cached acks/*.wav with no preset under them. Those were
    built for whatever the config named, so that is where they belong."""
    try:
        loose = list(ACKS.glob("*.wav"))
        if not loose:
            return
        home = ACKS / _config.configured_preset()
        home.mkdir(parents=True, exist_ok=True)
        for wav in loose:
            wav.rename(home / wav.name)
    except OSError:
        pass


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
    files = sorted(ack_dir().glob("*.wav"))
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


def build_acks(preset: str = "") -> None:
    """Synthesize the acknowledgement cache. Run once, or when changing voice.

    A preset name builds that language's cache instead of the active one,
    which is how the switch warms the other side without speaking it.
    """
    speak = _mod("speak")
    cfg = _config.resolve(preset) if preset else CFG
    phrases = cfg.get("ack.phrases", []) or []
    if not phrases:
        print("  no ack.phrases configured -- nothing to build")
        return

    out_dir = ack_dir(preset or cfg.preset)
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("*.wav"):
        old.unlink()
    for i, phrase in enumerate(phrases):
        out = out_dir / f"ack{i:02d}.wav"
        # synthesize() reads the module-level config, so building another
        # preset's cache means lending speak.py that preset for the duration.
        if speak.synthesize(phrase, out, cfg=cfg):
            print(f"  {out.name}  {phrase}")
    print(f"\n  {len(list(out_dir.glob('*.wav')))} acknowledgements cached "
          f"for {out_dir.name}")


def session_mute(session_id: str) -> Path:
    import tempfile
    return Path(tempfile.gettempdir()) / f"cv-mute-{session_id or 'default'}"


def enabled(session_id: str = "") -> bool:
    """The same four questions speak.py asks, in the same order: an open HUD,
    the switch, this session's mute, and the focus -- which silences every
    session except the one pane it names.

    The first one is what keeps a closed HUD from costing anything: this is
    consulted before the acknowledgement is spawned, before the heartbeat
    starts, and before the instruction is injected into the prompt.
    """
    if not _presence.open_now():
        return False
    if not STATE.exists():
        return False
    if session_mute(session_id).exists():
        return False
    return _focus.allows(session_id)


def main() -> int:
    arg = sys.argv[1] if sys.argv[1:] else ""

    if arg == "--hook-context":
        # UserPromptSubmit: inject the instruction only while the voice is on.
        try:
            data = json.load(sys.stdin)
        except Exception:
            return 0
        # Before the switch is consulted, because this is not about sound: the
        # binding is how a pane names its conversation, and dictation files its
        # spoken log by it whether the voice is on or off. SessionStart already
        # wrote one; this covers a session that was running before the hook was
        # installed, and refreshes the file so a live pane is never swept.
        try:
            _mod("thinking").bind(data.get("session_id", ""),
                                  cwd=data.get("cwd", ""))
        except Exception:
            pass
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
        build_acks(sys.argv[2] if sys.argv[2:] else "")
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
        if not _presence.open_now():
            # Turning it on and hearing nothing is the one confusion this
            # gate can cause, so it is answered at the moment it is caused.
            print("  (no HUD open, so nothing runs yet: claude-voice hud)")
    elif arg == "off":
        STATE.unlink(missing_ok=True)
        n = silence_all()          # off means shut up NOW, same as in the HUD
        print(f"  voice off{f' ({n} cut)' if n else ''}")
    elif arg in ("mute", "solo"):
        # `solo` is what this was called first, and it meant the opposite of
        # what the word means everywhere else. Kept working, not advertised.
        session_mute(sid).touch()
        print(f"  muted in this session only ({sid[:8] or 'default'})")
    elif arg == "focus":
        if any(a in ("--clear", "--off", "off") for a in sys.argv[2:]):
            _focus.clear()
            print("  focus cleared — every session speaks again")
            return 0
        pane = _focus.here()
        if not pane:
            # Nothing durable to hang it on: the session uuid changes when the
            # window is restarted, which is the one thing focus must survive.
            print("  no tmux pane here — focus needs tmux, like dictation does")
            return 1
        name = ""
        try:
            name = _mod("dictate").aim_at_pane_id(pane)
        except Exception:
            pass
        if not name:
            # Refused rather than granted, because a focus on a pane with no
            # conversation in it is silence EVERYWHERE, and the pane it was
            # typed in is the last place anyone would look for the cause. Same
            # rule dictation already follows about panes it does not recognise.
            print(f"  no claude running in this pane ({pane})")
            print("  run it inside the session you want to hear, or press f in the HUD")
            return 1
        _focus.set_pane(pane, name)
        # The other windows may be mid-sentence right now, and "only this one
        # speaks" is not a thing you want to wait a turn for.
        n = silence_all()
        print(f"  focus: this session only ({name}){f' — {n} cut' if n else ''}")
    else:
        on = STATE.exists()
        muted = session_mute(sid).exists()
        speaks = on and not muted and _focus.allows(sid) and _presence.open_now()
        n = len(_presence.windows())
        print(f"  window  : {f'open ({n})' if n else 'closed — nothing runs'}"
              + ("" if _presence.required() else "   (not required)"))
        print(f"  global  : {'ON' if on else 'off'}")
        print(f"  session : {'muted' if muted else 'normal'}")
        print(f"  focus   : {_focus.describe(sid)}")
        print(f"  effect  : {'SPEAKS' if speaks else 'silent'}")
        print("\n  voice on | voice off | voice mute | voice focus | voice silence")
    return 0


if __name__ == "__main__":
    sys.exit(main())
