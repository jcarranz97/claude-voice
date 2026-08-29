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
from typing import NoReturn

HERE = Path(__file__).resolve().parent

USAGE = """claude-voice — local voice, ear and HUD for Claude Code

  Start your session with `claude-voice` and everything works: the HUD opens
  if it is not open already, and the ear has somewhere to type. The bare name
  is `claude-voice run claude`, so `claude-voice --resume` reaches claude.

  The HUD is the application: while one is open Claude Code speaks through
  the hooks, and while none is, nothing of ours runs at all -- no voice, no
  microphone, no heartbeat. One HUD serves every session.

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
  claude-voice hud --terminal     ... in the terminal instead, for a box
                                  with no desktop
  claude-voice hud --url          ... print the address, open no window
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
  claude-voice voice              the expressive provider: is it ready?
  claude-voice voice --fetch      download its weights (about 490 MB, once)
  claude-voice voice --build      clone your own Piper voice for it to use
  claude-voice voice --say "..."  hear one line, tags and all

Run
  claude-voice                    start a session the ear can type into
  claude-voice --model opus       ... arguments pass straight through
  claude-voice run claude         ... the same thing, spelled out
  claude-voice --sessions         the wrapped sessions that are live

Input
  claude-voice dictate --panes    list the sessions text can be sent to
  claude-voice dictate --pane ID  pick the one dictation goes to
  claude-voice dictate --toggle   start recording / stop and send
  claude-voice dictate --can-send is there a session to send to?
  claude-voice listen             conversation mode: continuous listening
  claude-voice listen --check     verify the models and measure latencies

Setup
  claude-voice hooks --install    merge the hooks into ~/.claude/settings.json
  claude-voice hooks              ... or just print them, to paste yourself
  claude-voice doctor             check the install and say what is wrong
  claude-voice config             what is in effect, and where it came from
  claude-voice build-acks [lang]  re-synthesize the cached acknowledgements
  claude-voice build-ticks        regenerate the heartbeat sounds

Config lives in ~/.config/claude-voice/config.toml
Your own language packs live in ~/.config/claude-voice/presets/
"""

# subcommand -> (module, arguments to put in front of the user's own)
ROUTES = {
    "history": ("spokenlog.py", []),
    "say": ("speak.py", []),
    # Always dry: an acknowledgement belongs to a prompt, and there is no
    # prompt here. What this is for is reading the line -- and its cost -- back.
    "ack": ("ack.py", ["--dry-run"]),
    # Everything after `run` is the child's: no parsing here, no flags of
    # our own, so a `--model` or a `--resume` reaches claude untouched.
    "run": ("run.py", []),
    "dictate": ("dictate.py", []),
    "listen": ("listen.py", []),
    "pron": ("pron.py", []),
    # The expressive provider: fetch its weights, clone the Piper voice
    # it imitates, and hear one line without touching the switch.
    "voice": ("chatterbox.py", []),
    "narrate": ("narrate.py", []),
    "queue": ("audioq.py", []),
    "config": ("config.py", []),
    "lang": ("lang.py", []),
    "build-acks": ("voice.py", ["--build-acks"]),
    "build-ticks": ("thinking.py", ["--build"]),
    "agents": ("thinking.py", ["--agents"]),
    "sessions": ("turn.py", []),
    "mic": ("mic.py", []),
    "monitor": ("monitor.py", []),
    "doctor": ("doctor.py", []),
    "hooks": ("hooks.py", []),
}

# What Claude Code calls, keyed by the settings.json event name so that a
# person reading their settings can see which hook is which without a lookup.
HOOKS = {
    "session-start": ("thinking.py", ["--bind"]),
    "user-prompt-submit": ("voice.py", ["--hook-context"]),
    "message-display": ("narrate.py", []),
    "stop": ("speak.py", []),
}


def _exec(module: str, args) -> NoReturn:
    """Hand the process over. execv, so exit codes and signals are the module's."""
    os.execv(sys.executable, [sys.executable, str(HERE / module), *args])


def main() -> int:
    argv = sys.argv[1:]

    if argv and argv[0] in ("-h", "--help", "help"):
        print(USAGE, end="")
        return 0

    # The bare name starts a session, because that is the thing typed every
    # day and `run claude` is two words nobody should have to remember. A
    # leading flag belongs to the child for the same reason: no verb of ours
    # begins with a dash, so `claude-voice --resume` can only have meant
    # claude. `status` kept the bare name until it turned out to be the one
    # command you type when something is already wrong, which is rare.
    if not argv or argv[0].startswith("-"):
        _exec("run.py", argv)

    cmd = argv[0]
    rest = argv[1:]

    if cmd == "status":
        _exec("voice.py", rest)
    if cmd in ("on", "off", "focus", "mute", "solo", "silence"):
        _exec("voice.py", [cmd, *rest])

    if cmd == "hook":
        event = rest[0] if rest else ""
        if event not in HOOKS:
            print(f"unknown hook: {event or '(none)'}", file=sys.stderr)
            print(f"expected one of: {', '.join(HOOKS)}", file=sys.stderr)
            return 2
        module, prefix = HOOKS[event]
        _exec(module, [*prefix, *rest[1:]])

    # Same HUD, two surfaces. Both read hudcore, so they cannot disagree
    # about what is on screen -- only about how it is drawn.
    #
    # The window is the default because it is the better one: curves instead
    # of ring glyphs, panels that size themselves, and a layout that is not
    # eleven columns wide because the font is. The terminal one stays for the
    # machine with no desktop, the ssh session, and the spare pane -- which is
    # what --terminal is for. --web is still accepted, so an alias written
    # before this still lands where it meant to.
    if cmd == "hud":
        tty = {"--terminal", "--tty", "--curses"}
        if tty & set(rest):
            _exec("hud.py", [a for a in rest if a not in tty])
        _exec("hudweb.py", [a for a in rest if a != "--web"])

    if cmd in ROUTES:
        module, prefix = ROUTES[cmd]
        _exec(module, [*prefix, *rest])

    print(f"unknown command: {cmd}", file=sys.stderr)
    print("try: claude-voice --help", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
