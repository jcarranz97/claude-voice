---
title: Making it yours
---

# Tutorial 4 — Making it yours

Four changes, in rising order of how much they touch: the register it speaks in, a second language, a word that comes out wrong, and the panels you do not use.

## Step 1 — See what is actually in effect

Before changing anything:

```bash
claude-voice config
```

It prints the resolved configuration **and where each value came from**, which is the only way to answer "why is it still doing that" without guessing.

Four layers, lowest first:

1. **Built-in defaults** — a working English setup, no config file needed.
2. **The language pack** for the active preset — `en` and `es` ship inside the package.
3. **Your `~/.config/claude-voice/config.toml`**.
4. **That file's `[preset.<name>]` tables** — per-language overrides, the top layer.

Values fall back key by key, so a config that sets one thing does not wipe out the rest.

## Step 2 — Change the register

The instruction injected into every prompt is a config value, not a constant. The register belongs to you, not to the tool.

```toml
[instruction]
text = """
Voice output is ON: the user will HEAR this response, not read it.
End every response with a single-line HTML comment:
    <!-- TTS: one short spoken sentence -->
Rules for that sentence:
- 20 words maximum. One sentence where possible.
- Dry and precise. No enthusiasm, no filler.
- State the RESULT, not the procedure.
- NEVER include paths, hashes, commands or code.
- If the response is trivial, write <!-- TTS: SILENT --> to say nothing.
"""
```

Make it terse, make it formal, make it a pirate. It is your ear.

While you are there, the pacing:

```toml
[tts]
length_scale = 1.06     # >1 is slower — butler pacing lives here
```

Nothing needs restarting. The hooks are short-lived processes that read the config per invocation, so the next prompt gets the new instruction.

## Step 3 — Add a second language

A preset carries everything that changes with language: which voice speaks, which acknowledgements are cached, how the model is told to phrase the spoken line, the dictation glossary, the HUD labels and the pronunciation tables.

Download the Spanish voice, then switch:

```bash
claude-voice lang               # what speaks now, and what else is on disk
claude-voice lang --fetch es    # download that voice, if it never was
claude-voice lang es            # switch — or press l in the HUD
```

Press ++l++ in the HUD to cycle, labelled in the language it gives you.

!!! warning "Switching inverts two layers, and this is the part worth knowing"

    A config file written for Spanish carries Spanish in it: the voice model, the instruction, the acknowledgement phrases. Left on top it would keep speaking Spanish inside the English preset — which is exactly what makes a language switch look broken while everything else works.

    So while the active preset is not the one your config file names, **the language pack wins** for the keys it defines, and only those. Your microphone device and your panel position are in no preset, and never move.

To keep a personal setting through a switch, say which language it belongs to:

```toml
[preset.en.tts]
voice_model = "~/.local/share/piper-voices/en_US-lessac-high.onnx"

[preset.es.tts]
voice_model = "~/.local/share/piper-voices/es_MX-claude-high.onnx"
```

That table is the top layer and holds whichever way the switch is thrown.

??? question "Where is the active language stored?"

    In `~/.config/claude-voice/preset`, a file holding a name, next to `enabled` and `hud-history`. Deliberately **not** in `config.toml`: flipping one key there would mean a tool rewriting TOML you wrote by hand, and that eventually eats a comment it did not write.

    Delete the marker and `general.preset` in your config file is back in charge.

### Your own language pack

Copy a bundled one into `~/.config/claude-voice/presets/` and edit it. A user preset named after a bundled pack **shadows** it, so the way to adjust `es` slightly is to copy it there, keep the name, and change what you want. Nothing inside the install needs patching, and an upgrade cannot overwrite what you wrote.

## Step 4 — Fix a word that comes out wrong

Fix it by ear. An automated phoneme diff cannot tell you that "main" came out as two syllables, because both renderings are five phonemes.

```bash
claude-voice pron say "I merged into main"     # hear it
claude-voice pron diag main merge queue        # see both languages, get a fix
claude-voice pron list                         # what is currently overridden
```

`diag` prints the exact TOML to paste. There are two tiers, and the lower one is almost always right:

=== "foreign_terms"

    For words the *second* language says correctly. The primary language phonemizes the whole line — correct prosody, correct word boundaries — and then these are re-phonemized in the foreign voice and spliced in.

    ```toml
    [pronunciation]
    foreign_terms = ["main", "merge", "queue", "cache", "null"]
    ```

=== "overrides"

    For words *neither* language gets right — product names, acronyms — where you write the IPA by hand.

    ```toml
    [pronunciation.overrides]
    kubectl = "kjuːb kəntɹˈoʊl"     # "kube-control", not "ku-BEKTL"
    nginx = "ˈɛndʒɪn ˈɛks"          # "engine-X"
    ```

!!! tip "Do not overcorrect"

    Spanish already renders *commit*, *deploy*, *refactor*, *endpoint*, *server*, *debug*, *token*, *script*, *test*, *lint*, *bug*, *push* the way a developer actually says them. Forcing English on those sounds affected. The shipped `es` pack lists only the terms it genuinely mangles, and the list was curated by listening.

## Step 5 — Turn off what you do not use

Everything in the window is on out of the box, because a HUD that hides half of itself until you find a config file looks broken. But not everybody works in pull requests, and a panel listing subagents is noise to somebody who has never launched one.

```toml
[hud.panels]
system = true                     # cpu, memory, disk, gpu
repo = false                      # no GitHub, no branch row, nothing asked
session = true                    # where dictation goes, language, microphone
agents = false                    # the list goes; the reactor still says AGENTS
```

Off is genuinely off, not hidden: with `repo = false` the branch is not read and `gh` is never called.

The one thing a panel switch does not touch is the reactor, which shows the state of the work rather than a block of the window — waiting on subagents still colours it, and still says so, with the list switched off.

A narrower switch, if you want the branch but not the network:

```toml
[hud]
github = false     # keep the repository and branch, read off disk; ask gh nothing
```

`hud.github` is the only thing in this program that talks to a network.

Changes take effect when the HUD is next opened.

## Step 6 — Two settings worth understanding before you change them

`hud.required` decides whether the HUD is the application:

```toml
[hud]
required = false    # the voice runs on the hooks alone; the HUD becomes a viewer
```

That is right for a machine you never sit in front of and wrong for a laptop with a microphone in it.

`ack` decides how much of the conversation leaves the machine. The contextual acknowledgement — the line spoken the instant you hit enter — costs one small model call per prompt, and that call is sent the last few turns of what was said out loud as well as the prompt itself:

```toml
[ack]
context = 6           # turns of spoken history the call sees; 0 sends the prompt alone
contextual = false    # use the cached phrases instead — no call at all
enabled = false       # skip the acknowledgement entirely
```

`claude-voice ack "some prompt"` prints what would be said, with how long the call took and how many turns it read — the way to choose `ack.context` for your own connection, since a late acknowledgement is worse than a vague one.

## What you have now

- The register, the pacing and the language under your control.
- A pronunciation workbench for the words that come out wrong.
- A window with only the panels you use.

## Next

- [Settings](../reference/settings.md) — every key, its type and its default.
- [Configuration](../configuration.md) — how the layers resolve, in full.
- [Design decisions](../design.md) — why any of it works this way.
