#!/usr/bin/env python3
"""Push-to-talk dictation: record, transcribe, hand it to the Claude session.

  dictate.py --pane 0:0.0     set the target tmux pane (once)
  dictate.py --panes          list panes running claude
  dictate.py --toggle         start recording / stop and send
  dictate.py --status         current state
  dictate.py --target         the target pane as JSON (used by the HUD)

Why tmux
--------
There is no supported way to push text into an already-running interactive
Claude Code session: stdin is consumed at startup and there is no IPC. The
usual Wayland alternative (ydotool) needs uinput permissions and window focus.
If claude runs inside tmux, `send-keys` delivers the exact text to the pane
with no special permissions and without stealing focus.

Safety
------
This TYPES AND SENDS into a terminal. If the target pane were a shell, any
transcription error would run as a command. So sending is refused unless the
pane is running `claude`, where the text lands in the prompt box, not a shell.
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
    """Panes running claude, each with what identifies it.

    tmux exposes the pane title, and Claude Code puts the conversation title
    there -- which together with the directory is the only thing telling two
    sessions apart at a glance. Without it, picking a target is guesswork.
    """
    try:
        out = subprocess.run(
            ["tmux", "list-panes", "-a", "-F",
             "#{session_name}:#{window_index}.#{pane_index}\t"
             "#{pane_current_command}\t#{pane_current_path}\t#{pane_title}"],
            capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return []
    panes = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 4 or parts[1].strip() != "claude":
            continue
        panes.append({
            "id": parts[0],
            "dir": Path(parts[2]).name or parts[2],
            "path": parts[2],
            "title": parts[3].lstrip("✳ ").strip() or "(untitled)",
        })
    return panes


def pane_is_claude(target: str) -> bool:
    return any(p["id"] == target for p in claude_panes())


def describe(target: str) -> str:
    for p in claude_panes():
        if p["id"] == target:
            return f'{p["dir"]} · {p["title"]}'
    return target or "(not set)"


def cycle() -> str:
    """Move to the next Claude pane. Returns the new target's description."""
    panes = claude_panes()
    if not panes:
        return "(no Claude sessions in tmux)"
    cur = cfg().get("pane")
    ids = [p["id"] for p in panes]
    nxt = ids[(ids.index(cur) + 1) % len(ids)] if cur in ids else ids[0]
    BASE.mkdir(parents=True, exist_ok=True)
    PANE_CFG.write_text(json.dumps({"pane": nxt}))
    return describe(nxt)


def deliver(text: str) -> bool:
    """Type the text into the pane and send it. Refused if it is not claude."""
    target = cfg().get("pane")
    if not target:
        log("no target pane: set one with --pane")
        return False
    if not pane_is_claude(target):
        # This check IS the guardrail: in a shell, a bad transcription would
        # execute as a command.
        log(f"pane {target} is not running claude: refusing to send")
        return False
    try:
        # -l = literal: do not interpret the text as key names.
        subprocess.run(["tmux", "send-keys", "-t", target, "-l", text],
                       check=True, timeout=5)
        time.sleep(0.15)          # let the TUI process the paste
        subprocess.run(["tmux", "send-keys", "-t", target, "Enter"],
                       check=True, timeout=5)
        log(f"delivered to {target}: {text[:60]}")
        # This is the only place that knows a sentence was SPOKEN and not
        # typed -- conversation mode lands here too -- so it is where your
        # side of the spoken log gets written.
        _mod("spokenlog").record("in", text)
        return True
    except Exception as e:
        log(f"delivery failed: {e}")
        return False


def recording() -> bool:
    if not RECPID.exists():
        return False
    try:
        os.kill(int(RECPID.read_text().strip()), 0)
        return True
    except (ProcessLookupError, ValueError, PermissionError):
        RECPID.unlink(missing_ok=True)
        return False


def start() -> None:
    BASE.mkdir(parents=True, exist_ok=True)
    RECWAV.unlink(missing_ok=True)
    proc = subprocess.Popen(
        ["arecord", "-D", DEVICE, "-f", "S16_LE", "-r", "16000", "-c", "1",
         "-d", str(MAX_SECS), str(RECWAV)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True)
    RECPID.write_text(str(proc.pid))
    log("recording")


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
        cur = cfg().get("pane")
        print("  Claude sessions:")
        for p in panes:
            mark = "  <- target" if p["id"] == cur else ""
            print(f'    {p["id"]}  {p["dir"]:14} {p["title"][:44]}{mark}')
        if not panes:
            print("    none (is claude running outside tmux?)")
    elif arg == "--target":
        # For the HUD: path and title are what identify the session.
        cur = cfg().get("pane")
        print(json.dumps(next((p for p in claude_panes() if p["id"] == cur), {})))
    elif arg == "--next":
        print(f"  target: {cycle()}")
    elif arg == "--pane" and sys.argv[2:]:
        BASE.mkdir(parents=True, exist_ok=True)
        PANE_CFG.write_text(json.dumps({"pane": sys.argv[2]}))
        ok = pane_is_claude(sys.argv[2])
        print(f"  target: {sys.argv[2]}" + ("" if ok else "   (careful: no claude there)"))
    elif arg == "--toggle":
        try:
            stop_and_send() if recording() else start()
        except Exception as e:
            log(f"ERROR: {type(e).__name__}: {e}")
            return 1
    else:
        print(f"  recording : {'yes' if recording() else 'no'}")
        print(f"  target    : {describe(cfg().get('pane'))}")
        print(f"  device    : {DEVICE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
