#!/usr/bin/env python3
"""The Claude Code hooks: the snippet to paste, and the merge that installs it.

  claude-voice hooks              print the snippet
  claude-voice hooks --install    merge it into ~/.claude/settings.json

Printing JSON and asking for it to be pasted is honest, and it is also the
step people get wrong. The file usually already has a "hooks" block, so the
paste has to be merged by hand -- and a hand-merge that replaces the block
instead of adding to it takes away whatever else was hooked there, silently,
with no error and nothing on screen to notice. Merging four entries into a
JSON file is a thing a program can do exactly.

So --install does the merge, and the printed snippet stays for the settings
file this does not own: a project's .claude/settings.json, a machine managed
by something else, anyone who would rather look first.

It never turns the voice on. Installing the hooks is the wiring; `claude-voice
on` is the switch, and the switch stays yours to throw.
"""

import json
import sys
from datetime import datetime
from pathlib import Path

# event -> (slug, module, matcher). The matcher is Claude Code's own field and
# is only meaningful where it is written here; None leaves it out entirely,
# which is not the same as an empty string.
EVENTS = {
    "SessionStart": ("session-start", "thinking.py", None),
    "UserPromptSubmit": ("user-prompt-submit", "voice.py", ""),
    "MessageDisplay": ("message-display", "narrate.py", None),
    "Stop": ("stop", "speak.py", None),
}

SETTINGS = Path.home() / ".claude" / "settings.json"

SNIPPET = """Add this to the "hooks" block of ~/.claude/settings.json
(or a project's .claude/settings.json):

  "hooks": {
    "SessionStart": [
      { "hooks": [
        { "type": "command", "command": "claude-voice hook session-start" } ] }
    ],
    "UserPromptSubmit": [
      { "matcher": "", "hooks": [
        { "type": "command", "command": "claude-voice hook user-prompt-submit" } ] }
    ],
    "MessageDisplay": [
      { "hooks": [
        { "type": "command", "command": "claude-voice hook message-display" } ] }
    ],
    "Stop": [
      { "hooks": [
        { "type": "command", "command": "claude-voice hook stop" } ] }
    ]
  }

Or let it merge itself into your user settings, keeping what is already there:

  claude-voice hooks --install

The commands carry no paths, so moving or reinstalling claude-voice does not
break them and this snippet does not need pasting again for that reason.

SessionStart notes which tmux pane the conversation is in, before it has said
anything: without it the first dictated line of a conversation has no session
to be filed under and is lost to the history panel.

MessageDisplay drives live narration mid-turn; drop it if you only want the
final line spoken. The voice stays off until you run: claude-voice on
"""


def group(event: str) -> dict:
    """The one entry we add for an event, in Claude Code's own shape."""
    slug, _module, matcher = EVENTS[event]
    out = {"hooks": [{"type": "command", "command": f"claude-voice hook {slug}"}]}
    if matcher is not None:
        # Insertion order is what json.dump writes, and the matcher reads
        # first in every example Claude Code prints.
        out = {"matcher": matcher, **out}
    return out


def installed(groups, event: str) -> bool:
    """Is this event already ours, in either shape?

    The console script is the current form; older installs named the module
    file directly, and both work. Counting the old one as installed is the
    point -- a merge that cannot see it adds a second entry and the hook runs
    twice, which for the Stop hook means saying the same line twice.
    """
    slug, module, _matcher = EVENTS[event]
    for g in groups or []:
        if not isinstance(g, dict):
            continue
        for h in g.get("hooks") or []:
            cmd = (h or {}).get("command", "") if isinstance(h, dict) else ""
            if f"hook {slug}" in cmd or module in cmd:
                return True
    return False


class Merged:
    """What the merge did, for whoever has to print it."""

    def __init__(self, path: Path):
        self.path = path
        self.added: list = []
        self.already: list = []
        self.backup = None
        self.created = False


def install(path=None) -> Merged:
    """Merge our four hooks into a settings file, keeping everything else.

    Idempotent: an event already hooked to us is left exactly as it is, so
    running this again after an upgrade adds only what is new.
    """
    path = Path(path) if path else SETTINGS
    out = Merged(path)

    text = path.read_text() if path.exists() else ""
    if text.strip():
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            # Refuse rather than repair. A settings file that does not parse
            # is a file somebody is in the middle of editing, and the one
            # thing worse than not installing the hooks is overwriting it.
            raise ValueError(
                f"{path} is not valid JSON ({e}) — fix it, or paste the snippet"
            ) from e
        if not isinstance(data, dict):
            raise ValueError(f"{path} is not a JSON object — paste the snippet instead")
    else:
        data = {}
        out.created = not path.exists()

    hooks = data.get("hooks")
    if hooks is None:
        hooks = data["hooks"] = {}
    if not isinstance(hooks, dict):
        raise ValueError(f'{path} has a "hooks" that is not an object — paste the snippet instead')

    for event in EVENTS:
        groups = hooks.get(event)
        if not isinstance(groups, list):
            groups = hooks[event] = [] if groups is None else [groups]
        if installed(groups, event):
            out.already.append(event)
            continue
        groups.append(group(event))
        out.added.append(event)

    if not out.added:
        return out

    # The rewrite reformats the file, so keep the one it replaced. Timestamped
    # rather than a single .bak, because the second run is exactly when a
    # single one would overwrite the copy worth having.
    if text:
        out.backup = path.with_name(
            path.name + "." + datetime.now().strftime("%Y%m%d-%H%M%S") + ".bak"
        )
        out.backup.write_text(text)

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")
    return out


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    if argv and argv[0] in ("-h", "--help"):
        print(__doc__.strip())
        return 0

    path = None
    do_install = False
    while argv:
        a = argv.pop(0)
        if a == "--install":
            do_install = True
        elif a == "--settings":
            if not argv:
                print("--settings needs a path", file=sys.stderr)
                return 2
            path = argv.pop(0)
            do_install = True
        else:
            print(f"unknown flag: {a}", file=sys.stderr)
            print("usage: claude-voice hooks [--install] [--settings PATH]", file=sys.stderr)
            return 2

    if not do_install:
        print(SNIPPET, end="")
        return 0

    try:
        r = install(path)
    except (ValueError, OSError) as e:
        print(f"hooks: {e}", file=sys.stderr)
        return 1

    if r.added:
        print(f"  {'wrote' if r.created else 'updated'} {r.path}")
        print(f"  added: {', '.join(r.added)}")
        if r.backup:
            print(f"  kept the previous file as {r.backup.name}")
    if r.already:
        print(f"  already installed: {', '.join(r.already)}")
    if not r.added:
        print("  nothing to do")
    print("  the voice stays off until: claude-voice on")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
