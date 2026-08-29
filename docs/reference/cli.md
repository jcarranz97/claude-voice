---
title: CLI reference
---

# CLI reference

One console script, `claude-voice`. Everything is a subcommand of it.

```bash
claude-voice --help      # everything, grouped
```

!!! info "The bare name starts a session"

    `claude-voice` with nothing after it is `claude-voice run claude`. Anything beginning with a dash belongs to `claude`, since no subcommand here starts with one — so `claude-voice --resume`, `claude-voice -c` and `claude-voice --model opus` all reach Claude Code untouched.

    Everything else is a verb of ours, typed in full: `status`, not `-s`.

## Starting a session

| Command | |
|---|---|
| `claude-voice` | start a session the ear can type into; opens a HUD if none is up |
| `claude-voice --model opus` | arguments pass straight through to `claude` |
| `claude-voice run <cmd…>` | the long form; takes any command, not just `claude` |
| `claude-voice run -- claude --sessions` | pass down a name that is taken |
| `claude-voice --sessions` | the wrapped sessions that are live |

## The switch

| Command | |
|---|---|
| `claude-voice on` | start speaking (off is the default) |
| `claude-voice off` | stop, and silence anything playing now |
| `claude-voice status` | is it on? which session does it speak in? |
| `claude-voice silence` | panic button: cut all sound now |
| `claude-voice mute` | mute just this session |
| `claude-voice focus` | only this session speaks, the rest go quiet |
| `claude-voice focus --clear` | give every session its voice back |

`focus off` and `focus --off` are accepted spellings of `--clear`. `solo` is a legacy alias for `mute`.

## Language

| Command | |
|---|---|
| `claude-voice lang` | which language speaks, and what else is on disk |
| `claude-voice lang es` | switch to that language pack |
| `claude-voice lang --next` | cycle to the next one, like ++l++ in the HUD |
| `claude-voice lang --fetch es` | download that language's voice |

## Watching

| Command | |
|---|---|
| `claude-voice hud` | the status window |
| `claude-voice hud --terminal` | the same HUD on a character grid |
| `claude-voice hud --url` | print the address, open no window |
| `claude-voice hud --shell <webview\|browser\|none>` | pin the window shell for one run |
| `claude-voice sessions` | what each open session is doing right now |
| `claude-voice history [n]` | the last *n* spoken lines of this conversation (default 40) |
| `claude-voice history [n] --all` | ... of every session on this machine |
| `claude-voice history --session <id>` | ... of a named session |
| `claude-voice monitor` | what has the microphone and speakers, anyone's |
| `claude-voice monitor --watch [secs]` | ... live, until you quit (default 2.0s refresh) |
| `claude-voice agents [uuid]` | the subagents a session has running |

`--tty` and `--curses` are accepted spellings of `hud --terminal`; `--web` is accepted and ignored.

## Speech

| Command | |
|---|---|
| `claude-voice say "text"` | synthesize and play, ignoring the switch |
| `claude-voice ack "text"` | the acknowledgement for that prompt, printed not spoken |
| `claude-voice pron diag <word>…` | why a word sounds wrong, and the TOML that fixes it |
| `claude-voice pron say "…"` | hear a phrase, with a level sanity check |
| `claude-voice pron list` | the active pronunciation rules |
| `claude-voice narrate --tune <words> [<per_turn>]` | narration limits, without editing the config |
| `claude-voice narrate --show` | what those limits currently are |
| `claude-voice queue --status` | what is queued and what is playing |
| `claude-voice queue --drain` | empty the queue |

## Input

| Command | |
|---|---|
| `claude-voice dictate --panes` | list the sessions text can be sent to |
| `claude-voice dictate --pane <id>` | pick the one dictation goes to |
| `claude-voice dictate --next` | cycle, like ++t++ in the HUD |
| `claude-voice dictate --toggle` | start recording / stop and send |
| `claude-voice dictate --can-send` | is there a session to send to? exits non-zero if not |
| `claude-voice dictate --status` | the target and why it is or is not deliverable |
| `claude-voice listen` | conversation mode: continuous listening |
| `claude-voice listen --check` | verify the models and measure latencies |
| `claude-voice mic` | who is holding the microphone, and since when |
| `claude-voice mic --sweep` | close a capture of ours that was left behind |
| `claude-voice mic --once` | run one watchdog check now |
| `claude-voice mic --install` | notify when anyone holds the microphone too long |
| `claude-voice mic --uninstall` | stop watching |

## Setup and diagnosis

| Command | |
|---|---|
| `claude-voice doctor` | check the install and say what is wrong |
| `claude-voice config` | what is in effect, and where it came from |
| `claude-voice hooks` | print the settings snippet, to paste yourself |
| `claude-voice hooks --install` | merge the hooks into `~/.claude/settings.json` |
| `claude-voice hooks --settings <path>` | merge into a different settings file |
| `claude-voice build-acks [lang]` | re-synthesize the cached acknowledgements |
| `claude-voice build-ticks [style]` | regenerate the heartbeat sounds |

`doctor` exits non-zero if any check failed, which makes it usable in a script.

## The hook entry points

These are what the four `settings.json` entries run. You never type them yourself.

| Command | Event |
|---|---|
| `claude-voice hook session-start` | `SessionStart` |
| `claude-voice hook user-prompt-submit` | `UserPromptSubmit` |
| `claude-voice hook message-display` | `MessageDisplay` |
| `claude-voice hook stop` | `Stop` |

An unknown event name exits 2. See [Hooks](hooks.md).

## Running a module directly

The CLI is a thin dispatcher — it `exec`s into a module file, so argv, exit codes and signal handling stay identical to running that file yourself. Nothing requires the dispatcher:

```bash
uv tool run --from claude-voice python -m claude_voice.hudweb
uv run python claude_voice/hudweb.py            # from a clone
```

The mapping from subcommand to module is in [Modules](modules.md).

## The installer

`install.sh`, run from a clone or piped from the repository:

| Flag | |
|---|---|
| `--preset <en\|es>` | which language to set up and fetch a voice for |
| `--no-hooks` | print the hook snippet instead of installing it |
| `--editable` | from a clone: install the working tree itself, not a copy |
| `--no-stt` | deprecated no-op; warns and continues |
