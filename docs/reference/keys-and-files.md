---
title: Keys and files
---

# Keys and files

## Keys in the HUD

Both surfaces — the window and the terminal — route every key through one shared implementation, so they cannot disagree about what a key does or why it refused.

| Key | |
|---|---|
| ++m++ / ++space++ | voice off / on, for the whole machine. Off silences whatever is playing, instantly |
| ++f++ | mute every session except the one ++t++ points at, and unmute them again |
| ++l++ | language: switch to the next preset, labelled in the language it gives you |
| ++d++ | dictate: record, transcribe, send |
| ++c++ | conversation mode: continuous listening |
| ++t++ | switch which session receives dictation — the voice follows it |
| ++h++ | history: show or hide the spoken log |
| ++x++ | close an orphaned microphone capture (emergency) |
| ++q++ / ++escape++ | quit |

In the history panel:

| Key | |
|---|---|
| ++arrow-up++ / ++arrow-down++, ++j++ / ++k++ | scroll |
| ++g++ / ++shift+g++ | jump to the ends |

When the panel takes the whole window — under about 74 columns beside the reactor, or too few rows under it — ++q++ closes the panel rather than quitting the HUD. That is the only place the two differ.

In the frameless window, the page also lends the window what a title bar would give it: drag the HUD's bar to move, drag any edge or corner to resize, double-click the bar to maximise, and `✕` to close.

## Files

Everything lives under `~/.config/claude-voice/`. Set `CLAUDE_VOICE_HOME` to move the whole directory.

### Yours to edit

| | |
|---|---|
| `config.toml` | the configuration — see [Settings](settings.md) |
| `presets/` | your own language packs; one named after a bundled pack shadows it |

### Markers — one line each, written by commands

| | |
|---|---|
| `enabled` | the on/off switch. `claude-voice on` / `off` |
| `preset` | the active language. `claude-voice lang`, or ++l++ |
| `hud-history` | whether the history panel is showing. ++h++ |

They are files rather than config keys because a tool that rewrites TOML you wrote by hand eventually eats a comment it did not write. Delete one and the corresponding config value is back in charge.

### State

| | |
|---|---|
| `state.json` | what the speaker is doing, with the envelope of the line being played |
| `turn-<session>.json` | what each session is doing — one file each |
| `pane-<terminal>.json` | which conversation a terminal is bound to |
| `hud-<pid>.pid` | one per open window. Their existence *is* "a HUD is open" |
| `focus.json` | which terminal owns the voice |
| `pane.json` | which session dictation is aimed at |
| `run-<pid>.json` | one per wrapped session, with its pty and socket |
| `queue/`, `player.lock`, `seq`, `playing.pid` | the audio queue and its single player |
| `mic-level`, `mic-active`, `listen-stranded`, `listen.pid` | what the ear is doing |
| `mic-watch.json` | the watchdog's last notification, so it does not repeat |
| `hud-window.json`, `hud-profile/` | where you put the window, and the browser profile if one is used |
| `narrate.json`, `tick.json` | the `--tune` overrides |

### Content

| | |
|---|---|
| `spoken-<session>.jsonl` | the [spoken log](../history.md), one per conversation |
| `acks/<preset>/*.wav` | the cached acknowledgements, per language |
| `tick.wav`, `tick-agents.wav` | the heartbeat sounds |

### Logs

| | |
|---|---|
| `speak.log` | one line per turn saying exactly why something was or was not spoken |
| `dictate.log` | what was recorded, transcribed and delivered |
| `listen.log` | conversation mode's stderr |

`speak.log` is the first thing to read when nothing came out — [Troubleshooting](../troubleshooting.md#nothing-is-spoken) reads it line by line.

## Elsewhere on the machine

| | |
|---|---|
| `~/.local/share/piper-voices/` | the voice models, `.onnx` and `.onnx.json` in pairs |
| `~/.claude/settings.json` | the four [hooks](hooks.md) |
| `~/.claude/sessions/` | Claude Code's own registry, which the wrapper joins against |
| `$XDG_RUNTIME_DIR/claude-voice/` | the delivery sockets, one per wrapped session, mode 0600 |
| `~/.config/systemd/user/claude-voice-mic.{service,timer}` | the [microphone watchdog](../microphone.md), if you installed it |

## Environment variables

| | |
|---|---|
| `CLAUDE_VOICE_HOME` | move the config and state directory |
| `ANTHROPIC_API_KEY` | for the contextual acknowledgement. Falls back to the Claude Code OAuth credential |
| `XDG_RUNTIME_DIR` | where the delivery sockets live; also how the audio session is detected |
| `TMUX`, `TMUX_PANE` | how a hook inside tmux finds its own pane |
| `CLAUDE_SESSION_ID`, `CLAUDE_PID` | set by Claude Code; how a hook finds its session and its pty |

Two are set by the window launcher rather than read: `GDK_BACKEND=x11` and `WEBKIT_DISABLE_DMABUF_RENDERER=1`. [The HUD](../hud.md#the-window-itself) says why.
