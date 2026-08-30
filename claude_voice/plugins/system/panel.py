"""The system readout, as rows.

The numbers themselves are read in sysstat.py, which knows nothing about
windows. This file is only the shape they are shown in -- which is the
whole point of the split: the panel became a plugin without the code that
reads /proc having to care.
"""

import sysstat


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

    gpu = s.get("gpu") or {}
    if gpu.get("busy") is not None:
        rows.append({"label": "gpu", "value": f"{gpu['busy']:.0f}%", "meter": gpu["busy"]})
    if gpu.get("vram") is not None:
        rows.append({"label": "vram", "value": f"{gpu['vram']:.0f}%", "meter": gpu["vram"]})

    return {"title": "system", "rows": rows}
