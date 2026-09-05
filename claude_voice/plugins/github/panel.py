"""The repository panel, as rows.

repo.py does the asking, at two speeds, and returns whatever it last knew
rather than waiting -- so this function is a formatter and nothing else.
"""

import repo as _repo

STATES = {"OPEN": "ok", "MERGED": "ok", "CLOSED": "warn", "DRAFT": None}
CHECKS = {"passing": "ok", "running": "busy", "failing": "warn"}


def panel(ctx) -> dict:
    """Immediate, always: the network call is somebody else's thread."""
    info = _repo.info(ctx.path)
    if not info or not info.get("branch"):
        return {}

    rows = [
        {
            "label": "branch",
            "value": info["branch"],
            "state": "warn" if info.get("detached") else None,
        }
    ]

    pr = info.get("pr") or {}
    if pr:
        rows.append(
            {
                "label": "pr",
                "value": f"#{pr['number']} {pr.get('state', '').lower()}",
                "state": STATES.get(pr.get("state", "")),
                "action": "open",
            }
        )
        checks = (pr.get("checks") or {}).get("state")
        if checks:
            rows.append({"label": "checks", "value": checks, "state": CHECKS.get(checks)})
    elif not info.get("gh", True):
        # No `gh`, or no GitHub remote. Say so once rather than looking broken.
        rows.append({"label": "pr", "value": "no gh", "state": None})

    return {"title": "repo", "rows": rows}
