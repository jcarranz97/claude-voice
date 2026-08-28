#!/usr/bin/env python3
"""Everything the HUD knows, with nothing about how it is drawn.

There are two windows now -- the curses one (hud.py) and the web one
(hudweb.py) -- and they have to agree. Not roughly: exactly. A HUD that says
the microphone is closed while the other says it is open is worse than either
of them alone, because now neither can be trusted.

So the questions live here, once, and the drawing lives in the two front ends:

  what is this session doing        read_state()
  which session am I watching       target_session(), dictate_target()
  is anything refusing to work      dictate_blocked(), focus_state()
  who has the microphone            re-exported from mic.py
  what was said out loud            history_entries()
  what is the machine doing         system_stats()

and the four things a key press can do -- the voice, the focus, dictation,
conversation mode -- are act() and its neighbours, so that pressing `m` in a
terminal and clicking MUTE in a browser run the same code rather than two
copies of it that drift.

Every tmux and disk question in here is cached on a clock, because the callers
redraw far faster than any of these answers change.
"""

import json
import os
import shutil
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import config as _config  # noqa: E402
import focus as _focus  # noqa: E402
import lang as _lang  # noqa: E402

# How loud the voice is at this instant, in either direction. Both windows
# ask the same way, for the same reason every other question here is asked
# once: a reactor that pulses to a different number than the meter under it
# is two instruments disagreeing about one room.
import level as _level  # noqa: E402

# The branch the watched session is on, and what GitHub thinks of it. Asked
# here rather than in either window for the usual reason, and because the
# answer costs a network call: two windows must not mean two of them.
import repo as _repo  # noqa: E402

# Every microphone question -- who has it, whether anyone is actually
# being recorded, and how to close a capture of ours that was left
# behind -- is answered in one place, because the watchdog on the
# systemd timer has to give the same answers this window does.
from mic import (  # noqa: E402,F401
    daemon_alive,
    listen_stranded,
    mic_held,
    mic_open,
    mic_speaking,
    our_captures,
    sweep_orphans,
)

try:
    import spokenlog as _spokenlog  # noqa: E402
except Exception:  # an empty pane, not a crash
    _spokenlog = None

CFG = _config.load()
BASE = _config.BASE
# The SPEAKER's state: global, because there is one pair of speakers. What
# each session is doing lives in turn.py, one file per session.
STATE = BASE / "state.json"
ENABLED = BASE / "enabled"
# Whether the history panel was open when you last had a HUD up. A marker
# file, the same way the voice switch is one: the HUD keeps no other state.
PANEL_OPEN = BASE / "hud-history"
LISTEN_PID = BASE / "listen.pid"

IDLE_AFTER = 900  # after 15 min of nothing, treat it as asleep

TITLE = (CFG.get("hud.title", "") or CFG.name).strip()
# Letterspaced, the way the status labels are.
TITLE = " ".join(TITLE.upper())


def L(key: str, fallback: str) -> str:
    return CFG.get(f"hud.{key}", fallback) or fallback


def reload_cfg() -> None:
    """Pick up a language switch without being reopened.

    CFG and TITLE are resolved once at import, which is right for every other
    module here -- they are short-lived processes -- and wrong for the one
    window that stays open across a switch. The modules imported above keep
    their own copies, but nothing they answer (which microphone, where the
    panel sits) is a language question.
    """
    global CFG, TITLE
    CFG = _config.load(reload=True)
    TITLE = " ".join(((CFG.get("hud.title", "") or CFG.name).strip()).upper())


_lang_cache = {"preset": None, "name": "", "label": ""}


def next_language() -> tuple:
    """(preset, what that language calls itself) for the one `l` switches to.

    Cached against the active preset: the legend is redrawn twenty times a
    second and the answer is two TOML files off disk.
    """
    if _lang_cache["preset"] != CFG.preset:
        nxt = _lang.following(CFG.preset)
        _lang_cache.update(preset=CFG.preset, name=nxt, label=_lang.label(nxt) if nxt else "")
    return _lang_cache["name"], _lang_cache["label"]


_target_cache = {"t": 0.0, "pane": {}}


def dictate_target_info() -> dict:
    """The target pane as-is: id, path and title. Re-read every 2 s because it
    queries tmux, and the HUD redraws far more often than that."""
    if time.time() - _target_cache["t"] < 2.0:
        return _target_cache["pane"]
    try:
        out = subprocess.run(
            [sys.executable, str(HERE / "dictate.py"), "--target"],
            capture_output=True,
            text=True,
            timeout=3,
        ).stdout
        _target_cache["pane"] = json.loads(out.strip() or "{}")
    except Exception:
        _target_cache["pane"] = {}
    _target_cache["t"] = time.time()
    return _target_cache["pane"]


def dictate_target() -> str:
    """How that session is shown on screen."""
    p = dictate_target_info()
    if not p.get("ok"):
        return ""
    return f"{p.get('dir', '')} · {p.get('title', '')}".strip(" ·")


_focus_cache = {"t": 0.0, "val": ("", ""), "pane": ""}


def focus_state(fresh: bool = False) -> tuple:
    """(state, label) for the focused pane, cached like every tmux question.

    state is "" when nothing is focused, "live" when the focused pane still has
    a claude in it, and "gone" when it does not. The last one is why this asks
    tmux at all rather than reading the focus file: a focus pointing at a
    window that has been closed means the voice is silent EVERYWHERE, and a HUD
    that shows that as ordinary quiet is a HUD that lies.
    """
    if fresh:
        _focus_cache["t"] = 0.0
    if time.time() - _focus_cache["t"] < 2.0:
        return _focus_cache["val"]
    pane = _focus.pane()
    _focus_cache["pane"] = pane
    if not pane:
        _focus_cache.update(t=time.time(), val=("", ""))
        return _focus_cache["val"]
    try:
        panes = _mod("dictate").claude_panes()
    except Exception:
        panes = []
    p = next((q for q in panes if q.get("pane_id") == pane), None)
    live = f"{p['dir']} · {p['title']}".strip(" ·") if p else ""
    _focus_cache.update(
        t=time.time(), val=("live", live) if p else ("gone", _focus.label() or pane)
    )
    return _focus_cache["val"]


def focus_here() -> bool:
    """Is the focused pane the one dictation is aimed at? They are moved
    together, so a no means somebody pointed one of them somewhere else.

    Through the cache, not the file: this is asked on every frame, and the HUD
    draws twenty of those a second.
    """
    focus_state()
    pane = _focus_cache["pane"]
    return bool(pane) and dictate_target_info().get("pane_id") == pane


def dictate_blocked(fresh: bool = False) -> str:
    """Why nothing can be dictated, or "" when something can.

    Drawn as a WARNING rather than as a missing line: an absent footer reads
    as ordinary chrome, and "nobody is listening" is the one thing the HUD
    exists to make obvious before you start talking.
    """
    if fresh:
        # A key press is worth one tmux query: refusing because of a
        # two-second-old cache, right after the session was opened, would be
        # the same lie in the other direction.
        _target_cache["t"] = 0.0
    p = dictate_target_info()
    if p.get("ok"):
        return ""
    return p.get("why") or "no Claude Code session"


_mods = {}


def _mod(name: str):
    """Load a sibling module once and keep it. read_state() runs on every
    frame, and re-executing a module twenty times a second is pure waste."""
    if name not in _mods:
        import importlib.util

        spec = importlib.util.spec_from_file_location(name, HERE / f"{name}.py")
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        _mods[name] = m
    return _mods[name]


def _thinking():
    """The heartbeat module, which is where agent detection lives."""
    return _mod("thinking")


_session_cache = {"t": 0.0, "key": None, "sid": "", "cwd": ""}


def target_session() -> tuple:
    """(uuid, directory) of the session the HUD is watching -- the same one
    dictation goes to, the one `t` switches.

    Without this the HUD announced ANY window's agents, so switching to a quiet
    session still claimed there were agents running.
    """
    tgt = dictate_target_info()
    key = (tgt.get("path"), tgt.get("title"), tgt.get("pane_id"))
    if key == _session_cache["key"] and time.time() - _session_cache["t"] < 20:
        return _session_cache["sid"], _session_cache["cwd"]
    sid = ""
    if key[0]:
        try:
            sid = _thinking().session_for(key[0], key[1] or "", key[2] or "")
        except Exception:
            sid = ""
    _session_cache.update({"t": time.time(), "key": key, "sid": sid, "cwd": key[0] or ""})
    return sid, key[0] or ""


_agents_cache = {"t": 0.0, "list": []}


def agents_live() -> list:
    """Subagents working right now IN THE VISIBLE SESSION, with descriptions.

    Cached: the HUD redraws 20 times a second and sweeping the disk at that
    rate would be absurd.
    """
    if time.time() - _agents_cache["t"] < 1.5:
        return _agents_cache["list"]
    try:
        sid, cwd = target_session()
        _agents_cache["list"] = _thinking().agents_live(sid, cwd)
    except Exception:
        _agents_cache["list"] = []
    _agents_cache["t"] = time.time()
    return _agents_cache["list"]


_hist_sid = {"t": 0.0, "key": None, "sid": ""}


def history_session() -> str:
    """Which conversation the panel is showing.

    The pane's session when its title names one, and spokenlog.follow() for
    when it does not -- the same policy `claude-voice history` follows, so the
    two never disagree about whose history you are reading.
    """
    sid, cwd = target_session()
    if sid:
        return sid
    if time.time() - _hist_sid["t"] < 2.0 and cwd == _hist_sid["key"]:
        return _hist_sid["sid"]  # the panel asks 20 times a second
    try:
        sid = _spokenlog.follow("", cwd)
    except Exception:
        sid = ""
    _hist_sid.update(t=time.time(), key=cwd, sid=sid)
    return sid


_hist_cache = {"mtime": -1.0, "w": -1, "sid": None, "rows": []}


def history_rows(width: int) -> list:
    """The spoken log wrapped to the panel: (text, side, continuation) rows.

    The log of the session being watched -- the one `t` switches, the one
    dictation reaches -- as resolved by history_session(). Empty when that
    session has said nothing yet, which is the honest answer: a panel that
    borrows another conversation to look busy is worse than a blank one.

    Cached on the log's mtime, the width and the session. The HUD redraws 20
    times a second and the file only changes when something is actually said,
    so re-reading per frame would be pure waste.
    """
    if _spokenlog is None or width < 20:
        return []
    try:
        sid = history_session()
        mt = _spokenlog.mtime(sid)
    except Exception:
        return []
    if mt == _hist_cache["mtime"] and width == _hist_cache["w"] and sid == _hist_cache["sid"]:
        return _hist_cache["rows"]

    try:
        entries = _spokenlog.tail(int(CFG.get("history.show", 200) or 200), sid)
    except Exception:
        entries = []

    you, said = L("history_you", "you"), L("history_said", "said")
    pad = max(len(you), len(said))
    rows = []
    for e in entries:
        mine = e["side"] == "in"
        when = time.strftime("%H:%M", time.localtime(e["t"])) if e["t"] else "     "
        # Who said it is carried by the label, the arrow and the colour: one
        # of the three surviving a narrow terminal or a mono theme is enough.
        head = f"{when}  {(you if mine else said):>{pad}} {'›' if mine else '‹'} "
        body = textwrap.wrap(e["text"], max(8, width - len(head))) or [""]
        rows.append((head + body[0], e["side"], False))
        for cont in body[1:]:
            rows.append((" " * len(head) + cont, e["side"], True))
    _hist_cache.update(mtime=mt, w=width, sid=sid, rows=rows)
    return rows


def panel_open() -> bool:
    try:
        return PANEL_OPEN.exists()
    except Exception:
        return False


def set_panel_open(on: bool) -> None:
    """Remember the panel across restarts. Never worth an exception."""
    try:
        if on:
            PANEL_OPEN.parent.mkdir(parents=True, exist_ok=True)
            PANEL_OPEN.touch()
        else:
            PANEL_OPEN.unlink(missing_ok=True)
    except Exception:
        pass


def position() -> str:
    """Where the panel sits: left, right or bottom. Anything else reads left."""
    pos = str(CFG.get("history.position", "left") or "left").strip().lower()
    return pos if pos in ("left", "right", "bottom") else "left"


def read_state() -> dict:
    """What the session on screen is doing, with the speaker laid over it.

    Two questions, two files. What a SESSION is doing is per session (turn.py),
    because three windows and a bot can all be busy at once and only one of
    them is the one being watched. What the SPEAKER is doing is global -- there
    is one pair of them -- so it wins while it is playing, but only when the
    line belongs to the session on screen.

    Before this they shared one state.json, and whichever session finished
    first wrote "ready" over everyone: the reactor went calm while the window
    in front of you was still working.
    """
    turn = _mod("turn")
    sid, _ = target_session()
    try:
        # Unknown session (no tmux, no pane title yet): fall back to the
        # liveliest one, which is the guess the HUD made before any of this.
        d = turn.read(sid) if sid else turn.newest()
    except Exception:
        d = {"state": "idle", "text": "", "until": 0, "ts": 0, "session": ""}

    try:
        sp = json.loads(STATE.read_text())
    except Exception:
        return d
    # "speaking" expires on its own: the audio is over whether or not anyone
    # said so. Past that, the session's own state is the honest answer.
    if sp.get("state") != "speaking":
        return d
    if sp.get("until", 0) and time.time() > sp["until"]:
        return d
    owner = sp.get("session", "")
    # No owner recorded (the CLI, or a queue item from before this change):
    # show it rather than swallow it. Silence would be the worse error.
    if owner and sid and owner != sid:
        return d
    return sp


def conversation_alive() -> bool:
    """Is the continuous listening daemon running? A pidfile is not an answer:
    a session that died leaves one behind, and nothing would ever start again."""
    try:
        os.kill(int(LISTEN_PID.read_text().strip()), 0)
        return True
    except Exception:
        LISTEN_PID.unlink(missing_ok=True)
        return False


def conversation_stop() -> None:
    """Stop it, and make sure the microphone actually closed."""
    try:
        # The whole group: pw-record is its child.
        os.killpg(int(LISTEN_PID.read_text().strip()), signal.SIGTERM)
    except Exception:
        pass
    LISTEN_PID.unlink(missing_ok=True)
    # Verify, do not assume: if the mic is still open after the signal, an
    # orphan was left and has to be swept.
    for _ in range(12):
        time.sleep(0.15)
        # fresh=True: the one-second cache is what makes the HUD cheap, and
        # exactly what must not be trusted here.
        if not mic_open(fresh=True):
            break
    else:
        sweep_orphans()


# Where the listening daemon's own log goes. Its stderr is pointed at the
# same file, which is the part that matters: listen.py logs plenty once it is
# running, and none of that helps when it dies on an import before its logger
# exists. Discarding that stderr is how conversation mode came to fail in
# perfect silence -- the button lit, the daemon was already dead, and nothing
# anywhere said so.
LISTEN_LOG = BASE / "listen.log"


def conversation_start() -> None:
    try:
        BASE.mkdir(parents=True, exist_ok=True)
        err = open(LISTEN_LOG, "a")
    except Exception:
        err = subprocess.DEVNULL
    subprocess.Popen(
        [sys.executable, str(HERE / "listen.py")],
        stdout=subprocess.DEVNULL,
        stderr=err,
        start_new_session=True,
    )


def listen_failed() -> str:
    """The last thing the daemon said before it stopped being there.

    Only ever asked after it failed to come up, and kept to one line: this is
    a HUD, and the useful half of a Python traceback is its last line anyway.
    """
    try:
        tail = LISTEN_LOG.read_text().strip().splitlines()[-1]
    except Exception:
        return ""
    return tail.strip()[-90:]


def run(script: str, *args, detach: bool = False) -> None:
    cmd = [sys.executable, str(HERE / script), *args]
    if detach:
        subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True
        )
    else:
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)


def history_entries(n: int = 0) -> list:
    """The spoken log of the session being watched, unwrapped.

    history_rows() above wraps to a character width because curses has to. A
    browser does its own wrapping, and wrapping twice is how a sentence ends
    up broken mid-word inside a box that had room for it. So the raw entries
    are the shared answer and the terminal's wrapping is the terminal's
    business.
    """
    if _spokenlog is None:
        return []
    try:
        sid = history_session()
        n = n or int(CFG.get("history.show", 200) or 200)
        return _spokenlog.tail(n, sid)
    except Exception:
        return []


# --- the graphics card ---------------------------------------------------
#
# Read out of sysfs, like everything else here. amdgpu publishes utilisation
# and VRAM directly; nvidia needs its own tool, which is asked for only when
# sysfs has nothing. Neither is a dependency: a machine with no readable GPU
# shows no GPU rows, rather than three zeros pretending to be a measurement.

DRM = Path("/sys/class/drm")
# Every distro ships one of these, and it is the difference between a card
# called "PULSE RX 7900 XTX" and one called "1002:744c".
PCI_IDS = ("/usr/share/hwdata/pci.ids", "/usr/share/misc/pci.ids")

_gpu_cache = {"t": 0.0, "val": None, "card": None, "name": None}


def _pci_name(pci_id: str, sub_vendor: str, sub_device: str) -> str:
    """The board's name, as specific as the database can be.

    Two cards can share a device id and be different products -- an XTX and a
    GRE differ in the subsystem id and nowhere else -- so the subsystem line
    is tried first and the generic device line is the fallback.

    The file is a three-level indent: vendor at the margin, device under it,
    subsystem under that. Walking it is a dozen lines and saves shelling out
    to lspci on every machine that has one and not the other.
    """
    try:
        vendor, device = pci_id.lower().split(":")
    except ValueError:
        return pci_id
    sub = f"{sub_vendor} {sub_device}".lower()

    for path in PCI_IDS:
        try:
            lines = Path(path).read_text(errors="ignore").splitlines()
        except Exception:
            continue
        in_vendor = in_device = False
        generic = ""
        for line in lines:
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            depth = len(line) - len(line.lstrip("\t"))
            body = line.strip()
            if depth == 0:
                if in_vendor:
                    break  # past our vendor entirely
                in_vendor = body.split()[0].lower() == vendor
            elif depth == 1 and in_vendor:
                if in_device:
                    break  # past our device
                in_device = body.split()[0].lower() == device
                if in_device:
                    generic = body.split(None, 1)[-1]
            elif depth == 2 and in_device and body.lower().startswith(sub):
                return body[len(sub) :].strip()
        if generic:
            # "Navi 31 [Radeon RX 7900 XT/...]" -- the bracket is the part a
            # person recognises, and the codename in front of it is not.
            if "[" in generic and generic.endswith("]"):
                return generic[generic.index("[") + 1 : -1]
            return generic
    return pci_id


def _read(p: Path, cast=str):
    try:
        return cast(p.read_text().strip())
    except Exception:
        return None


def _gpu_card() -> Path:
    """The card worth showing, which is the one with the most memory.

    A desktop with a discrete card also has the one built into the processor,
    and reporting the idle 512 MB one would be true and useless.
    """
    best, most = None, -1
    try:
        cards = sorted(DRM.glob("card[0-9]*"))
    except Exception:
        return None
    for c in cards:
        total = _read(c / "device" / "mem_info_vram_total", int)
        if total and total > most:
            best, most = c / "device", total
    return best


def _gpu_sysfs() -> dict:
    dev = _gpu_cache["card"]
    if dev is None:
        dev = _gpu_cache["card"] = _gpu_card() or False
    if not dev:
        return None
    used = _read(dev / "mem_info_vram_used", int)
    total = _read(dev / "mem_info_vram_total", int)
    if not total:
        return None
    if _gpu_cache["name"] is None:
        pci = ""
        try:
            for line in (dev / "uevent").read_text().splitlines():
                if line.startswith("PCI_ID="):
                    pci = line.split("=", 1)[1].strip()
        except Exception:
            pass
        sv = (_read(dev / "subsystem_vendor") or "").removeprefix("0x")
        sd = (_read(dev / "subsystem_device") or "").removeprefix("0x")
        _gpu_cache["name"] = _pci_name(pci, sv, sd) if pci else "GPU"
    return {
        "name": _gpu_cache["name"],
        "busy": float(_read(dev / "gpu_busy_percent", int) or 0),
        "vram_used": float(used or 0),
        "vram_total": float(total),
    }


def _gpu_nvidia() -> dict:
    """The other half of the world. Asked only when sysfs had nothing, and
    never installed on our account."""
    if not shutil.which("nvidia-smi"):
        return None
    try:
        out = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,utilization.gpu,memory.used,memory.total",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=3,
        ).stdout.strip()
        name, busy, used, total = [x.strip() for x in out.splitlines()[0].split(",")]
    except Exception:
        return None
    return {
        "name": name,
        "busy": float(busy),
        "vram_used": float(used) * 1024**2,
        "vram_total": float(total) * 1024**2,
    }


def gpu_stats() -> dict:
    """The card, or None. Cached: sysfs is cheap, the name lookup is not."""
    if time.time() - _gpu_cache["t"] < 1.5:
        return _gpu_cache["val"]
    try:
        g = _gpu_sysfs() or _gpu_nvidia()
    except Exception:
        g = None
    if g and g["vram_total"]:
        g["vram"] = round(100.0 * g["vram_used"] / g["vram_total"], 1)
    _gpu_cache.update(t=time.time(), val=g)
    return g


_stat_cache = {"t": 0.0, "val": {}, "cpu": None}


def _cpu_sample() -> tuple:
    """(busy, total) jiffies from /proc/stat, or None off Linux."""
    try:
        parts = Path("/proc/stat").read_text().split("\n", 1)[0].split()[1:]
        nums = [int(x) for x in parts[:8]]
    except Exception:
        return None
    total = sum(nums)
    idle = nums[3] + (nums[4] if len(nums) > 4 else 0)
    return total - idle, total


def system_stats() -> dict:
    """CPU, memory and disk, as percentages and as the numbers behind them.

    Read straight out of /proc and statvfs rather than through psutil: this is
    a status window, the numbers are three files away, and the whole point of
    the install being two dependencies is that it stays two.

    CPU is a delta between calls, so the first one after startup reports 0 --
    which is honest, since nothing has been measured yet.
    """
    now = time.time()
    if now - _stat_cache["t"] < 1.5 and _stat_cache["val"]:
        return _stat_cache["val"]

    cpu = 0.0
    sample = _cpu_sample()
    prev = _stat_cache["cpu"]
    if sample and prev and sample[1] > prev[1]:
        cpu = 100.0 * (sample[0] - prev[0]) / (sample[1] - prev[1])
    _stat_cache["cpu"] = sample

    mem_used = mem_total = 0.0
    try:
        info = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            k, _, v = line.partition(":")
            info[k] = float(v.split()[0]) * 1024
        mem_total = info.get("MemTotal", 0.0)
        mem_used = mem_total - info.get("MemAvailable", 0.0)
    except Exception:
        pass

    disk_used = disk_total = 0.0
    try:
        v = os.statvfs(str(Path.home()))
        disk_total = v.f_blocks * v.f_frsize
        disk_used = disk_total - v.f_bavail * v.f_frsize
    except Exception:
        pass

    def pct(used, total):
        return round(100.0 * used / total, 1) if total else 0.0

    _stat_cache["val"] = {
        "cpu": round(max(0.0, min(100.0, cpu)), 1),
        "mem": pct(mem_used, mem_total),
        "mem_used": mem_used,
        "mem_total": mem_total,
        "disk": pct(disk_used, disk_total),
        "disk_free": disk_total - disk_used,
        "disk_total": disk_total,
        "load": list(os.getloadavg()) if hasattr(os, "getloadavg") else [0, 0, 0],
        "gpu": gpu_stats(),
    }
    _stat_cache["t"] = now
    return _stat_cache["val"]


def voice_on() -> bool:
    try:
        return ENABLED.exists()
    except Exception:
        return False


def display_state() -> tuple:
    """(state, label-key, spoken-line, agents) -- the reactor's whole story.

    The precedence is the part worth having in one place, because it is not
    obvious and both windows have to get it identically right:

      you talking wins over anything I am doing;
      ...except when nothing is on the other end, which is louder still;
      agents out replaces "thinking" with who is doing the thinking;
      and the voice being off replaces a calm state, but never a live one.
    """
    d = read_state()
    st = d.get("state", "idle")
    age = time.time() - d.get("ts", 0) if d.get("ts") else 1e9
    if st in ("thinking", "speaking") and age > IDLE_AFTER:
        st = "idle"
    if mic_speaking():
        st = "listening"
    stranded = listen_stranded() if daemon_alive() else ""
    if stranded:
        st = "stranded"

    agents = agents_live()
    if agents and st not in ("speaking", "listening", "stranded"):
        st = "agents"
    if not voice_on() and st in ("idle", "ready"):
        st = "voice_off"
    return st, d.get("text", ""), agents, stranded


# The label each state is drawn with, and the colour role it takes. The
# names are the config keys, so a language pack relabels both windows at once.
STATE_LABELS = {
    "thinking": "thinking",
    "speaking": "speaking",
    "listening": "listening",
    "stranded": "stranded",
    "ready": "ready",
    "idle": "idle",
    "agents": "agents",
    "voice_off": "voice_off",
}


def snapshot() -> dict:
    """Everything a front end needs for one frame, as plain JSON.

    One call, because the web HUD asks over a socket and a dozen round trips
    for one frame would be a dozen chances to show a half-updated window.
    """
    st, said, agents, stranded = display_state()
    show = panels()
    fstate, flabel = focus_state()
    other, other_label = next_language()
    tgt = dictate_target_info()
    return {
        "title": TITLE,
        "state": st,
        "label": L(STATE_LABELS.get(st, "idle"), st.upper()),
        "said": said,
        "agents": agents,
        "voice_on": voice_on(),
        "focus": {"state": fstate, "label": flabel, "here": focus_here()},
        "dictation": {
            "target": dictate_target(),
            "blocked": dictate_blocked(),
            "recording": (BASE / "dictate.pid").exists(),
        },
        "mic": {
            "open": mic_open(),
            "speaking": mic_speaking(),
            "held": mic_held(),
            "daemon": daemon_alive(),
            "stranded": stranded,
            "conversation": conversation_alive(),
        },
        "language": {
            "preset": CFG.preset,
            "name": CFG.get("general.language", ""),
            "next": other,
            "next_label": other_label,
        },
        "session": {
            "id": tgt.get("session", "") or target_session()[0],
            "dir": tgt.get("dir", ""),
            "title": tgt.get("title", ""),
        },
        # The pane's real path, not the pretty name beside it: `dir` is a
        # basename for a label, and a basename resolves against whatever
        # directory this process happens to be in.
        "repo": _repo.info(tgt.get("path", "")) if show["repo"] else {},
        "level": level_shape(),
        "panels": show,
        "system": system_stats(),
        "history": history_entries(),
        "labels": {
            k: L(k, k)
            for k in (
                "history",
                "history_empty",
                "history_you",
                "history_said",
                "mic_ready",
                "mic_hearing",
                "mic_deaf",
            )
        },
        "ts": time.time(),
    }


def level_now(d: dict = None) -> float:
    """How loud the voice is right now, 0..1, whoever is talking.

    The mouth wins over the ear: while a line is playing, the envelope is
    what to move to, and the microphone at that moment is hearing the
    speakers rather than you. `d` is the state the caller already read --
    the curses HUD reads it once a frame and has no reason to read it twice.
    """
    d = read_state() if d is None else d
    if d.get("state") == "speaking" and d.get("env"):
        return _level.at(d["env"], d.get("t0", 0), d.get("step", _level.STEP))
    return _level.live()


# Which blocks the window draws at all. Everything is on by default, because
# a HUD that hides half of itself until you find a config file is a HUD that
# looks broken -- but not everyone works in pull requests, and a panel about
# subagents is noise to somebody who has never launched one. Off is off: the
# question behind a hidden panel is not asked either, so switching the repo
# block off also stops the branch being read and GitHub being called.
PANELS = {"system": True, "repo": True, "session": True, "agents": True}


def panels() -> dict:
    """{name: shown}, one entry per block, straight from the config.

    Both windows read this, and the terminal one honours the entries it has
    something to draw for. A name it does not draw is not an error: the
    config is a description of the HUD, not of one of its two surfaces.
    """
    return {k: bool(CFG.get(f"hud.panels.{k}", v)) for k, v in PANELS.items()}


def repo_now() -> dict:
    """The branch and pull request of the session being watched.

    Both windows ask this; the caching is inside repo.py, where the two
    different costs -- a file read and a network call -- are kept apart.
    """
    return _repo.info(dictate_target_info().get("path", ""))


def ear_level() -> tuple:
    """(anyone listening, how loud), for a caller that wants only the ear.

    One stat and one short read, which is what makes it safe to ask twenty
    times a second from a stream thread.
    """
    return _level.publishing(), _level.live()


def level_shape() -> dict:
    """The whole shape of the line being spoken, for a front end that would
    rather interpolate it itself.

    The browser gets this instead of a level: it repaints sixty times a
    second and the state stream ticks four, so a number sent from here would
    arrive as fifteen frames of the same value. The envelope and the moment
    it started are enough to draw every one of those frames correctly, and
    they are sent once per utterance rather than per frame.
    """
    d = read_state()
    speaking = d.get("state") == "speaking"
    return {
        "env": d.get("env", []) if speaking else [],
        "t0": d.get("t0", 0) if speaking else 0,
        "step": d.get("step", _level.STEP),
        "lead": _level.LEAD,
        "live": _level.live(),
    }


# --- the things a key press does ----------------------------------------
#
# One implementation each, called by the terminal's key handler and by the
# browser's button alike. They return (ok, message): a refusal is a message
# too, and a HUD that swallows one is a HUD you press twice.


def act_voice() -> tuple:
    """Toggle the switch. Turning it off means SHUT UP NOW, not "don't speak
    again": if it is droning mid-answer, this is the key."""
    if voice_on():
        ENABLED.unlink(missing_ok=True)
        run("voice.py", "silence")
        return True, "voice off, silence"
    ENABLED.parent.mkdir(parents=True, exist_ok=True)
    ENABLED.touch()
    return True, "voice on"


def act_focus() -> tuple:
    """Give the voice to the session dictation points at, or take it back."""
    if _focus.pane():
        _focus.clear()
        focus_state(fresh=True)
        return True, "every session speaks"
    # One tmux query is worth it: focusing the session named by a
    # two-second-old cache is how the voice ends up in the window you just
    # switched away from.
    _target_cache["t"] = 0.0
    tgt = dictate_target_info()
    if not tgt.get("pane_id"):
        return False, dictate_blocked(fresh=True) or "no session to focus"
    _focus.set_pane(tgt["pane_id"], dictate_target())
    # The other windows may be mid-sentence right now, and "only this one"
    # should not have to wait out a paragraph.
    run("voice.py", "silence")
    focus_state(fresh=True)
    return True, "this session only"


def act_dictate() -> tuple:
    """Record / stop and deliver. Detached, because transcription takes about
    a second and the window has to keep animating through it."""
    # A recording already under way is always allowed to stop: the refusal is
    # about opening the microphone, not closing it.
    if not (BASE / "dictate.pid").exists():
        why = dictate_blocked(fresh=True)
        if why:
            return False, f"{why} — nothing to dictate to"
    run("dictate.py", "--toggle", detach=True)
    return True, ""


def act_conversation() -> tuple:
    """Toggle continuous listening. Stopping is never refused, or a daemon
    left over from a session since closed could not be killed from here."""
    if conversation_alive():
        conversation_stop()
        return True, "conversation off"
    why = dictate_blocked(fresh=True)
    if why:
        return False, f"{why} — nothing to dictate to"
    conversation_start()
    # Verify, do not assume. The daemon does its own refusing -- no window
    # open, no session to send to -- and it can also die on an import before
    # it refuses anything. Either way the button would go back to "off" one
    # frame later and read as a click that did nothing.
    for _ in range(24):
        time.sleep(0.1)
        if conversation_alive():
            return True, "conversation on"
    return False, listen_failed() or "the listener would not start"


def act_session_next() -> tuple:
    """Point dictation at the next Claude pane, and take the voice with it.

    Leaving the focus behind would mean typing into one window while another
    one answers out loud, which is two settings pretending to be one.
    """
    run("dictate.py", "--next")
    # Refresh now, not in 2 s: the first thing you look at after switching
    # session is whether it has agents, and a HUD that lags reads as one that
    # lies.
    _target_cache["t"] = 0.0
    _agents_cache["t"] = 0.0
    if _focus.pane():
        tgt = dictate_target_info()
        if tgt.get("pane_id"):
            _focus.set_pane(tgt["pane_id"], dictate_target())
            run("voice.py", "silence")
            focus_state(fresh=True)
    return True, dictate_target() or "no session"


def act_language() -> tuple:
    """Next preset in the cycle, relabelled in place.

    Everything else reads the config per invocation and follows on its own.
    The listening daemon read its Whisper language when it started, so it is
    the one thing a switch has to restart.
    """
    listening = conversation_alive()
    ok, msg = _lang.switch_next()
    if not ok:
        return False, msg
    reload_cfg()
    if listening:
        conversation_stop()
        conversation_start()
    return True, msg


def act_sweep() -> tuple:
    n = sweep_orphans()
    return True, f"closed {n}" if n else "nothing to close"


ACTIONS = {
    "voice": act_voice,
    "focus": act_focus,
    "dictate": act_dictate,
    "conversation": act_conversation,
    "session": act_session_next,
    "language": act_language,
    "sweep": act_sweep,
}


def act(name: str) -> tuple:
    fn = ACTIONS.get(name)
    if not fn:
        return False, f"unknown action: {name}"
    try:
        return fn()
    except Exception as e:  # a refusal, never a dead window
        return False, str(e) or e.__class__.__name__
