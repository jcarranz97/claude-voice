---
title: Configuring claude-voice
---

# Configuring claude-voice

Everything you edit lives in one file:

```
~/.config/claude-voice/config.toml
```

Set `CLAUDE_VOICE_HOME` to move that directory somewhere else — the whole of it, config and state together.

```bash
claude-voice config      # what is actually in effect, and where each value came from
```

That command is the one to reach for before changing anything, and the one that answers "why is it still doing that" without guessing.

## A config that does something

The starter file the installer writes is nearly empty on purpose. Here is one with the keys people actually change:

```toml
[general]
preset = "en"
name = "Jarvis"                   # the HUD banner

[tts]
length_scale = 1.06               # >1 is slower — butler pacing lives here

[narrate]
word_limit = 50                   # spoken whole below this, trimmed above
max_per_turn = 12

[stt]
device = "plughw:CARD=Headset,DEV=0"   # arecord -L; prefer a NAME over an index
node = "alsa_input.usb-..."            # pw-record --list-targets

[ack]
context = 6                       # turns of spoken history the acknowledgement sees
timeout = 3.0                     # past this, the cached phrase plays instead

[hud]
required = true                   # no window open, nothing of ours runs at all
github = true                     # the repo panel may ask gh about its pull request

[hud.panels]                      # which blocks the window draws; all on by default
system = true                     # cpu, memory, disk, gpu
repo = true                       # repository, branch, pull request, checks
session = true                    # where dictation goes, language, microphone
agents = true                     # the list of running subagents

[history]
enabled = true                    # the spoken log behind the HUD's h panel
position = "left"                 # left, right or bottom of the HUD window
cap = 400                         # lines kept per session; older ones trimmed
keep_days = 7                     # a session silent this long is swept away
```

Every key, its type and its default is in [Settings](reference/settings.md).

## The four layers

Values fall back **key by key**, so a config that sets one value does not wipe out the rest. Lowest priority first:

<div class="annotate" markdown>

1. **Built-in defaults** — a complete working English setup, no config file needed. (1)
2. **The language pack** for the active preset — `presets/<lang>.toml`, bundled or yours.
3. **Your `config.toml`**.
4. **`[preset.<name>]` tables** in your config file — per-language overrides.

</div>

1.  This is why deleting `config.toml` entirely leaves you with a working program rather than a broken one.

And above all four sits `~/.config/claude-voice/preset`, a one-line marker file holding the active language's name, which outranks `general.preset` in your config. That is what [`claude-voice lang`](languages.md) and ++l++ in the HUD write.

### The inversion

There is one exception, and it is the reason a language switch used to look broken while everything else worked.

**While the active preset is not the one your config file names, layers 2 and 3 swap.** The language pack outranks your config file, for the keys the pack defines and only those.

A config file written for Spanish carries Spanish in it — the voice model, the instruction, the acknowledgement phrases. Left on top it would keep speaking Spanish inside the English preset. Your microphone device and your panel position are in no preset, so they never move.

To pin a personal value to a language, use layer 4, which is always on top:

```toml
[preset.en.tts]
voice_model = "~/.local/share/piper-voices/en_US-lessac-high.onnx"

[preset.es.tts]
voice_model = "~/.local/share/piper-voices/es_MX-claude-high.onnx"
```

## When changes take effect

| | |
|---|---|
| The hooks — instruction, acknowledgement, heartbeat, narration, the spoken line | **immediately**. They are short-lived processes that read the config per invocation |
| The HUD's labels and language | reloaded in place |
| `[hud.panels]`, `hud.shell`, window size | when the HUD is next opened |
| Conversation mode's language | when it is next started — the HUD restarts it for you on a language switch |
| `[ack].phrases`, the voice model | after `claude-voice build-acks`, which re-synthesizes the cache |
| `[thinking].style` | after `claude-voice build-ticks` |

Nothing here needs a restart of Claude Code.

## What the config file is not

Three things are stored beside it rather than in it, as one-line marker files, because a tool that rewrites TOML you wrote by hand eventually eats a comment it did not write:

| File | |
|---|---|
| `~/.config/claude-voice/enabled` | the on/off switch |
| `~/.config/claude-voice/preset` | the active language |
| `~/.config/claude-voice/hud-history` | whether the history panel is showing |

Delete any of them and the corresponding config value is back in charge.

The rest of that directory is state — session files, pidfiles, the spoken logs, the acknowledgement cache, the tick sounds. [Keys and files](reference/keys-and-files.md) lists it.

## Where the config comes from, in one command

```bash
claude-voice config
```

It prints the resolved configuration and the provenance of the preset — whether it came from the switch, from `general.preset`, or from the default. Between that and `claude-voice doctor`, which names the voice model actually loaded, there is no configuration question that needs guessing at.
