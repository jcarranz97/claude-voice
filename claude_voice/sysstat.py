"""What the machine is doing, read straight out of /proc.

Lifted out of the HUD core so that the panel which shows it can be a
plugin without the plugin having to import the window it is drawn in.
Nothing here knows what a HUD is; it answers one question and caches it.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

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
