#!/usr/bin/env python3
"""Push-to-talk dictation: record, transcribe, hand it to the Claude session.

  dictate.py --pane ID        set the target session (once)
  dictate.py --panes          list the sessions text can be delivered to
  dictate.py --toggle         start recording / stop and send
  dictate.py --status         current state
  dictate.py --target         the target as JSON (used by the HUD)
  dictate.py --target-session the target's session uuid, if it resolves
  dictate.py --can-send       exit 0 if a Claude session can receive text

The way in
----------
There is no supported way to push text into an already-running interactive
Claude Code session: stdin belongs to the terminal emulator, which holds the
pty master, and `/dev/pts/N` is the slave -- writing there paints the screen
instead of feeding the program. The usual Wayland alternative (ydotool) needs
uinput permissions and types into whatever has focus, which is a window and
not a session.

So the text arrives from something that was present at launch. `claude-voice
run claude` holds the pty master itself, and run.py writes into it. That works
in any terminal, tmux included -- a wrapper inside a pane is still a wrapper,
and delivery does not go through tmux at all.

Safety
------
This TYPES AND SENDS into a terminal, so a bad transcription in a shell would
run as a command. The guarantee is identity: a socket reaches only the process
its own wrapper started. There is nothing to guess about and nothing to check.
"""

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import config as _config                              # noqa: E402


def _mod(name: str):
    """Load a sibling module by path: these files are scripts, not a package."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, HERE / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


CFG = _config.load()
BASE = _config.BASE
PANE_CFG = BASE / "pane.json"
RECPID = BASE / "dictate.pid"
RECWAV = Path(tempfile.gettempdir()) / "cv-dictation.wav"

# Set stt.device by NAME, not index: ALSA card numbers reorder on reconnect or
# reboot. A setup pinned to plughw:4,0 silently started recording from a webcam
# mic -- digital silence -- the day a card moved.
DEVICE = CFG.get("stt.device", "default")
MODEL = CFG.get("stt.model", "small")        # base mishears technical vocabulary
LANGUAGE = CFG.get("stt.language", "en")
MAX_SECS = int(CFG.get("stt.max_secs", 120))  # a forgotten recording can't grow forever
GLOSSARY = CFG.get("stt.glossary", "") or ""

LOG = BASE / "dictate.log"


def log(msg: str) -> None:
    """The HUD launches this with output to /dev/null: without a log, a failure
    is invisible and looks like "it records and goes nowhere"."""
    print(f"  {msg}")
    try:
        BASE.mkdir(parents=True, exist_ok=True)
        with LOG.open("a") as f:
            f.write(f"{time.strftime('%F %T')} {msg}\n")
    except Exception:
        pass


def cfg() -> dict:
    try:
        return json.loads(PANE_CFG.read_text())
    except Exception:
        return {}


def claude_panes() -> list:
    """Every session text can be delivered to: the ones started by the wrapper.

    Named for what it used to enumerate -- panes -- because the HUD and the
    history reader ask this question by that name, and the answer is the same
    kind of thing. What changed is where sessions come from: they are started
    on purpose now, not found by looking at every pane on the machine and
    guessing which ones were meant.
    """
    try:
        return _mod("run").sessions()
    except Exception:
        return []


def find(target: str) -> dict:
    """The session with that id, {} when it is not there any more."""
    return next((p for p in claude_panes() if p["id"] == target), {})


def pane_is_claude(target: str) -> bool:
    return bool(find(target))


def describe(target: str) -> str:
    p = find(target)
    if p:
        return f'{p["dir"]} · {p["title"]}'
    return target or "(not set)"


# Why the wording matters: "no session" and "the session you picked is gone"
# look identical from the microphone's side -- nothing gets delivered -- but
# they need different fixes, so they are never collapsed into one message.
NO_SESSION = "no Claude Code session"
STALE_TARGET = "target session is gone"
NO_TARGET = "several sessions, none picked"


def current() -> str:
    """The session dictation is aimed at, choosing one when it need not ask.

    A stored target wins while it is alive. Otherwise, if there is exactly one
    session on the machine, that is the answer and there was never a question
    -- which is the ordinary case now that a session is started on purpose
    rather than found. Making somebody pick from a list of one was a step that
    existed only because the old lookup showed every pane on the machine,
    most of which nobody meant to dictate into.

    Not written back to disk: an implicit target is a fact about right now,
    and remembering it would turn "the only session" into a stale pointer the
    moment a second one opened.
    """
    live = claude_panes()
    stored = cfg().get("pane")
    if stored and any(p["id"] == stored for p in live):
        return stored
    if len(live) == 1:
        return live[0]["id"]
    return ""


def target_status() -> tuple:
    """(deliverable, why-not). The single answer to "is anyone listening?".

    Everything that opens the microphone asks this FIRST. Recording into a
    void and discovering it afterwards costs a transcription and, worse, makes
    a dead setup look exactly like one that simply did not hear you.
    """
    live = claude_panes()
    if not live:
        return False, NO_SESSION
    if not current():
        # Sessions exist and none of them is the answer. Which of the two
        # ways that happened decides what to do about it, so they stay apart:
        # a target that died wants re-picking, several fresh ones want a
        # choice, and saying "no session" to either was the lie that sent
        # people looking for a session that was right there.
        return False, STALE_TARGET if cfg().get("pane") else NO_TARGET
    return True, ""


def cycle() -> str:
    """Move to the next session. Returns the new target's description."""
    live = claude_panes()
    if not live:
        return "(no Claude sessions)"
    cur = current()
    ids = [p["id"] for p in live]
    nxt = ids[(ids.index(cur) + 1) % len(ids)] if cur in ids else ids[0]
    BASE.mkdir(parents=True, exist_ok=True)
    PANE_CFG.write_text(json.dumps({"pane": nxt}))
    return describe(nxt)


def aim_at_pane_id(pane_id: str) -> str:
    """Point dictation at the session on that `pts:` terminal, or "".

    The two ids are not interchangeable and both are needed. `pane.json` holds
    the delivery handle, `wrap:<pid>`, which is what the socket is found by.
    The other is what a hook INSIDE the session knows itself by -- the pty,
    which it can read off `$CLAUDE_PID` without being told. This is the
    crossing between them, so that focusing a session and dictating into it
    stay one act rather than two settings that happen to agree.
    """
    p = next((q for q in claude_panes() if q.get("pane_id") == pane_id), None)
    if not p:
        return ""
    BASE.mkdir(parents=True, exist_ok=True)
    PANE_CFG.write_text(json.dumps({"pane": p["id"]}))
    return f'{p["dir"]} · {p["title"]}'.strip(" ·")


def target_session() -> str:
    """The session uuid behind the target, "" if it cannot be resolved.

    The wrapper already knows: run.py joins Claude Code's own registry on the
    pty it holds, so the uuid is there from the first moment -- including for
    the dictated line that OPENS a conversation, which is the one a title
    lookup could never match, because the title does not exist yet.
    """
    return find(current()).get("session", "")


def deliver(text: str) -> bool:
    """Type the text into the target session and send it.

    Nothing is checked here beyond "is there a target", because there is
    nothing left to check: the socket reaches the process its own wrapper
    started, and no other. Identity is the guarantee, not inspection.
    """
    ok, why = target_status()
    if not ok:
        log(f"refusing to send: {why}")
        return False
    target = current()
    sent = False
    try:
        sent = _mod("run").deliver(find(target), text)
    except Exception as e:
        log(f"delivery failed: {e}")
        return False
    if not sent:
        log(f"delivery failed: {target} did not take it")
        return False
    log(f"delivered to {target}: {text[:60]}")
    # This is the only place that knows a sentence was SPOKEN and not typed --
    # conversation mode lands here too -- so it is where your side of the
    # spoken log gets written, under the session it went to.
    _mod("spokenlog").record("in", text, session=target_session())
    return True


def recording() -> bool:
    if not RECPID.exists():
        return False
    try:
        os.kill(int(RECPID.read_text().strip()), 0)
        return True
    except (ProcessLookupError, ValueError, PermissionError):
        RECPID.unlink(missing_ok=True)
        return False


def start() -> bool:
    """Open the microphone -- unless there is nothing to deliver to.

    Refusing here rather than at delivery time is the whole point: a
    transcription that cannot go anywhere is a minute of the user's breath and
    a second of Whisper spent to produce silence.
    """
    ok, why = target_status()
    if not ok:
        log(f"not recording: {why}")
        return False
    BASE.mkdir(parents=True, exist_ok=True)
    RECWAV.unlink(missing_ok=True)
    proc = subprocess.Popen(
        ["arecord", "-D", DEVICE, "-f", "S16_LE", "-r", "16000", "-c", "1",
         "-d", str(MAX_SECS), str(RECWAV)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True)
    RECPID.write_text(str(proc.pid))
    log("recording")
    return True


def stop_and_send() -> None:
    try:
        os.kill(int(RECPID.read_text().strip()), 15)
    except (ProcessLookupError, ValueError, PermissionError, OSError):
        pass
    RECPID.unlink(missing_ok=True)
    time.sleep(0.35)                       # let arecord close the WAV header

    if not RECWAV.exists() or RECWAV.stat().st_size < 4000:
        log("nothing captured")
        return

    from faster_whisper import WhisperModel
    t0 = time.time()
    model = WhisperModel(MODEL, device="cpu", compute_type="int8",
                         cpu_threads=os.cpu_count() or 4)
    segments, _ = model.transcribe(
        str(RECWAV), language=LANGUAGE, beam_size=5,
        initial_prompt=GLOSSARY or None,
        vad_filter=True,                   # without this the glossary hallucinates
        no_speech_threshold=0.6)
    text = " ".join(s.text.strip() for s in segments).strip()
    log(f"transcribed in {time.time()-t0:.1f}s: {text or '(silence)'}")

    if text:
        deliver(text)


def main() -> int:
    arg = sys.argv[1] if sys.argv[1:] else "--status"

    if arg == "--panes":
        panes = claude_panes()
        cur = current()
        print("  Claude sessions:")
        for p in panes:
            mark = "  <- target" if p["id"] == cur else ""
            print(f'    {p["id"]:16} {p["dir"]:14} {p["title"][:40]}{mark}')
        if not panes:
            print("    none — start one with: claude-voice")
    elif arg == "--target":
        # For the HUD: path and title are what identify the session, and
        # ok/why are what it shows when there is no session to name.
        ok, why = target_status()
        info = find(current())
        print(json.dumps({**info, "ok": ok, "why": why}))
    elif arg == "--target-session":
        # For anything that needs the session rather than the pane: the CLI
        # history reader, which shows the same conversation the HUD does.
        print(target_session())
    elif arg == "--can-send":
        # For anything that wants the answer without parsing JSON: exit 0 when
        # a session is there, 1 when not, with the reason on stdout.
        ok, why = target_status()
        print(why or describe(current()))
        return 0 if ok else 1
    elif arg == "--next":
        print(f"  target: {cycle()}")
    elif arg == "--pane" and sys.argv[2:]:
        BASE.mkdir(parents=True, exist_ok=True)
        PANE_CFG.write_text(json.dumps({"pane": sys.argv[2]}))
        ok = pane_is_claude(sys.argv[2])
        print(f"  target: {sys.argv[2]}" + ("" if ok else "   (careful: no claude there)"))
    elif arg == "--toggle":
        try:
            if recording():
                stop_and_send()
            elif not start():
                # Non-zero so a key binding or script can react instead of
                # assuming the microphone opened.
                print(f"  {target_status()[1]}: dictation disabled")
                return 1
        except Exception as e:
            log(f"ERROR: {type(e).__name__}: {e}")
            return 1
    else:
        ok, why = target_status()
        print(f"  recording : {'yes' if recording() else 'no'}")
        print(f"  target    : {describe(current())}"
              + ("" if ok else f"   ({why}: dictation disabled)"))
        print(f"  device    : {DEVICE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
