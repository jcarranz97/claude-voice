---
title: Tutorials
---

# Tutorials

Four short walkthroughs, in the order they are worth doing. Each one ends with something working that you can hear or see, and each one builds on the last — but they are written so that you can start at any of them if you already have the earlier parts running.

<div class="grid cards" markdown>

-   :material-numeric-1-circle: **[Your first spoken line](first-voice.md)**

    Install it, turn it on, and get Claude Code to say something out loud. About ten minutes, most of it a download.

-   :material-numeric-2-circle: **[Talking to Claude](talking-to-claude.md)**

    Dictate a prompt instead of typing it, then leave the microphone open and hold an actual conversation.

-   :material-numeric-3-circle: **[Living in the HUD](the-hud.md)**

    Read the window: the reactor's states, the panels, the spoken log, and the keys that matter when three sessions are open at once.

-   :material-numeric-4-circle: **[Making it yours](making-it-yours.md)**

    Change the voice, switch to Spanish, fix a word that comes out wrong, and turn off the parts of the window you do not use.

</div>

## Before you start

These assume:

- **Linux.** macOS and Windows are not supported yet — see [Platform support](../platforms.md).
- **Claude Code, installed and working.** Run `claude` once and get a session; everything here attaches to that.
- **A working sound output.** If `aplay /usr/share/sounds/alsa/Front_Center.wav` makes a noise, you are fine.
- **A microphone**, for tutorial 2 onwards. Tutorials 1, 3 and 4 do not need one.

You do not need Python — [uv](https://docs.astral.sh/uv/) brings its own — and you do not need tmux, which nothing here uses.

!!! note "These are tutorials, not the reference"

    They take one path through the program and do not stop to list alternatives. When you want the whole surface of a command, [the CLI reference](../reference/cli.md) has it; when you want every configuration key, [Settings](../reference/settings.md) does.
