#!/usr/bin/env python3
"""What is using the microphone and the speakers, right now — anyone's.

  monitor.py              print it once and exit
  monitor.py --watch [s]  repeat in place until you quit

This exists because "nothing of ours runs while no window is open" is a claim,
and a claim about a microphone is worth being able to check rather than
believe. So it answers the question from the machine's side: not "what does
claude-voice think it is doing", but "what has a claim on the capture device
and the speakers at this instant", ours or anybody's.

Whose it is matters less than that it is open. A browser tab left in a call
holds the microphone exactly as much as we would, and the reason the light on
the laptop is on is a question about the machine, not about this program. Ours
are named as ours; everything else is named as itself.

Nothing here kills anything. It is a window, not a broom -- `claude-voice mic
--sweep` is the one that clears a capture of ours left behind, and other
people's processes are theirs to close.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import config as _config  # noqa: E402
import mic as _mic  # noqa: E402
import presence as _presence  # noqa: E402

BASE = _config.BASE

# What each of our processes is FOR, in the words the README uses. A pid and a
# filename tell you a thing is running; this tells you what it is doing there.
OURS = {
    "hud.py": "the window",
    "listen.py": "conversation mode · microphone open",
    "dictate.py": "dictation — recording or transcribing",
    "thinking.py": "the heartbeat",
    "ack.py": "the acknowledgement",
    "speak.py": "synthesizing a line",
    "narrate.py": "narrating mid-turn",
    "audioq.py": "playing the queue",
    "aplay": "playing a line",
}


def _age(pid) -> float:
    """Seconds since that process started, 0 when it cannot be read.

    From the kernel's own start time rather than anything we recorded, for the
    same reason mic.py pairs it with the pid: a recycled pid must not inherit
    the age of whoever held the number before it.
    """
    try:
        ticks = float(_mic._started(pid))
        up = float(Path("/proc/uptime").read_text().split()[0])
        return max(0.0, up - ticks / os.sysconf("SC_CLK_TCK"))
    except Exception:
        return 0.0


def _pw_streams(media_class: str) -> list:
    """Every PipeWire stream of one class, with the process behind it.

    Same shape of read as mic.py's holders(), pointed at a different class:
    input streams are the microphone, output streams are the speakers. A
    stream whose process is already gone is skipped -- that is residue, and it
    clears itself.
    """
    out = []
    try:
        objs = json.loads(
            subprocess.run(["pw-dump"], capture_output=True, text=True, timeout=5).stdout
        )
    except Exception:
        return out
    byid = {o.get("id"): o for o in objs}
    for o in objs:
        info = o.get("info") or {}
        props = info.get("props") or {}
        if props.get("media.class") != media_class:
            continue
        client = byid.get(props.get("client.id"), {}).get("info") or {}
        cprops = client.get("props") or {}
        pid = cprops.get("pipewire.sec.pid") or props.get("application.process.id")
        pid = int(pid) if pid else None
        name = _mic._comm(pid) if pid else ""
        if pid and not name:
            continue
        out.append(
            {
                "pid": pid,
                "running": info.get("state") == "running",
                "name": name or props.get("application.name") or "?",
                # What a person would recognise it by: the tab title, the file
                # being played, the app's own name for itself.
                "detail": (props.get("media.name") or props.get("application.name") or ""),
            }
        )
    return out


def _is_ours(pid) -> bool:
    """Is that process one of ours, asked of /proc at this instant?

    Not "was it in the list we built a moment ago": the players are born and
    die inside a single line of speech, so a stream sampled after the list was
    taken would be attributed to nobody and read as another app on the
    speakers -- the one mistake this view must not make.
    """
    try:
        cmd = (Path("/proc") / str(pid) / "cmdline").read_bytes().decode("utf-8", "ignore")
    except Exception:
        return False
    return bool(cmd) and (str(HERE) in cmd or str(BASE) in cmd)


def our_pids() -> dict:
    """Our own processes: pid -> (what it is, its command name).

    By command line rather than by pidfile, deliberately. A pidfile says what
    was supposed to be running; /proc says what is. The whole point of this
    view is the gap between those two.
    """
    me, found = os.getpid(), {}
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit() or int(proc.name) == me:
            continue
        try:
            cmd = (proc / "cmdline").read_bytes().decode("utf-8", "ignore")
        except Exception:
            continue
        if not cmd:
            continue
        # The package directory catches every module of ours; the config
        # directory catches the players, which are ordinary aplay processes
        # pointed at a file in our queue.
        if str(HERE) not in cmd and str(BASE) not in cmd:
            continue
        # The script, not the command name: every module of ours runs under
        # the same interpreter, so /proc/comm says "python" for all of them
        # and names nothing at all.
        comm = _mic._comm(proc.name) or "?"
        script = next((s for s in OURS if s in cmd), comm)
        found[int(proc.name)] = (OURS.get(script, script), script)
    return found


def _line(mark: str, name: str, detail: str, age: float) -> str:
    age_s = _mic._human(age) if age else ""
    return f"  {mark} {name:<16} {detail:<38} {age_s:>7}".rstrip()


def report() -> None:
    ours = our_pids()
    windows = len(_presence.windows())

    print("microphone")
    holders = _mic.holders()
    if not holders:
        print("  ◦ free — nothing has the microphone open")
    for h in holders:
        pid = h.get("pid")
        if h["kind"] == "orphan":
            mark, who = "⚠", "claude-voice · ORPHAN, nothing owns it"
        elif h["kind"] == "ours":
            mark, who = "●", "claude-voice · conversation mode"
        else:
            mark = "●" if h["kind"] == "recording" else "◦"
            who = "recording" if h["kind"] == "recording" else "open, not recording"
        print(_line(mark, h.get("name") or "?", who, _age(pid) if pid else 0))

    print("\nspeakers")
    streams = _pw_streams("Stream/Output/Audio")
    live = [s for s in streams if s["running"]]
    if not live:
        print("  ◦ idle — nothing is playing")
    for s in live:
        who = (
            "claude-voice · speaking"
            if s["pid"] and _is_ours(s["pid"])
            else (s["detail"] or "playing")
        )
        print(_line("●", s["name"], who, _age(s["pid"]) if s["pid"] else 0))

    print("\nclaude-voice")
    if not ours and not _mic.our_captures():
        print(
            "  ◦ nothing running"
            + ("" if _presence.required() else "   (a window is not required)")
        )
    for pid, (what, comm) in sorted(ours.items(), key=lambda kv: -_age(kv[0])):
        if what == "the window":
            what = f"the window ({windows} open)"
        print(_line("●", comm, what, _age(pid)))
    if windows == 0 and _presence.required() and ours:
        # The one combination worth calling out: things of ours are running
        # with no window behind them, which is what this whole gate is for.
        print("\n  ⚠ no window is open — these should be stopping on their own")


def main() -> int:
    argv = sys.argv[1:]
    if argv[:1] == ["--watch"]:
        try:
            every = float(argv[1]) if argv[1:] else 2.0
        except ValueError:
            every = 2.0
        try:
            while True:
                # Home and clear, rather than curses: this has to stay
                # readable when it is piped, and there is nothing to interact
                # with. Ctrl-C is the only key it needs.
                print("\033[H\033[2J", end="")
                print(f"claude-voice monitor — every {every:g}s, Ctrl-C to quit\n")
                report()
                sys.stdout.flush()
                time.sleep(every)
        except KeyboardInterrupt:
            print()
        return 0
    report()
    return 0


if __name__ == "__main__":
    sys.exit(main())
