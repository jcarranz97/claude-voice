"""The system readout, as rows.

The numbers themselves are read in sysstat.py, which knows nothing about
windows. This file is only the shape they are shown in -- which is the
whole point of the split: the panel became a plugin without the code that
reads /proc having to care.

Percentages get meters, because the question they answer is "how full", and
a bar answers it without being read. The absolutes get tiles, because the
question they answer is "how much", and that one has to be read.
"""

import sysstat

UNITS = ("B", "KB", "MB", "GB", "TB")


def scale(n: float) -> tuple:
    """(number, unit), with the precision that fits: 4.2 GB, then 128 GB."""
    i = 0
    while n >= 1024 and i < len(UNITS) - 1:
        n /= 1024
        i += 1
    return (f"{n:.1f}" if n < 10 else f"{round(n)}"), UNITS[i]


def human(n: float) -> str:
    return " ".join(scale(n))


def pair(a: float, b: float) -> str:
    """One unit where both share it: "21 / 30 GB", not "21 GB / 30 GB". The
    repeated unit is what pushes a tile onto two lines, and it says nothing
    the second time."""
    av, au = scale(a)
    bv, bu = scale(b)
    return f"{av} / {bv} {bu}" if au == bu else f"{av} {au} / {bv} {bu}"


def panel(ctx) -> dict:
    """Immediate, always: sysstat caches on a second-and-a-half clock."""
    s = sysstat.system_stats()
    if not s:
        return {}

    rows = [
        {"label": "cpu", "value": f"{s['cpu']:.0f}%", "meter": s["cpu"]},
        {"label": "ram", "value": f"{s['mem']:.0f}%", "meter": s["mem"]},
        {
            "label": "disk",
            "value": f"{s['disk']:.0f}%",
            "meter": s["disk"],
            # The one row that is ever alarming, and only when it is.
            "state": "warn" if s["disk"] > ctx.get("disk_warn", 90) else None,
        },
    ]
    tiles = [
        {"label": "memory", "value": pair(s["mem_used"], s["mem_total"])},
        {"label": "free", "value": human(s["disk_free"])},
    ]

    # A machine with no readable card shows no card rows at all. Zeros would
    # read as a measurement, and it is the one number here that is not one.
    gpu = s.get("gpu") or {}
    if gpu.get("busy") is not None:
        rows.append({"label": "gpu", "value": f"{gpu['busy']:.0f}%", "meter": gpu["busy"]})
    if gpu.get("vram") is not None:
        rows.append({"label": "vram", "value": f"{gpu['vram']:.0f}%", "meter": gpu["vram"]})
        tiles.append({"label": "vram", "value": pair(gpu["vram_used"], gpu["vram_total"])})

    tiles.append({"label": "load", "value": " ".join(f"{x:.2f}" for x in s["load"])})

    return {
        "title": "system",
        "rows": rows,
        "tiles": tiles,
        # The card, not a measurement of it: what the two GPU meters are of.
        "note": gpu.get("name", ""),
    }
