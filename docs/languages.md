---
title: Languages
---

# Languages

A preset carries everything that changes with language: which voice speaks, which acknowledgements are cached, how the model is told to phrase the spoken line, the dictation glossary, the HUD labels, and the pronunciation tables.

English and Spanish ship inside the package. Your own go in `~/.config/claude-voice/presets/`.

## Switching

```bash
claude-voice lang                # what speaks now, and what else is on disk
claude-voice lang es             # switch to it
claude-voice lang --next         # cycle, the same thing l does in the HUD
claude-voice lang --fetch es     # download that language's voice first
```

++l++ in the HUD cycles, labelled in the language it gives you — `Español`, not `Spanish`.

**Nothing needs restarting.** The hooks are short-lived processes that read the config per invocation, so the next prompt gets the new instruction and the next dictation the new Whisper language. The HUD reloads its labels in place. Conversation mode is the one exception — its daemon holds the language for the length of its run — so the HUD restarts it for you when you switch.

### Where the choice lives

In `~/.config/claude-voice/preset`, a file holding a name, next to `enabled` and `hud-history`.

Deliberately **not** in `config.toml`: flipping one key there would mean a tool rewriting TOML you wrote by hand, and that eventually eats a comment it did not write. Delete the marker and the config file's own `general.preset` is back in charge.

`claude-voice config` prints which preset is in effect and which of the two put it there.

## Switching inverts two layers

This is the part worth knowing, and the reason a language switch used to look broken while everything else worked.

A config file written for Spanish carries Spanish in it: the voice model, the instruction, the acknowledgement phrases. Left on top it would keep speaking Spanish inside the English preset.

So **while the active preset is not the one your config file names, the language pack wins** — for the keys it defines, and only those. Your microphone device and your panel position are in no preset, and never move.

### Keeping a personal setting through a switch

Say which language it belongs to:

```toml
[preset.en.tts]
voice_model = "~/.local/share/piper-voices/en_US-lessac-high.onnx"

[preset.es.tts]
voice_model = "~/.local/share/piper-voices/es_MX-claude-high.onnx"
```

That table is the top layer and holds whichever way the switch is thrown. The full resolution order is in [Configuration](configuration.md#the-four-layers).

## Two things that refuse rather than half-work

**A voice that was never downloaded.** Switching into a language whose `.onnx` is missing declines with the reason on screen — switching into a voice that cannot speak is a silent failure. `claude-voice lang --fetch <name>` is the way out, and it caches that language's acknowledgements while it is there.

**The acknowledgement cache** is kept per preset, in `acks/<preset>/`, because it is indexed by position. One shared directory would play the old language's wav while the spoken log recorded the new language's words.

## Writing your own pack

Copy a bundled one and edit it:

```bash
mkdir -p ~/.config/claude-voice/presets
claude-voice config          # tells you where the bundled packs live
```

A user preset named after a bundled pack **shadows** it, so the way to adjust `es` slightly is to copy it there, keep the name, and change what you want. Nothing inside the install needs patching, and an upgrade cannot overwrite what you wrote.

### What a pack contains

```toml
[general]
preset = "es"
language = "Español"          # named in its own language: this labels the HUD key

[tts]
voice_model = "~/.local/share/piper-voices/es_MX-ald-medium.onnx"
primary_voice = "es-419"      # Latin American: seseo, no Castilian theta
foreign_voice = "en-us"       # spliced in for the terms listed below
length_scale = 1.06

[instruction]
text = """..."""              # what the model is told, in that language

[ack]
phrases = ["Permítame.", "Enseguida.", "..."]   # cached, synthesized once
system = """..."""                              # the small model call's system prompt
quick_system = """..."""                        # added only when ack.skip_quick
context_system = """..."""                      # added only when ack.context > 0

[stt]
language = "es"
glossary = "Terminos: Docker, kubectl, pytest, ..."
hallucinations = ["subtítulos realizados por", "amara.org", "gracias por ver"]

[pronunciation]
foreign_terms = ["main", "merge", "queue", "cache", "null"]

[pronunciation.overrides]
kubectl = "kjuːb kontɾˈol"

[hud]
thinking = "P E N S A N D O"
speaking = "H A B L A N D O"
# ... every label the window draws
```

The English pack has no `[hud]` table — the English labels are the built-in defaults.

### After editing a pack

```bash
claude-voice build-acks es     # re-synthesize that language's cached phrases
```

Any change to `[ack].phrases` or to the voice needs this, because the cache is synthesized once.

## Downloading a voice

Voices come from the [rhasspy/piper-voices](https://huggingface.co/rhasspy/piper-voices) collection on Hugging Face, into `~/.local/share/piper-voices/`. Each is a pair of files, an `.onnx` and its `.onnx.json`, and both are required.

```bash
claude-voice lang --fetch en
```

If the model your config names is missing, another voice of the same language already on disk is substituted rather than failing silent — preferring `high` over `medium` over `low`. `claude-voice doctor` names the model actually loaded, which is the fastest way to catch a substitution you did not intend.

## It still speaks the old language

Something above the preset is pinning it. `claude-voice config` prints the preset in effect and where it came from; `claude-voice doctor` names the voice model actually loaded.

The usual culprit is a `[tts] voice_model` — or an `[instruction] text` — in your own `config.toml`, written for the language you switched away from. Move it under `[preset.<name>]` so it applies to that language only.
