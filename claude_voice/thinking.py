#!/usr/bin/env python3
"""A soft heartbeat while it works.

Started by the UserPromptSubmit hook, killed when the Stop hook plays the
answer. Without it there is a long silence between the acknowledgement and the
response, and you cannot tell whether it is still running or has hung.

  thinking.py            run the loop (internal, backgrounded)
  thinking.py --build    regenerate the sounds
  thinking.py --demo     audition the styles

Two different heartbeats
------------------------
The normal tick says "still thinking". When subagents are working, the tick
changes to a pair of descending notes: without looking at the screen you know
the wait is theirs, not mine.

How it knows agents are alive
-----------------------------
From the transcripts, not from hooks. The agent tool RETURNS in ~1.5 s while
the agent keeps working in the background, so a PreToolUse/PostToolUse pair
would measure a second and a half instead of the ten minutes that matter.

Each subagent writes its own .jsonl while it works. A file touched just now =
someone alive writing. Reading the filesystem also means this works in sessions
that were already running, with no restart and no dependency on hook config.

Scope
-----
Agents are counted PER SESSION, not globally. Otherwise a HUD pointed at a
quiet session would announce another window's agents, and one session's
heartbeat would chirp about someone else's work.

  thinking.py --agents [uuid]          which agents are running (there, or all)
  thinking.py --session <uuid>         run the loop bound to that session
  thinking.py --whose <dir> <title>    which session a tmux pane belongs to
"""

import json
import math
import os
import struct
import subprocess
import sys
import time
import wave
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import config as _config                              # noqa: E402

CFG = _config.load()
BASE = _config.BASE
TICK = BASE / "tick.wav"
TICK_AGENTS = BASE / "tick-agents.wav"

# Defaults come from config; tune them live without editing anything:
#   thinking.py --tune <delay> <interval>
DELAY = float(CFG.get("thinking.delay", 1.75))     # nothing sounds before this
INTERVAL = float(CFG.get("thinking.interval", 2.45))
# Hard cap. If the session dies (out of tokens, a hang, Ctrl-C) the Stop hook
# never fires and nobody kills the loop: this is the only thing that stops it.
MAX_RUN = float(CFG.get("thinking.max_run", 150))

# Past this the tick spaces out. A legitimately long turn stops nagging instead
# of hammering at the same rate until the cap.
DECAY_AFTER = 45
DECAY_FACTOR = 1.6
DECAY_MAX = 8.0

# While agents are out. Slower, and it does NOT decay: it is a sign of life
# during a long wait, not the rhythm of a normal turn.
AGENT_INTERVAL = float(CFG.get("thinking.agent_interval", 4.0))
AGENT_MAX_RUN = float(CFG.get("thinking.agent_max_run", 1800))

# Where subagents write their transcripts.
AGENT_ROOT = Path.home() / ".claude" / "projects"
AGENT_GLOB = "*/*/subagents/agent-*.jsonl"
AGENT_FRESH = 90          # wrote just now: alive, no further questions
AGENT_QUIET = 900         # quiet, but if it left a tool call open, still working

TUNE = BASE / "tick.json"


def timing() -> tuple:
    """Values from the file win, so tuning takes effect without a restart."""
    try:
        cfg = json.loads(TUNE.read_text())
        return float(cfg.get("delay", DELAY)), float(cfg.get("interval", INTERVAL))
    except Exception:
        return DELAY, INTERVAL


def _last_line(path: Path, nbytes: int = 16384) -> bytes:
    """The last line without reading the whole file: they weigh hundreds of KB."""
    with path.open("rb") as f:
        f.seek(0, 2)
        f.seek(max(0, f.tell() - nbytes))
        chunk = f.read()
    lines = [l for l in chunk.split(b"\n") if l.strip()]
    return lines[-1] if lines else b""


def _mid_tool(path: Path) -> bool:
    """Is it waiting on a tool? An agent running a long Bash or a web search
    writes nothing for minutes, and by mtime would look dead. If its last line
    is a call with no response, it is still working."""
    try:
        d = json.loads(_last_line(path))
    except Exception:
        return False
    content = (d.get("message") or {}).get("content")
    return isinstance(content, list) and any(
        isinstance(b, dict) and b.get("type") == "tool_use" for b in content)


def _slug(cwd: str) -> str:
    """The project directory is the path with the slashes swapped out."""
    return str(cwd).rstrip("/").replace("/", "-")


def _last_ai_title(path: Path) -> dict:
    """The last ai-title line of a transcript. Read whole, in binary: the title
    is rewritten as the conversation goes and the old one is useless. Measured,
    it is a couple of milliseconds even on multi-MB transcripts."""
    last = None
    try:
        with path.open("rb") as f:
            for line in f:
                if b'"type":"ai-title"' in line:
                    last = line
    except OSError:
        return {}
    try:
        return json.loads(last) if last else {}
    except Exception:
        return {}


def session_for(cwd: str, title: str) -> str:
    """Which session runs in that tmux pane. "" if it cannot be known.

    The title is the only thread joining a pane to its session: the claude
    process carries no uuid in its environment and holds no transcript open,
    and on resuming a conversation the process start no longer matches the
    file's. The title does: tmux shows the conversation's, and the transcript
    stores it in an ai-title line.
    """
    title = (title or "").lstrip("✳ ").strip()
    if not title:
        return ""
    d = AGENT_ROOT / _slug(cwd)
    if not d.is_dir():
        return ""
    try:
        files = sorted(d.glob("*.jsonl"),
                       key=lambda q: q.stat().st_mtime, reverse=True)[:10]
    except OSError:
        return ""
    for q in files:
        rec = _last_ai_title(q)
        if rec and (rec.get("aiTitle") or "").strip() == title:
            return rec.get("sessionId") or q.stem
    return ""


def sessions_in(cwd: str) -> list:
    """Every session id that has a transcript in that project, liveliest first.

    The narrowing for when `session_for` cannot name a pane: a window still
    carrying the default title has no ai-title to match, but it is certainly
    one of the sessions of the directory it runs in. Guessing inside the
    project is a guess about which conversation; guessing outside it is how a
    pane ends up showing another project's.
    """
    d = AGENT_ROOT / _slug(cwd)
    if not cwd or not d.is_dir():
        return []
    try:
        return [q.stem for q in sorted(d.glob("*.jsonl"),
                                       key=lambda q: q.stat().st_mtime,
                                       reverse=True)]
    except OSError:
        return []


def agents_live(session: str = "", cwd: str = "") -> list:
    """Subagents writing right now, with their description.

    There is no API or state file saying "N agents are running": this infers it
    from who is touching their transcript.

    With `session`, only that session's. With `cwd` and no session, that
    project's: worse, but better than mixing in other windows.
    """
    now = time.time()
    live = []
    if session:
        pattern = f"*/{session}/subagents/agent-*.jsonl"
    elif cwd:
        pattern = f"{_slug(cwd)}/*/subagents/agent-*.jsonl"
    else:
        pattern = AGENT_GLOB
    try:
        paths = sorted(AGENT_ROOT.glob(pattern))
    except Exception:
        return live
    for p in paths:
        try:
            age = now - p.stat().st_mtime
        except OSError:
            continue
        if age > AGENT_FRESH and not (age <= AGENT_QUIET and _mid_tool(p)):
            continue
        desc = ""
        try:
            meta = p.with_suffix("").with_suffix(".meta.json")
            desc = json.loads(meta.read_text()).get("description", "")
        except Exception:
            pass
        live.append(desc or p.stem[:14])
    return live


def agents_running(session: str = "", cwd: str = "") -> int:
    return len(agents_live(session, cwd))


RATE = 22050


def _write(path: Path, samples) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(b"".join(struct.pack("<h", int(max(-1, min(1, s)) * 32767))
                               for s in samples))


def make_tick(style: str = "soft", gain: float = 0.10):
    """A short blip with exponential decay. Deliberately soft: it is ambience."""
    tones = {
        "soft":   [(1180, 0.075)],                    # one discreet ting
        "double": [(1180, 0.055), (1480, 0.055)],     # two rising notes
        "low":    [(720, 0.090)],                     # deeper, less intrusive
        # Agents: two notes that FALL, with a gap between them. The normal tick
        # is flat; this one drops. That is what makes it need no thinking about.
        "agents": [(980, 0.060), (0, 0.055), (700, 0.080)],
    }[style]

    out = []
    for freq, dur in tones:
        n = int(RATE * dur)
        if not freq:                                   # silence between notes
            out.extend([0.0] * n)
            continue
        for i in range(n):
            t = i / RATE
            env = math.exp(-t * 42)                    # fast decay
            env *= min(1.0, i / (RATE * 0.004))        # soft attack, no click
            out.append(math.sin(2 * math.pi * freq * t) * env * gain)
    return out


def build(style: str = "") -> None:
    style = style or CFG.get("thinking.style", "soft")
    _write(TICK, make_tick(style))
    print(f"  {TICK}  ({style})")
    _write(TICK_AGENTS, make_tick("agents"))
    print(f"  {TICK_AGENTS}  (agents)")


def demo() -> None:
    for style in ("soft", "double", "low", "agents"):
        p = BASE / f"tick-{style}.wav"
        _write(p, make_tick(style))
        print(f"  {style} ...")
        for _ in range(3):
            subprocess.run(["aplay", "-q", str(p)], check=False)
            time.sleep(0.9)
        time.sleep(0.6)
    print("\n  pick one:  thinking.py --build <soft|double|low>")
    print("  ('agents' is not a choice: it plays by itself when agents run)")


def _queue_busy() -> bool:
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("audioq", HERE / "audioq.py")
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        return m.is_busy()
    except Exception:
        return False


def run(session: str = "") -> None:
    if not TICK.exists() or not TICK_AGENTS.exists():
        build()
    delay, interval = timing()
    time.sleep(delay)
    started = time.time()
    last_agent = None            # when an agent was last seen alive
    while True:
        elapsed = time.time() - started
        n = agents_running(session)
        if n:
            last_agent = time.time()
        # The 150 s cap counts from the start; if there were agents, it counts
        # from when the last one finished. That way a twenty-minute wait does
        # not go mute halfway, and a dead session still shuts itself up.
        budget = elapsed if last_agent is None else time.time() - last_agent
        if budget > MAX_RUN or elapsed > AGENT_MAX_RUN:
            break
        # Do not step on the voice: if the queue is playing, skip this tick.
        # The tick is ambience; it does not deserve to queue.
        if not _queue_busy():
            subprocess.run(["aplay", "-q", str(TICK_AGENTS if n else TICK)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           check=False)
        if n:
            gap = AGENT_INTERVAL          # no decay: the wait is long by nature
        else:
            gap = interval
            if elapsed > DECAY_AFTER:
                # grows gently: 2.45 -> 3.9 -> 6.3 -> cap
                steps = (elapsed - DECAY_AFTER) / 30.0
                gap = min(DECAY_MAX, interval * (DECAY_FACTOR ** steps))
        time.sleep(gap)


def tune(delay: float, interval: float) -> None:
    TUNE.parent.mkdir(parents=True, exist_ok=True)
    TUNE.write_text(json.dumps({"delay": delay, "interval": interval}))
    print(f"  first tick at {delay}s, then every {interval}s")


def main() -> int:
    arg = sys.argv[1] if sys.argv[1:] else ""
    if arg == "--build":
        build(sys.argv[2] if sys.argv[2:] else "")
    elif arg == "--demo":
        demo()
    elif arg == "--tune" and len(sys.argv) >= 4:
        tune(float(sys.argv[2]), float(sys.argv[3]))
    elif arg == "--agents":
        sid = sys.argv[2] if sys.argv[2:] else ""
        live = agents_live(sid)
        print(f"  agents running ({('session ' + sid[:8]) if sid else 'all'}): "
              f"{len(live)}")
        for d in live:
            print(f"    · {d}")
    elif arg == "--whose" and len(sys.argv) >= 4:
        print(f"  session: {session_for(sys.argv[2], sys.argv[3]) or '(unidentified)'}")
    elif arg == "--show":
        d, i = timing()
        print(f"  first tick: {d}s   interval: {i}s")
        print(f"  with agents: every {AGENT_INTERVAL}s, no decay")
        print(f"  agents now: {agents_running()} (all sessions)")
    elif arg.startswith("--") and arg != "--session":
        # Without this, a mistyped flag starts the tick loop silently and it
        # chirps away with nobody knowing why.
        print(f"  unknown flag {arg}; see the header of this file")
        return 2
    else:
        # --session <uuid>: bind the heartbeat to whoever launched it, so it
        # does not announce another window's agents.
        sid = sys.argv[2] if arg == "--session" and sys.argv[2:] else ""
        try:
            run(sid)
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
