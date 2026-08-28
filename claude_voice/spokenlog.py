#!/usr/bin/env python3
"""The spoken log: what was actually said out loud, in the order it was heard.

  spokenlog.py [n]                print the last n lines of one conversation
  spokenlog.py [n] --session ID   another session's
  spokenlog.py [n] --all          every session on this machine, interleaved

Why this exists
---------------
The transcript is the wrong place to read the conversation from, because most
of what you HEAR never reaches it: narration is derived on the fly, the
contextual acknowledgement is a separate model call, and a dictated sentence
arrives as an ordinary user message, indistinguishable from something typed.
Only the final `<!-- TTS: -->` line survives. A reader built on the transcript
would show the endings and silently drop everything in between.

So both sides write here instead, at the point where a line becomes sound:

  out   audioq.enqueue()   -- every spoken thing passes through it, in the
                              exact order it is played
  in    dictate.deliver()  -- the one place that knows a sentence was SPOKEN
                              rather than typed (conversation mode goes
                              through it too)

The two learn the session differently. `out` is handed it by the hook that
fires; `in` only holds a tmux pane and has to ask which conversation runs
there, which is why the hooks bind pane to session at SessionStart -- before
that binding existed, the line that OPENED a conversation arrived before the
window had a title to be recognised by, and every conversation lost its first
spoken line to `spoken-default.jsonl`.

Whose history is a question for the READER: `tail()` answers about the session
it is given and about no other. The HUD asks about the session it is watching,
narrows to that pane's project when the title cannot name it, and only falls
back to the liveliest session on the machine when there is no pane at all.

It is read for more than a panel now: ack.py shows the last few turns to the
acknowledgement call, which is why the assistant side being the SPOKEN line
rather than the whole answer is worth stating twice. It is a summary of a
summary -- enough to tell what "try it again" refers to, and cheap, which is
what a call in the latency path can afford.

One file per conversation
-------------------------
History is a SESSION question -- it is the record of one conversation -- so it
is keyed by session id the way turn state is, `spoken-<session-id>.jsonl`.
Shared, the panel was not a dialogue at all: with two windows open it showed a
question from one and an answer from the other, interleaved by the clock, with
nothing on screen saying they belonged to different conversations.

Per file rather than one file with a session field per line, because a filter
would make every read scan other sessions' lines to throw them away, and the
cap would become a machine-wide budget a chatty session could spend on your
behalf. It also ages out the way turn files do.

Two decisions worth knowing about:

* Resuming (`--continue`, `--resume`) comes back with a NEW session id, so its
  history starts empty even though the conversation did not. The old file is
  still there and still readable with --session; nothing is lost, but the
  panel does start blank.
* An existing pre-split `spoken.jsonl` carries no session information and
  cannot be divided after the fact. It is left alone and still shows up under
  --all, rather than being discarded or guessed at.

Rules
-----
* Appending must not slow a hook: one line, opened in append mode, never a
  rewrite. Trimming happens only when the file has grown past twice its cap,
  which is once every few hundred lines, and sweeping only when a session
  writes its first line.
* Fails silent, in both directions. A log that cannot be written must not
  break the voice, and one that cannot be read must not break the HUD.
"""

import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import config as _config  # noqa: E402
import turn as _turn  # noqa: E402

CFG = _config.load()
BASE = _config.BASE
PREFIX = "spoken-"
# Written before history was keyed by session. Read-only from here on: it has
# no session to split it by, so it lives on under --all and nowhere else.
LEGACY = BASE / "spoken.jsonl"

# Rough bytes per entry, used to decide when trimming is worth the read. Only
# the trigger depends on it being right; the trim itself counts real lines.
_AVG = 160

# A log outlives the turn that produced it -- that is the point of a log -- so
# it is swept on a far longer clock than turn.py's six hours.
STALE_DAYS = 7


def cap() -> int:
    """Entries kept per session. Per session is the intent: one window talking
    all afternoon must not push another window's morning off the end."""
    try:
        return max(20, int(CFG.get("history.cap", 400)))
    except Exception:
        return 400


def keep_secs() -> float:
    try:
        return max(1.0, float(CFG.get("history.keep_days", STALE_DAYS))) * 86400
    except Exception:
        return STALE_DAYS * 86400


def path(session: str) -> Path:
    """This session's log. Keyed exactly the way turn.py keys its state, so a
    session has one name across both."""
    return BASE / f"{PREFIX}{_turn.safe_session(session)}.jsonl"


def record(side: str, text: str, session: str = "") -> None:
    """Append one spoken line. `side` is "out" (heard) or "in" (said).

    Without a session the line lands in the `default` log rather than being
    dropped: the CLI speaks with no session to name, and a line nobody can
    attribute is still a line that was said out loud. It should now be rare
    from dictation -- a session is addressable from its pane from the moment
    it starts -- so a `default` log filling up with `in` lines means the
    SessionStart hook is not installed. `claude-voice doctor` says so.
    """
    text = (text or "").strip()
    if not text or not CFG.get("history.enabled", True):
        return
    try:
        BASE.mkdir(parents=True, exist_ok=True)
        log = path(session)
        first = not log.exists()
        line = json.dumps(
            {
                "t": time.time(),
                "side": "in" if side == "in" else "out",
                "session": session or "",
                "text": text,
            },
            ensure_ascii=False,
        )
        with log.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        if first:
            sweep()  # once per session, not once per line
        elif log.stat().st_size > cap() * _AVG * 2:
            _trim(log)
    except Exception:
        pass


def _trim(log: Path) -> None:
    """Keep the last `cap` entries. Written aside and renamed, so a reader
    never sees a half-written log."""
    try:
        lines = log.read_text(encoding="utf-8").splitlines()[-cap() :]
        tmp = log.with_suffix(".jsonl.tmp")
        tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.replace(tmp, log)
    except Exception:
        pass


def files() -> list:
    try:
        return sorted(BASE.glob(f"{PREFIX}*.jsonl"))
    except Exception:
        return []


def sessions() -> list:
    """Every session we have logged a spoken line for."""
    return [p.name[len(PREFIX) : -len(".jsonl")] for p in files()]


_newest = {"t": 0.0, "key": None, "sid": ""}


def newest_session(among: list = None) -> str:
    """The liveliest logged session's id, "" if there is none.

    The fallback for a reader that cannot tell which session it is looking at.
    `among` narrows it to a list of candidates -- the sessions of one project,
    say -- because a guess has to stay inside the question being asked: a pane
    whose title cannot be resolved is still certainly one of the conversations
    of the directory it runs in, and showing it another project's log is the
    interleaving this file exists to end.
    """
    key = tuple(among) if among is not None else None
    if time.time() - _newest["t"] < 2.0 and key == _newest["key"]:
        return _newest["sid"]  # the HUD asks 20 times a second
    allow = {_turn.safe_session(s) for s in among} if among is not None else None
    best, best_m = "", -1.0
    for p in files():
        sid = p.name[len(PREFIX) : -len(".jsonl")]
        if allow is not None and sid not in allow:
            continue
        try:
            m = p.stat().st_mtime
        except OSError:
            continue
        if m > best_m:
            best, best_m = sid, m
    _newest.update(t=time.time(), key=key, sid=best)
    return best


def _entries(log: Path, n: int) -> list:
    try:
        raw = log.read_text(encoding="utf-8").splitlines()
    except Exception:
        return []
    out = []
    for line in raw[-max(1, n) :]:
        try:
            d = json.loads(line)
            if d.get("text"):
                out.append(
                    {
                        "t": float(d.get("t", 0)),
                        "side": "in" if d.get("side") == "in" else "out",
                        "session": str(d.get("session", "")),
                        "text": str(d["text"]),
                    }
                )
        except Exception:
            continue  # one malformed line must not lose the rest
    return out


def tail(n: int = 200, session: str = "") -> list:
    """The last n entries of one conversation, oldest first.

    Named session or nothing: a reader that cannot say whose history it wants
    gets none. Falling back in here is what made the panel show the busiest
    window on the machine instead of the one being watched -- the guess belongs
    to the reader, which is the only side that knows what it is looking at.
    """
    return _entries(path(session), n) if session else []


def tail_all(n: int = 200) -> list:
    """The last n entries said by this machine, whoever said them.

    Interleaved by the clock, which is exactly what the panel must not do --
    but it is the right answer to "what did this box say in the last hour",
    and it is the only way to read the pre-split log.
    """
    out = []
    for log in files() + ([LEGACY] if LEGACY.exists() else []):
        out.extend(_entries(log, n))
    out.sort(key=lambda e: e["t"])
    return out[-max(1, n) :]


def mtime(session: str = "") -> float:
    """When that log last changed, so readers can cache until it does.

    ctime counts as a change: a log that becomes readable again after its
    permissions were fixed is new to a reader, even though its contents are
    not, and a stamp that ignored that would leave the pane empty until the
    next spoken line.
    """
    if not session:
        return 0.0
    try:
        st = path(session).stat()
        return max(st.st_mtime, st.st_ctime)
    except Exception:
        return 0.0


def sweep(max_age: float = 0.0) -> None:
    """Drop logs of sessions that stopped talking days ago.

    Only our own per-session files: the pre-split `spoken.jsonl` is never
    touched, since --all is the only thing that can still show it.
    """
    now = time.time()
    limit = max_age or keep_secs()
    for p in files():
        try:
            if now - p.stat().st_mtime > limit:
                p.unlink(missing_ok=True)
        except OSError:
            pass


def _mod(name: str):
    """A sibling module, loaded late: these are the readers' paths, and
    record() -- which runs inside a hook -- must not pay for them."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(name, HERE / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def follow(session: str = "", cwd: str = "") -> str:
    """Which conversation a reader pointed at (session, cwd) should show.

    The one policy, so the panel and `claude-voice history` never disagree
    about whose history you are looking at. Narrowest answer first:

      session   the pane's title named it: exact, nothing to guess
      cwd       it did not -- a window Claude Code has not titled yet still
                says `Claude Code` -- so the liveliest conversation OF THAT
                PROJECT, and none when that project has said nothing
      neither   no pane to point at, outside tmux: the liveliest on the box

    The middle step is the one that matters. Without it, a session that cannot
    be named showed whichever window had spoken last, which is the same panel
    in every session -- exactly what a shared log looked like.
    """
    if session:
        return session
    if cwd:
        try:
            return newest_session(_mod("thinking").sessions_in(cwd))
        except Exception:
            return ""
    return newest_session()


def target() -> tuple:
    """(session, directory) of the pane dictation reaches, for the CLI."""
    try:
        d = _mod("dictate")
        pane = next((p for p in d.claude_panes() if p["id"] == d.cfg().get("pane")), {})
        return d.target_session(), pane.get("path", "")
    except Exception:
        return "", ""


def main() -> int:
    args = sys.argv[1:]
    every = "--all" in args
    args = [a for a in args if a != "--all"]
    session = ""
    if "--session" in args:
        i = args.index("--session")
        session = args[i + 1] if len(args) > i + 1 else ""
        del args[i : i + 2]
    try:
        n = int(args[0])
    except (IndexError, ValueError):
        n = 40

    if every:
        entries, where = tail_all(n), "this machine"
    else:
        session = session or follow(*target())
        entries = tail(n, session)
        where = f"session {session[:8]}" if session else "no session to read"

    if not entries:
        print(f"  nothing spoken yet ({where})")
        return 0
    for e in entries:
        when = time.strftime("%H:%M", time.localtime(e["t"])) if e["t"] else "     "
        # Only --all can show two conversations at once, so only --all has to
        # say whose line it is.
        who = f" {(e['session'] or '-')[:8]:>8}" if every else ""
        print(f"  {when}{who}  {'you  ›' if e['side'] == 'in' else 'said ‹'} {e['text']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
