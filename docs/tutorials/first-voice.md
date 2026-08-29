---
title: Your first spoken line
---

# Tutorial 1 — Your first spoken line

By the end of this you will have Claude Code speaking one sentence at the end of every turn, in a neural voice running on your own CPU, and you will know which of the four moving parts to blame when it does not.

Ten minutes, most of which is a 63 MB download.

## Step 1 — The system packages

These are your package manager's, and they are the one step nothing here does for you. A script that runs `sudo` on your behalf is not a thing this project does.

=== "Debian / Ubuntu"

    ```bash
    sudo apt install alsa-utils pipewire-bin python3-gi gir1.2-webkit2-4.1
    ```

=== "Fedora"

    ```bash
    sudo dnf install alsa-utils pipewire-utils python3-gobject webkit2gtk4.1
    ```

=== "Arch"

    ```bash
    sudo pacman -S alsa-utils pipewire python-gobject webkit2gtk-4.1
    ```

Only `alsa-utils` is strictly required for this tutorial — it is `aplay`, which plays the sound. The other two are for the microphone and the frameless window, which are tutorials 2 and 3. Installing them now saves coming back.

## Step 2 — The program, a voice, the config and the hooks

```bash
curl -fsSL https://raw.githubusercontent.com/jcarranz97/claude-voice/main/install.sh | bash
```

One command, from any directory, with nothing cloned. It installs `uv` if you do not have it, installs the program, downloads a Piper voice, writes a starter config, synthesizes the cached acknowledgements and the heartbeat sounds, and merges four hooks into `~/.claude/settings.json`.

Everything it does is to your home directory. It never asks for `sudo` — which is why step 1 was a step of yours.

??? question "What are the hooks, and what did it just put in my settings file?"

    Claude Code fires a hook at four moments, and `claude-voice` attaches one command to each: `SessionStart` notes which terminal the conversation is in, `UserPromptSubmit` injects the speaking instruction and plays the acknowledgement, `MessageDisplay` narrates progress mid-turn, and `Stop` speaks the final line.

    The installer **merges** rather than pastes: everything already in that file stays, and the copy it replaced is kept next to it with a timestamp. [Hooks](../reference/hooks.md) is the whole story, including how to install them by hand.

Watch for the last few lines of its output. If it reports a missing system package, go back to step 1; if it says the hooks were printed rather than installed, your settings file could not be parsed and it refused to repair it rather than guess.

## Step 3 — Turn it on

```bash
claude-voice on
```

Off is the default, and the installer deliberately does not change that. While it is off the hook injects nothing at all, so a machine that installed this and never asked for it spends no tokens and makes no sound.

Check that the machinery works before involving Claude Code at all:

```bash
claude-voice say "test one two"
```

You should hear it. If you do not, stop here and run `claude-voice doctor` — the [Troubleshooting](../troubleshooting.md) page is organised by exactly what `doctor` prints.

## Step 4 — Start a session

Start it with `claude-voice` rather than `claude`:

```bash
claude-voice
```

Two things happen. A HUD window opens, and the session it started is one the ear can type into later.

!!! important "The HUD is the application"

    While a HUD is open the hooks speak; while none is open, nothing of ours runs at all — nothing spoken, no acknowledgement, no heartbeat, no microphone held open, and no instruction added to your prompts.

    So if you close the window, the voice goes quiet. That is the design, not a bug, and [Design decisions](../design.md) explains why it is worth the surprise.

Everything after `claude-voice` is handed to `claude` untouched, so `claude-voice --model opus`, `claude-voice --resume` and `claude-voice -c` all work.

## Step 5 — Ask it something

Type a prompt with actual work behind it. Not "hello" — the acknowledgement deliberately stays silent for anything it could simply answer.

```text
run the tests and tell me what fails
```

Here is what you should hear, in order:

1. **An acknowledgement**, almost immediately — a short line naming what it is about to do.
2. **A soft tick**, underneath, for as long as the turn runs.
3. **Progress**, spoken between tool calls, if the turn is long enough to have any.
4. **One final line** when it stops.

And on screen, at the very bottom of Claude's response, an HTML comment:

```html
<!-- TTS: Suite fails. Two cases, both in the parser. -->
```

That comment is the thing that gets spoken. It is written by the model, for the ear, on purpose — the response body is never read aloud, because markdown, diffs and file paths are unlistenable.

## Step 6 — Read back what was said

The line you missed is not gone:

```bash
claude-voice history 20
```

That prints the last twenty spoken lines of this conversation, both sides — what you said and what it said. The same log is a panel in the HUD, behind ++h++.

## What you have now

- A voice that speaks one line per turn, locally.
- A switch: `claude-voice on` and `claude-voice off`, with `claude-voice silence` as the panic button that cuts whatever is playing right now.
- A log of everything said out loud, per conversation.

## If it said nothing

Every turn appends one line to `~/.config/claude-voice/speak.log` saying exactly why it was silent:

```text
2026-08-25 11:19:16 fields=[...] len=73 marker=yes on=True audio=True
```

| What it says | What it means |
|---|---|
| `marker=NO` | The model did not write the `<!-- TTS: -->` line — either the voice was off when you sent the prompt, or it judged the turn not worth speaking |
| `on=False` | Run `claude-voice on` |
| `audio=False` | No PipeWire/PulseAudio session — expected over plain SSH |
| no line at all | The `Stop` hook is not installed; `claude-voice hooks --install` |

[Troubleshooting](../troubleshooting.md) has the rest.

## Next

[Tutorial 2 — Talking to Claude](talking-to-claude.md), where the microphone gets involved.
