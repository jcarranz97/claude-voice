#!/usr/bin/env python3
"""The microphone: who has it, for how long, and when that is worth saying.

Two audiences, one set of facts. The HUD asks these questions twenty times a
second and draws the answer; the watchdog asks once a minute from a systemd
timer and speaks up only when an answer has been true for too long. Both need
the same definitions, and those definitions are subtle enough that having two
copies of them would mean having two different opinions about whether your
microphone is on.

The distinction the whole file turns on: a capture stream that EXISTS is not a
microphone that is RECORDING. Streams linger. An app can open one at launch and
hold it, parked, until it quits -- nothing is captured through it, but the
object sits there, and your desktop's own indicator counts objects. That is why
the tray icon can stay lit for hours with nobody listening, and why "is the
light on" and "is anyone hearing me" are different questions.

The watchdog exists because the HUD can only answer while you are looking at
it. The failure that prompted it went unseen for two hours and eleven minutes:
a session died without unwinding, its capture stream outlived it, and the only
thing that knew was a tray icon with no explanation attached. Nothing was
running that could have said so. So this runs on a timer instead, outside the
HUD and outside any session, and it survives all of them.
"""

import json
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import config as _config                              # noqa: E402

CFG = _config.load()
BASE = _config.BASE

# What the watchdog remembers between runs: when each holder was first seen,
# and when we last said something about it. On disk because the timer starts a
# fresh process every minute -- an in-memory clock would reset each tick and
# nothing would ever reach five minutes.
WATCH_STATE = BASE / "mic-watch.json"

_open_cache = {"t": 0.0, "v": False}



def our_captures() -> list:
    """PIDs of pw-record processes that are ours, by command-line signature.

    Deliberately by signature and not by pidfile: the case worth catching is
    exactly the one where the pidfile is gone -- an orphan left by an unclean
    shutdown, with no parent and nobody able to name it.
    """
    node = CFG.get("stt.node", "") or ""
    pids = []
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        try:
            if (proc / "comm").read_text().strip() != "pw-record":
                continue
            cmd = (proc / "cmdline").read_bytes().decode("utf-8", "ignore")
        except Exception:
            continue
        if (node and node in cmd) or ("--raw" in cmd and "--latency" in cmd):
            pids.append(int(proc.name))
    return pids


def mic_open(fresh: bool = False) -> bool:
    """Is the microphone actually in use? The truth, not what we think.

    Two things count, and neither is "a capture stream exists":

    A stream that is RUNNING -- somebody is pulling audio right now, whoever
    they are. Existence is not enough: a capture node lingers as `idle` or
    `suspended` long after recording stops, and an app that holds its input
    open for its whole run (Claude Code's own dictation does) parks one there
    until it quits. The desktop's own indicator counts those, which is why the
    system tray can show a microphone while nothing is being recorded.

    Or a pw-record of OURS being alive, whatever state its stream is in. That
    is the orphan this warning exists for, and `x` can actually clear it. It
    also keeps the post-stop verification honest: a process on its way out
    stops pulling audio before it exits, and treating that instant as "closed"
    would skip the sweep in precisely the case that needs it.

    What we deliberately do NOT warn about is another app's parked stream. It
    has an owner, nobody is being recorded, and `x` cannot clear it -- so the
    warning would stand there permanently with no action behind it, and a
    privacy notice that is always on is one you stop reading.
    """
    if not fresh and time.time() - _open_cache["t"] < 1.0:
        return _open_cache["v"]
    val = False
    try:
        val = bool(our_captures())
    except Exception:
        val = False
    if not val:
        try:
            out = subprocess.run(["pw-dump"], capture_output=True, text=True, timeout=3).stdout
            objs = json.loads(out)
            val = any(
                o.get("type") == "PipeWire:Interface:Node"
                and o.get("info", {}).get("props", {}).get("media.class") == "Stream/Input/Audio"
                and o.get("info", {}).get("state") == "running"
                for o in objs)
        except Exception:
            # Without pw-dump, ALSA is the fallback. It reads RUNNING too, so
            # it agrees with the rule above rather than second-guessing it.
            try:
                val = any(f.read_text().startswith("state: RUNNING")
                          for f in Path("/proc/asound").glob("card*/pcm*c/sub*/status"))
            except Exception:
                val = False
    _open_cache.update(t=time.time(), v=val)
    return val


def mic_speaking() -> bool:
    """Are you talking RIGHT NOW? Only meaningful in conversation mode."""
    return (BASE / "mic-active").exists()


_held_cache = {"t": 0.0, "v": []}


def mic_held() -> list:
    """Who is holding the microphone open without recording through it.

    Not an alarm, and not ours. An app can open a capture stream and never
    close it -- some hold one for their whole run -- and the stream sits there
    parked: nothing is being captured, but the object exists. That is what
    lights the desktop's own microphone indicator, which counts streams rather
    than recordings, and it is why the tray icon can stay lit for hours with
    nobody listening.

    We cannot close someone else's stream, and `x` deliberately only sweeps our
    own captures. But naming who holds it turns an icon that cannot be
    explained into one sentence that can be acted on: quit that app.
    """
    if time.time() - _held_cache["t"] < 5.0:
        return _held_cache["v"]
    held = []
    try:
        out = subprocess.run(["pw-dump"], capture_output=True, text=True,
                             timeout=3).stdout
        objs = json.loads(out)
        byid = {o.get("id"): o for o in objs}
        for o in objs:
            info = o.get("info") or {}
            props = info.get("props") or {}
            if props.get("media.class") != "Stream/Input/Audio":
                continue
            if info.get("state") == "running":
                continue           # that is recording, and mic_open() has it
            client = (byid.get(props.get("client.id"), {}).get("info") or {})
            cprops = client.get("props") or {}
            pid = cprops.get("pipewire.sec.pid") or props.get("application.process.id")
            name = ""
            if pid:
                try:
                    name = (Path("/proc") / str(pid) / "comm").read_text().strip()
                except OSError:
                    continue       # the process is gone; the node is just residue
            name = name or props.get("application.name") or "?"
            held.append(f"{name} ({pid})" if pid else name)
    except Exception:
        held = []
    _held_cache.update(t=time.time(), v=held)
    return held


def listen_stranded() -> str:
    """Why the listening daemon is holding rather than working, or "".

    Written by listen.py when there is no Claude session to deliver to. The
    daemon stays up on purpose -- it resumes on its own when a session opens
    -- so without this the HUD would keep drawing a working conversation.
    """
    try:
        return (BASE / "listen-stranded").read_text().strip()
    except Exception:
        return ""


def daemon_alive() -> bool:
    try:
        import os as _os
        _os.kill(int((BASE / "listen.pid").read_text().strip()), 0)
        return True
    except Exception:
        return False


def sweep_orphans() -> int:
    """Kill captures of ours that were left without an owner."""
    import os as _os, signal
    killed = 0
    me = _os.getpid()
    # Ours by signature -- the configured node, or the exact raw capture flags
    # listen.py uses. Never a blanket pw-record kill: comm is matched against
    # the EXECUTABLE, so a shell that merely mentions pw-record is not a target.
    for pid in our_captures():
        if pid == me:
            continue
        try:
            _os.kill(pid, signal.SIGTERM)
            killed += 1
        except Exception:
            pass
    return killed


# --- who holds it, and since when ---------------------------------------
#
# Everything below is the watchdog. The functions above answer "right now";
# these answer "for how long", which is the only question that separates a
# microphone in use from a microphone left behind.


def _started(pid) -> str:
    """The process's start time, as the kernel counts it.

    Paired with the pid this makes an identity that survives across timer
    ticks and, more importantly, does not survive a pid being reused. Without
    it a recycled pid inherits the age of whatever held that number before,
    and the watchdog reports two hours against a process born a minute ago --
    a false alarm indistinguishable from the real thing it exists to catch.

    Field 22 of /proc/<pid>/stat, counted after the comm field, which is
    parenthesised and may itself contain spaces and parentheses. Hence the
    split on the LAST ')' rather than a naive split on whitespace.
    """
    try:
        raw = (Path("/proc") / str(pid) / "stat").read_text()
        return raw[raw.rindex(")") + 2:].split()[19]
    except Exception:
        return ""


def _comm(pid) -> str:
    try:
        return (Path("/proc") / str(pid) / "comm").read_text().strip()
    except OSError:
        return ""


# Which claim outranks which, when one process makes more than one.
RANK = {"parked": 0, "recording": 1, "ours": 2, "orphan": 3}


def holders() -> list:
    """Everyone with a claim on the microphone, classified by what it means.

    Four kinds, because four different things should happen:

      ours       a capture of ours with a live listen daemon behind it. This
                 is conversation mode working correctly. Never reported.
      orphan     a capture of ours with nothing owning it. The original bug:
                 recording, actively, on behalf of a session that is gone.
                 The only kind we can clear ourselves.
      recording  somebody else's stream, running. Not ours to close, but it
                 is audio leaving the room, so it is worth a word.
      parked     somebody else's stream, open but idle. Nothing is heard
                 through it. It is nonetheless what lights the tray icon, and
                 past a few minutes it is usually a process that forgot to die.

    A node whose owning process is already gone is skipped: that is PipeWire
    residue rather than a holder, and it clears itself.
    """
    found, seen, by_pid = [], set(), {}

    owned = daemon_alive()
    for pid in our_captures():
        seen.add(pid)
        found.append({"kind": "ours" if owned else "orphan", "pid": pid,
                      "name": _comm(pid) or "pw-record"})

    try:
        out = subprocess.run(["pw-dump"], capture_output=True, text=True,
                             timeout=5).stdout
        objs = json.loads(out)
        byid = {o.get("id"): o for o in objs}
        for o in objs:
            info = o.get("info") or {}
            props = info.get("props") or {}
            if props.get("media.class") != "Stream/Input/Audio":
                continue
            client = (byid.get(props.get("client.id"), {}).get("info") or {})
            cprops = client.get("props") or {}
            pid = cprops.get("pipewire.sec.pid") or props.get("application.process.id")
            pid = int(pid) if pid else None
            if pid and pid in seen:
                continue                # ours, and already classified above
            running = info.get("state") == "running"
            name = _comm(pid) if pid else ""
            if pid and not name:
                continue                # the process is gone; residue, not a holder
            if not running and not pid:
                continue                # parked and unattributable: nothing to say
            kind = "recording" if running else "parked"
            entry = {"kind": kind, "pid": pid,
                     "name": name or props.get("application.name") or "?"}
            # One process can own several streams -- a parked one it opened at
            # launch and a live one it is recording through. Reporting whichever
            # pw-dump happened to list first would let a recording hide behind a
            # parked stream, so the more serious claim wins.
            prior = by_pid.get(pid) if pid else None
            if prior is None:
                found.append(entry)
                if pid:
                    by_pid[pid] = entry
            elif RANK[kind] > RANK[prior["kind"]]:
                prior.update(entry)
    except Exception:
        pass

    for h in found:
        h["key"] = f"{h['pid']}:{_started(h['pid'])}" if h["pid"] else h["name"]
    return found


def _human(secs: float) -> str:
    secs = int(secs)
    if secs < 90:
        return f"{secs}s"
    if secs < 5400:
        return f"{secs // 60}m"
    return f"{secs // 3600}h{(secs % 3600) // 60:02d}m"


def _state() -> dict:
    try:
        return json.loads(WATCH_STATE.read_text())
    except Exception:
        return {}


def _save(state: dict) -> None:
    try:
        BASE.mkdir(parents=True, exist_ok=True)
        tmp = WATCH_STATE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state))
        tmp.replace(WATCH_STATE)
    except Exception:
        pass


URGENCY = {"orphan": "critical", "recording": "normal", "parked": "low"}


def _notify(holder: dict, age: float) -> None:
    """Say it on the desktop, where a lit tray icon is already asking.

    Replacing rather than stacking: a watchdog that leaves one notification
    per tick teaches you to sweep them away without reading, which defeats the
    point. The synchronous hint makes each new one take the place of the last.
    """
    kind, name, pid = holder["kind"], holder["name"], holder["pid"]
    who = f"{name} ({pid})" if pid else name
    if kind == "orphan":
        title = "Microphone left open"
        body = (f"{who} has been recording for {_human(age)} with no session "
                f"behind it.\nClear it with: claude-voice mic --sweep")
    elif kind == "recording":
        title = "Microphone in use"
        body = f"{who} has been recording for {_human(age)}."
    else:
        title = "Microphone held open"
        body = (f"{who} has held the microphone open for {_human(age)} "
                f"— not recording.\nQuitting it turns the indicator off.")
    try:
        subprocess.run(
            ["notify-send", "--app-name=claude-voice",
             f"--urgency={URGENCY.get(kind, 'normal')}",
             "--icon=audio-input-microphone",
             "-h", "string:x-canonical-private-synchronous:claude-voice-mic",
             title, body],
            check=False, timeout=5)
    except Exception:
        pass


def check(notify: bool = True) -> list:
    """One tick: age every holder, speak about the ones that are overdue.

    Returns the holders past the threshold whether or not anything was sent,
    so `--once` can print what a real tick would have announced.
    """
    now = time.time()
    after = float(CFG.get("mic.watch.after", 300))
    repeat = float(CFG.get("mic.watch.repeat", 1800))
    ignore = [str(p).lower() for p in (CFG.get("mic.watch.ignore", []) or [])]

    old, new, overdue = _state(), {}, []
    for h in holders():
        if h["kind"] == "ours":
            continue                    # conversation mode, working as intended
        was = old.get(h["key"]) or {}
        first = was.get("first", now)
        entry = {"first": first, "notified": was.get("notified", 0.0),
                 "name": h["name"], "pid": h["pid"], "kind": h["kind"]}
        age = now - first
        new[h["key"]] = entry
        if any(p in h["name"].lower() for p in ignore):
            continue                    # aged, deliberately never announced
        if age < after:
            continue
        overdue.append(dict(h, age=age))
        if now - entry["notified"] >= repeat:
            if notify:
                _notify(h, age)
            entry["notified"] = now
    # Holders absent from this tick are simply dropped: the stream closed, and
    # a released microphone is not news.
    _save(new)
    return overdue


def report() -> int:
    """`claude-voice mic` -- the same facts, for a person at a terminal."""
    now, state = time.time(), _state()
    hs = holders()
    if not hs:
        print("  microphone : free — no capture stream open")
        return 0
    label = {"ours": "conversation mode (ours)",
             "orphan": "ORPHAN — no session behind it",
             "recording": "recording",
             "parked": "held open, not recording"}
    for h in hs:
        first = (state.get(h["key"]) or {}).get("first")
        age = f"  for {_human(now - first)}" if first else ""
        who = f"{h['name']} ({h['pid']})" if h["pid"] else h["name"]
        print(f"  {who:<26} {label[h['kind']]}{age}")
    return 0


UNIT = "claude-voice-mic"


def _units() -> tuple:
    """The service and timer, generated rather than shipped as files.

    Generated because the two values that matter -- which interpreter, and
    where this file lives -- are properties of the machine it is installed on,
    not of the repository. A checked-in unit file would need the same values
    substituted at install time anyway, and would go stale the moment the
    checkout moved.

    A timer firing a oneshot, rather than one long-lived process: the state
    that has to survive is already on disk, so a tick that dies takes nothing
    with it and the next one recovers on its own. A daemon would have to be
    watched by something, and this file exists precisely because the thing
    that was supposed to be watching had died.
    """
    every = int(float(CFG.get("mic.watch.interval", 60)))
    service = f"""[Unit]
Description=claude-voice: notice a microphone left open
Documentation=https://github.com/jcarranz97/claude-voice

[Service]
Type=oneshot
# Spelled out rather than inherited. The user manager usually has this
# imported by the graphical session, but not always and not from the first
# tick after a boot -- and a watchdog whose notifications go nowhere is worse
# than no watchdog, because it looks like good news.
Environment=DBUS_SESSION_BUS_ADDRESS=unix:path=%t/bus
ExecStart={sys.executable} {Path(__file__).resolve()} --once
"""
    timer = f"""[Unit]
Description=claude-voice: check every {every}s for a microphone left open

[Timer]
OnStartupSec=30
OnUnitActiveSec={every}
AccuracySec=10
Unit={UNIT}.service

[Install]
WantedBy=timers.target
"""
    return service, timer


def install() -> int:
    d = Path.home() / ".config" / "systemd" / "user"
    d.mkdir(parents=True, exist_ok=True)
    service, timer = _units()
    (d / f"{UNIT}.service").write_text(service)
    (d / f"{UNIT}.timer").write_text(timer)
    for args in (["daemon-reload"], ["enable", "--now", f"{UNIT}.timer"]):
        r = subprocess.run(["systemctl", "--user"] + args,
                           capture_output=True, text=True)
        if r.returncode:
            print(f"systemctl {' '.join(args)}: {r.stderr.strip()}",
                  file=sys.stderr)
            return 1
    print(f"  installed  : {d / (UNIT + '.timer')}")
    print(f"  checks     : every {int(float(CFG.get('mic.watch.interval', 60)))}s")
    print(f"  warns after: {int(float(CFG.get('mic.watch.after', 300)))}s held")
    print(f"  remove with: claude-voice mic --uninstall")
    return 0


def uninstall() -> int:
    subprocess.run(["systemctl", "--user", "disable", "--now", f"{UNIT}.timer"],
                   capture_output=True, text=True)
    d = Path.home() / ".config" / "systemd" / "user"
    for name in (f"{UNIT}.timer", f"{UNIT}.service"):
        try:
            (d / name).unlink()
        except FileNotFoundError:
            pass
    subprocess.run(["systemctl", "--user", "daemon-reload"],
                   capture_output=True, text=True)
    print("  the microphone watchdog is removed")
    return 0


def main(argv: list) -> int:
    if "--install" in argv:
        return install()
    if "--uninstall" in argv:
        return uninstall()
    if "--sweep" in argv:
        n = sweep_orphans()
        print(f"closed {n} capture{'' if n == 1 else 's'} of ours")
        return 0
    if "--once" in argv or "--watch" in argv:
        if not CFG.get("mic.watch.enabled", True):
            return 0
    if "--once" in argv:
        for h in check():
            print(f"{h['kind']}: {h['name']} ({h['pid']}) for {_human(h['age'])}")
        return 0
    if "--watch" in argv:
        every = float(CFG.get("mic.watch.interval", 60))
        while True:
            try:
                check()
            except Exception as e:
                print(f"claude-voice mic: {e}", file=sys.stderr)
            time.sleep(every)
    return report()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
