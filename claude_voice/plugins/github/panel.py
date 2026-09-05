"""The repository panel, as rows.

repo.py does the asking, at two speeds, and returns whatever it last knew
rather than waiting -- so this function is a formatter and nothing else.

Nothing here says "unknown" or "loading". A row with no answer is not
returned: an empty pull request row on a branch that has never been pushed
reads as a failure to find one, and there is nothing to find.
"""

import repo as _repo

# What the row's colour means, per value repo.py can report. Open and draft
# take none: they are where a pull request lives, not news about it. Closed
# is the one that is worth a second look, because it is the one that means
# the branch went nowhere.
STATES = {"merged": "ok", "closed": "warn"}
CHECKS = {"passing": "ok", "running": "busy", "failing": "warn"}
MARKS = {"passing": "✓", "running": "●", "failing": "✗"}


def panel(ctx) -> dict:
    """Immediate, always: the network call is somebody else's thread."""
    info = _repo.info(ctx.path)
    if not info or not info.get("branch"):
        return {}

    # Two rows, not one: a repository name and a branch name on the same line
    # wrap into three in a panel this narrow, and the wrap lands mid-word.
    rows = [
        {"label": "repo", "value": info.get("name") or "—"},
        {
            "label": "branch",
            "value": info["branch"] + (" (detached)" if info.get("detached") else ""),
            "state": "warn" if info.get("detached") else None,
        },
    ]

    pr = info.get("pr") or {}
    if not pr:
        if not info.get("gh", True):
            # No `gh`, or no GitHub remote. Say so once rather than looking
            # broken -- and say it dimly, because it is not a failure.
            rows.append({"label": "pull request", "short": "pr", "value": "no gh"})
        return {"title": "repo", "mark": "github", "rows": rows}

    state = "draft" if pr.get("draft") and pr.get("state") == "open" else pr.get("state", "")
    rows.append(
        {
            "label": "pull request",
            "short": "pr",
            "value": f"#{pr['number']} · {state}",
            "detail": pr.get("title", ""),
            "state": STATES.get(state),
        }
    )

    # Counts read better as a sentence than as a table, and the failing names
    # are the only thing here that saves a trip to the browser.
    c = pr.get("checks") or {}
    bits = []
    if c.get("running"):
        bits.append(f"{c['running']} running")
    if c.get("fail"):
        bits.append(f"{c['fail']} failing")
    if c.get("pass"):
        bits.append(f"{c['pass']} passing")
    rows.append(
        {
            "label": "checks",
            "value": (
                MARKS.get(c.get("state"), "") + " " + (" · ".join(bits) or "none yet")
            ).strip(),
            "detail": ", ".join(c.get("failing") or []),
            "state": CHECKS.get(c.get("state")),
        }
    )

    return {"title": "repo", "mark": "github", "rows": rows}
