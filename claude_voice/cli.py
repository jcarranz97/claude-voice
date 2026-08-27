"""Single entry point. Everything else is an implementation detail.

This is the console script `uv tool install` puts on PATH. It replaces a bash
launcher whose real work was finding an interpreter with piper in it -- a
question that stops existing once the package owns its own environment.

Subcommands are dispatched with execv rather than by importing and calling,
deliberately: each module is also a hook entry point run as `python speak.py`,
and handing it the process whole keeps argv, exit codes and signal handling
identical to running it directly. A hook that behaves differently depending on
how it was reached is not a thing worth debugging later.
"""
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

USAGE = """claude-voice — local voice, ear and HUD for Claude Code

  The HUD is the application: while one is open Claude Code speaks through
  the hooks, and while none is, nothing of ours runs at all -- no voice, no
  microphone, no heartbeat. Start it in a spare terminal: claude-voice hud

Switch
  claude-voice on                 start speaking (off is the default)
  claude-voice off                stop, and silence anything playing now
  claude-voice focus              only this session speaks, the rest go quiet
  claude-voice focus --clear      give every session its voice back
  claude-voice mute               mute just this session
  claude-voice silence            panic button: cut all sound now
  claude-voice status             is it on? which session does it speak in?
  claude-voice lang               which language speaks, and what else is here
  claude-voice lang es            switch to that language pack
  claude-voice lang --next        cycle to the next one, like l in the HUD
  claude-voice lang --fetch es    download that language's voice

Watch
  claude-voice hud                the status window
  claude-voice history [n]        the last n spoken lines of this conversation
  claude-voice history [n] --all  ... of every session on this machine
  claude-voice sessions           what each open session is doing right now
  claude-voice monitor            what has the microphone and speakers, anyone's
  claude-voice monitor --watch    ... live, until you quit
  claude-voice mic                who is holding the microphone, and since when
  claude-voice mic --sweep        close a capture of ours that was left behind
  claude-voice mic --install      notify when anyone holds it open too long

Speech
  claude-voice say "text"         synthesize and play, ignoring the switch
  claude-voice ack "text"         the acknowledgement, printed not spoken
  claude-voice pron diag <word>…  why a word sounds wrong, and how to fix it
  claude-voice pron say "…"       hear a phrase, with a level sanity check
  claude-voice pron list          the active pronunciation rules

Input (needs tmux)
  claude-voice dictate --panes    list panes running claude
  claude-voice dictate --pane ID  pick the one dictation goes to
  claude-voice dictate --toggle   start recording / stop and send
  claude-voice dictate --can-send is there a session to send to?
  claude-voice listen             conversation mode: continuous listening
  claude-voice listen --check     verify the models and measure latencies

Setup
  claude-voice hooks              print the settings.json snippet to install
  claude-voice doctor             check the install and say what is wrong
  claude-voice config             what is in effect, and where it came from
  claude-voice build-acks [lang]  re-synthesize the cached acknowledgements
  claude-voice build-ticks        regenerate the heartbeat sounds

Config lives in ~/.config/claude-voice/config.toml
Your own language packs live in ~/.config/claude-voice/presets/
"""

# subcommand -> (module, arguments to put in front of the user's own)
ROUTES = {
    "hud":         ("hud.py", []),
    "history":     ("spokenlog.py", []),
    "say":         ("speak.py", []),
    # Always dry: an acknowledgement belongs to a prompt, and there is no
    # prompt here. What this is for is reading the line -- and its cost -- back.
    "ack":         ("ack.py", ["--dry-run"]),
    "dictate":     ("dictate.py", []),
    "listen":      ("listen.py", []),
    "pron":        ("pron.py", []),
    "narrate":     ("narrate.py", []),
    "queue":       ("audioq.py", []),
    "config":      ("config.py", []),
    "lang":        ("lang.py", []),
    "build-acks":  ("voice.py", ["--build-acks"]),
    "build-ticks": ("thinking.py", ["--build"]),
    "agents":      ("thinking.py", ["--agents"]),
    "sessions":    ("turn.py", []),
    "mic":         ("mic.py", []),
    "monitor":     ("monitor.py", []),
    "doctor":      ("doctor.py", []),
}

# What Claude Code calls, keyed by the settings.json event name so that a
# person reading their settings can see which hook is which without a lookup.
HOOKS = {
    "session-start":      ("thinking.py", ["--bind"]),
    "user-prompt-submit": ("voice.py", ["--hook-context"]),
    "message-display":    ("narrate.py", []),
    "stop":               ("speak.py", []),
}

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

The commands carry no paths, so moving or reinstalling claude-voice does not
break them and this snippet does not need pasting again for that reason.

SessionStart notes which tmux pane the conversation is in, before it has said
anything: without it the first dictated line of a conversation has no session
to be filed under and is lost to the history panel.

MessageDisplay drives live narration mid-turn; drop it if you only want the
final line spoken. The voice stays off until you run: claude-voice on
"""


def _exec(module: str, args) -> "NoReturn":
    """Hand the process over. execv, so exit codes and signals are the module's."""
    os.execv(sys.executable, [sys.executable, str(HERE / module), *args])


def main() -> int:
    argv = sys.argv[1:]
    cmd = argv[0] if argv else "status"
    rest = argv[1:]

    if cmd in ("-h", "--help", "help"):
        print(USAGE, end="")
        return 0

    if cmd == "status":
        _exec("voice.py", rest)
    if cmd in ("on", "off", "focus", "mute", "solo", "silence"):
        _exec("voice.py", [cmd, *rest])

    if cmd == "hooks":
        print(SNIPPET, end="")
        return 0

    if cmd == "hook":
        event = rest[0] if rest else ""
        if event not in HOOKS:
            print(f"unknown hook: {event or '(none)'}", file=sys.stderr)
            print(f"expected one of: {', '.join(HOOKS)}", file=sys.stderr)
            return 2
        module, prefix = HOOKS[event]
        _exec(module, [*prefix, *rest[1:]])

    if cmd in ROUTES:
        module, prefix = ROUTES[cmd]
        _exec(module, [*prefix, *rest])

    print(f"unknown command: {cmd}", file=sys.stderr)
    print("try: claude-voice --help", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
