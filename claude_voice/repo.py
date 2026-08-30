#!/usr/bin/env python3
"""What the session on screen is working on: a branch, and what GitHub thinks.

The HUD already says which session it is watching. This says what that session
is IN -- the repository, the branch, the pull request it belongs to, and
whether the checks on it are still running. It exists for the ten minutes
after a push, when the only question in the room is whether the thing went
green, and the honest answer used to require another window.

Two speeds, because the questions cost wildly different amounts:

  the branch   a file read. Asked every couple of seconds, so switching
               branches shows up almost at once.

  the rest     `gh`, over the network, about a second. Asked on a clock in a
               background thread and never on the path that draws a frame:
               a HUD that stalls for a second is a HUD that stutters every
               time somebody's CI is slow. Faster while a check is running,
               because that is the number being watched, and slower once
               everything has settled.

Nothing here is required. No repository, no `gh`, no network, no GitHub
remote: each one just removes a line from the window. The whole block is off
with `plugins.github.network = false` for anyone who would rather this
to GitHub at all -- it is the only thing in it that leaves the machine.
"""

import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import config as _config  # noqa: E402

CFG = _config.load()

BRANCH_TTL = 2.0  # a file read; the branch may change under us any time
PR_TTL = 60.0  # settled: nobody is waiting on this number
PR_BUSY_TTL = 12.0  # something is running: this IS what is being watched
PR_GONE_TTL = 300.0  # no gh, or no GitHub remote: stop asking so often
GH_TIMEOUT = 12.0  # it is a network call, and networks hang

SHA = re.compile(r"^[0-9a-f]{7,40}$")


def enabled() -> bool:
    return bool(CFG.get("plugins.github.network", True))


def root(start: str) -> Path:
    """The repository a directory is in, or None. Walks up, like git does."""
    try:
        p = Path(start).resolve()
    except Exception:
        return None
    if not p.is_dir():
        p = p.parent
    for d in (p, *p.parents):
        if (d / ".git").exists():
            return d
    return None


def _head_file(gitdir: Path) -> Path:
    """.git is a directory in a clone and a pointer file in a worktree, and a
    worktree's HEAD is the one that matters -- it is the branch you are on."""
    if gitdir.is_file():
        try:
            line = gitdir.read_text().strip()
            if line.startswith("gitdir:"):
                return Path(line.split(":", 1)[1].strip()) / "HEAD"
        except Exception:
            return None
        return None
    return gitdir / "HEAD"


def branch(rt: Path) -> tuple:
    """(name, detached). A detached HEAD gets its short sha, because that is
    what you are on, and calling it a branch would be a lie."""
    head = _head_file(rt / ".git")
    try:
        line = head.read_text().strip()
    except Exception:
        return "", False
    if line.startswith("ref: refs/heads/"):
        return line[len("ref: refs/heads/") :], False
    if SHA.match(line):
        return line[:8], True
    return "", False


def summarise(rollup) -> dict:
    """Counts, and the names of what is failing.

    A count of failures is enough to know the answer is no; the name is what
    saves the trip to the browser to find out which one. Two names, because
    a third line of red in a status panel is not read, it is scrolled past.
    """
    ok = fail = run = 0
    failing = []
    for c in rollup or []:
        # Two shapes come back: check runs have a status and a conclusion,
        # the older commit statuses have only a state.
        if "conclusion" in c or c.get("status"):
            status = (c.get("status") or "").upper()
            done = (c.get("conclusion") or "").upper()
            name = c.get("name", "")
            if status and status != "COMPLETED":
                run += 1
            elif done in ("SUCCESS", "NEUTRAL", "SKIPPED"):
                ok += 1
            elif done:
                fail += 1
                failing.append(name)
        else:
            state = (c.get("state") or "").upper()
            name = c.get("context", "")
            if state == "PENDING":
                run += 1
            elif state == "SUCCESS":
                ok += 1
            elif state:
                fail += 1
                failing.append(name)
    total = ok + fail + run
    return {
        "pass": ok,
        "fail": fail,
        "running": run,
        "total": total,
        # One word for the whole thing, because that is what gets read from
        # across the room. Failing outranks running: a suite that has already
        # lost is not pending, whatever the rest of it is still doing.
        "state": ("none" if not total else "failing" if fail else "running" if run else "passing"),
        "failing": failing[:2],
    }


def _gh(rt: Path, br: str) -> dict:
    """Ask GitHub about this branch. Returns {} when there is nothing to say
    and {"gh": False} when there is nobody to ask."""
    try:
        p = subprocess.run(
            ["gh", "pr", "view", br, "--json", "number,title,state,isDraft,statusCheckRollup"],
            cwd=str(rt),
            capture_output=True,
            text=True,
            timeout=GH_TIMEOUT,
            # gh reads the terminal for its prompts; there is no terminal here
            # and a prompt would hang until the timeout.
            stdin=subprocess.DEVNULL,
            env={**os.environ, "GH_PROMPT_DISABLED": "1", "NO_COLOR": "1"},
        )
    except FileNotFoundError:
        return {"gh": False}  # not installed: say so once, quietly
    except Exception:
        return {}  # a timeout is a slow network, not news
    if p.returncode != 0:
        err = (p.stderr or "").lower()
        if "no pull requests found" in err:
            return {"pr": None}
        # Not a repository we can ask about, or not logged in. Same silence
        # either way: this is a status panel, not a linter for someone's setup.
        return (
            {"gh": False}
            if ("auth" in err or "not a git repository" in err or "no git remotes" in err)
            else {}
        )
    try:
        d = json.loads(p.stdout or "{}")
    except Exception:
        return {}
    return {
        "pr": {
            "number": d.get("number", 0),
            "title": d.get("title", ""),
            "state": (d.get("state") or "").lower(),  # open, merged, closed
            "draft": bool(d.get("isDraft")),
            "checks": summarise(d.get("statusCheckRollup")),
        }
    }


# One asker for the whole process. Two windows watching the same repository
# is one question, and a refresh that is already in flight is not worth a
# second one -- gh is a second of somebody's rate limit either way.
_lock = threading.Lock()
_state = {"key": (), "t": 0.0, "pr": None, "gh": True, "busy": False}


def _refresh(rt: Path, br: str, key: tuple) -> None:
    got = _gh(rt, br)
    with _lock:
        if got.get("gh") is False:
            _state.update(gh=False, pr=None)
        elif "pr" in got:
            _state.update(gh=True, pr=got["pr"])
        _state.update(key=key, t=time.time(), busy=False)


_branch_cache = {"where": None, "t": 0.0, "val": {}}


def local(where: str) -> dict:
    """{name, branch, detached} for a directory, cached on a short clock.

    Cheap is not free: the terminal HUD draws twenty times a second, and
    finding the repository means a stat per directory on the way up. Two
    seconds is far below the time it takes to notice a branch has changed
    and far above the time between two frames.
    """
    now = time.time()
    if _branch_cache["where"] == where and now - _branch_cache["t"] < BRANCH_TTL:
        return dict(_branch_cache["val"])
    rt = root(where) if where else None
    if not rt:
        val = {}
    else:
        br, detached = branch(rt)
        val = {"name": rt.name, "branch": br, "detached": detached, "root": str(rt)}
    _branch_cache.update(where=where, t=now, val=val)
    return dict(val)


def info(where: str) -> dict:
    """What to draw, right now, without waiting for anything.

    Always returns immediately: the branch is fresh, and the pull request is
    whatever the last answer was, with a new one asked for in the background
    if that answer has gone stale. A window that has just opened shows the
    branch alone for a second, which is the correct thing to show while the
    only honest answer to the rest is "asking".
    """
    out = local(where)
    if not out:
        return {}
    rt, br = Path(out.pop("root")), out["branch"]
    if not br or out["detached"] or not enabled():
        # A pull request belongs to a branch. Without one there is nothing
        # to ask about, and asking anyway would be one subprocess per HUD
        # per minute for an answer that cannot exist.
        return out

    key = (str(rt), br)
    with _lock:
        fresh = _state["key"] == key
        pr = _state["pr"] if fresh else None
        out["gh"] = _state["gh"] if fresh else True
        if pr:
            out["pr"] = pr
        busy = pr and pr.get("checks", {}).get("state") == "running"
        ttl = PR_BUSY_TTL if busy else PR_GONE_TTL if fresh and not _state["gh"] else PR_TTL
        due = not fresh or time.time() - _state["t"] > ttl
        if due and not _state["busy"]:
            _state["busy"] = True
            threading.Thread(target=_refresh, args=(rt, br, key), daemon=True).start()
    return out


if __name__ == "__main__":
    where = sys.argv[1] if sys.argv[1:] else os.getcwd()
    print(json.dumps(info(where), indent=2))
    time.sleep(3)  # let the background answer land
    print(json.dumps(info(where), indent=2))
