---
title: Settings
---

# Settings

Every key `claude-voice` reads, what it is for, and what you get if you leave it alone.

The file is `~/.config/claude-voice/config.toml`. Values fall back key by key through [four layers](../configuration.md#the-four-layers), so anything omitted here still has a working value — a config that sets one thing does not wipe out the rest.

```bash
claude-voice config      # what is in effect, and where each value came from
```

---

## `[general]`

### `general.name`

The name across the top of the HUD. Letterspaced when drawn, so short names read better than long ones.

**Default**: `"Claude"` · **Type**: `string`

```toml
[general]
name = "Jarvis"
```

### `general.preset`

Which language pack to layer in. Outranked by the `preset` marker file that `claude-voice lang` and ++l++ write, so this is the value that applies when nothing has been switched — see [Languages](../languages.md#where-the-choice-lives).

**Default**: `"en"` · **Type**: `string` — any pack in `presets/`

### `general.language`

How a preset names its own language, on screen, **written in that language** — `Español`, not `Spanish`. It labels the key that switches *into* the preset, and that label is read by somebody who wants that language.

**Default**: `"English"` · **Type**: `string`

---

## `[tts]`

### `tts.provider`

Which engine speaks.

`piper` is local, flat and always there: about 800 ms per line whatever you say, and no emotion at all, ever. `chatterbox` is local, expressive and one to two seconds slower, and takes [emotion tags](#tags) inline in the text.

Piper stays the default and the fallback. If Chatterbox cannot run — no weights, no cloned voice, a broken install — the line is spoken flat rather than not at all, because a voice that fails to a silence is worse than one that fails to a flat delivery.

**Default**: `"piper"` · **Type**: `string` — `piper` | `chatterbox`

```toml
[tts]
provider = "chatterbox"
```

[The expressive provider](../voice.md#the-expressive-provider) covers setup; [Performance](../performance.md#the-voice-piper-against-chatterbox) has the timings.

### `tts.threads`

CPU threads Chatterbox may use for a single ONNX operation. Ignored by Piper.

More is not better. Measured on a 32-core machine, a 31-character line took 1.80 s at 4 threads, 1.42 s at 8, and 2.7–3.7 s at 32, where coordination costs more than the work. Eight is the measured optimum **on that machine** — on a four-core laptop the optimum is lower, which is why this is a knob and not a constant.

**Default**: `8` · **Type**: `int`

### `tts.voice_model`

The Piper voice, as a path to an `.onnx` file. Its `.onnx.json` must sit beside it.

If the named model is missing, another voice of the same language already on disk is substituted rather than failing silent, preferring `high` over `medium` over `low`. `claude-voice doctor` names the model actually loaded, which is how you catch a substitution you did not intend.

**Default**: `"~/.local/share/piper-voices/en_US-amy-medium.onnx"` · **Type**: `path`

!!! warning "Pin it per language, not globally"

    A `voice_model` at the top level of your config outranks the language pack — so switching to Spanish would change everything except the voice doing the speaking. Put it under `[preset.<name>.tts]` instead. See [the inversion](../configuration.md#the-inversion).

### `tts.length_scale`

Speaking rate. Above 1 is slower; butler pacing lives near 1.06.

**Default**: `1.0` · **Type**: `float`

```toml
[tts]
length_scale = 1.06
```

### `tts.primary_voice`

The espeak-ng voice that phonemizes the whole line. Sets the prosody and the word boundaries for everything, including words later swapped out by the foreign pass.

**Default**: `"en-us"` · **Type**: `string` — an espeak-ng code, e.g. `es-419`

### `tts.foreign_voice`

A second espeak-ng voice, used to re-phonemize the words listed in [`pronunciation.foreign_terms`](#pronunciationforeign_terms) and splice them back in.

Blank disables the mixed-phoneme pass entirely, which is what the English pack does — English technical vocabulary is already English. It exists for languages that mangle it: in Spanish, "merge" comes out MER-je without this.

**Default**: `""` · **Type**: `string`

### `tts.max_chars`

The extracted spoken line is truncated here. A guard against a model that ignores its instruction and writes a paragraph, not a target — the instruction asks for one sentence.

**Default**: `400` · **Type**: `int`

---

## `[tags]`

Emotion tags the spoken line may carry, as square brackets inline in the text. They only reach a provider that understands them; **everything else has them stripped before synthesis**, because Piper reads an unknown tag aloud as ordinary words — `[sigh]` comes out as "size".

### `tags.enabled`

Whether the model may write tags at all.

Off still strips them. The two are separate questions on purpose: recognising a tag matters most exactly when nothing can hear it.

**Default**: `true` · **Type**: `bool`

### `tags.vocabulary`

Which tags exist — both what the model may write and what is recognised for stripping.

Blank uses the built-in list of nineteen, read from Chatterbox's own token file: `laugh`, `chuckle`, `sigh`, `groan`, `gasp`, `cough`, `sniff`, `shush`, `clear throat`, `sarcastic`, `dramatic`, `angry`, `happy`, `crying`, `fear`, `surprised`, `whispering`, `narration`, `advertisement`.

`[narration]` is the neutral one — a real trained token meaning plain delivery. Keeping it is what makes "a tag on every line" workable without forcing a feeling onto lines that have none.

**Default**: `[]` — meaning all nineteen · **Type**: `list[string]`

```toml
[tags]
vocabulary = ["sigh", "chuckle", "groan", "narration"]
```

### `tags.instruction`

How the model is told it may act. Appended to the spoken-line instruction, and only when something can actually hear tags.

The built-in wording asks for restraint — "most lines need none" — which in practice produces very few. This is where you ask for more. It is a per-language value like the rest of the register, so a preset carries its own.

**Default**: `""` — meaning the built-in wording · **Type**: `string`

```toml
[tags]
instruction = """
Every spoken line carries exactly one tag. Use [narration] when the line
carries no feeling; that is the correct answer often.
"""
```

---

## `[instruction]`

### `instruction.enabled`

Whether to inject the prompt addition at all.

`false` means the model is never told to write the spoken line, so it never writes one, so nothing is ever spoken. That is a heavier switch than `claude-voice off` and is rarely what you want.

**Default**: `true` · **Type**: `bool`

### `instruction.text`

The register. This is what makes the model write a line *for the ear* rather than a summary of its response.

It is a config value and not a constant on purpose: terse, formal, or a pirate — it is your ear. Blank builds it from the active language pack.

**Default**: `""` — meaning the preset's · **Type**: `string`

See [Changing the register](../voice.md#changing-the-register).

---

## `[narrate]`

Progress spoken between tool calls, so the long middle of a turn is not silence.

### `narrate.enabled`

Dropping the `MessageDisplay` hook entirely has the same effect and saves the process spawn.

**Default**: `true` · **Type**: `bool`

### `narrate.word_limit`

Below this many words a block is spoken whole; above it, trimmed to a two-sentence lead-in.

**Default**: `50` · **Type**: `int`

### `narrate.max_per_turn`

Notices per turn. A cap rather than a target — a turn with thirty tool calls should not narrate thirty times.

**Default**: `12` · **Type**: `int`

### `narrate.min_words`

Below this, a block is discarded as noise rather than spoken.

**Default**: `3` · **Type**: `int`

`claude-voice narrate --tune <words> [<per_turn>]` sets the first two without editing the config.

---

## `[thinking]`

The heartbeat: a soft tick so a five-minute turn does not sound like a crash.

### `thinking.enabled`

**Default**: `true` · **Type**: `bool`

### `thinking.delay`

Nothing sounds before this many seconds, so short turns stay silent.

**Default**: `1.75` · **Type**: `float`

### `thinking.interval`

Seconds between ticks. **It decays**: past 45 seconds the gap grows by a factor of 1.6 every half minute, up to 8 seconds, so a long turn gets quieter rather than more insistent.

**Default**: `2.45` · **Type**: `float`

### `thinking.style`

Which tick sound. Run `claude-voice build-ticks` after changing — the sounds are synthesized once, not per tick.

**Default**: `"soft"` · **Type**: `string` — `soft` | `double` | `low`

### `thinking.max_run`

Hard cap on the tick, in seconds. What stops a session that died mid-turn from ticking forever.

**Default**: `150` · **Type**: `float`

### `thinking.agent_interval`

Tick spacing while subagents are running. Does not decay — a wait with an owner is a different sound from thinking.

**Default**: `4.0` · **Type**: `float`

### `thinking.agent_max_run`

Absolute elapsed cap while agents run. Longer than `max_run` because agent work legitimately is.

**Default**: `1800` · **Type**: `float`

---

## `[ack]`

The line spoken the instant you hit enter, so a long turn does not open with silence.

### `ack.enabled`

**Default**: `true` · **Type**: `bool`

### `ack.contextual`

Ask a small model what to say, rather than playing a cached phrase.

`false` always plays from the cache and makes no network call at all, which is the setting to reach for if you would rather nothing left the machine.

**Default**: `true` · **Type**: `bool`

### `ack.model`

Which model is asked.

**Default**: `"claude-haiku-4-5"` · **Type**: `string`

### `ack.max_words`

A returned line longer than `max_words + 4` is discarded and the cache plays instead. The slack is deliberate — a model that overshoots by a word has still said something useful.

**Default**: `9` · **Type**: `int`

### `ack.timeout`

Seconds to wait. No retries: past this the cached phrase plays, because an acknowledgement that arrives after the answer is worse than a vague one.

**Default**: `3.0` · **Type**: `float`

### `ack.context`

Turns of the [spoken log](../history.md) sent with the prompt.

Without them the call can only paraphrase the sentence it was handed, which is how "try it again with the flag" comes back as "Retrying with the flag" — a line with no content in it. It is also what lets the call quietly read "bump" where the microphone heard "pump".

`0` sends the prompt alone. Every turn is tokens sent, in the one call that has to beat the answer to the speaker.

**Default**: `6` · **Type**: `int`

```toml
[ack]
context = 0      # the prompt alone; nothing of the conversation leaves
```

### `ack.skip_quick`

Let the call answer `SILENT` and play nothing at all.

Say hello and the answer arrives in about the time an acknowledgement takes to play, so you were told twice that nothing was happening. The test is how long the *answer* takes, not how short the request was — "run the tests" is three words and several minutes.

**Default**: `true` · **Type**: `bool`

### `ack.phrases`

The cached fallback phrases, synthesized once per language into `acks/<preset>/`. Index matters: it maps to the wav filename.

Run `claude-voice build-acks` after changing.

**Default**: `[]` — meaning the preset's · **Type**: `list[string]`

### `ack.system`

The system prompt for the acknowledgement call.

**Default**: `""` — meaning the preset's · **Type**: `string`

### `ack.context_system`

Appended to `ack.system`, and only when `ack.context` actually supplied history. It has to explain what that history *is*: the assistant lines in it are short spoken summaries, not the answers they stood for.

**Default**: `""` — meaning the preset's · **Type**: `string`

### `ack.quick_system`

Appended to `ack.system`, and only when `skip_quick` is on. It is the part that describes when to answer `SILENT`.

**Default**: `""` — meaning the preset's · **Type**: `string`

---

## `[stt]`

### `stt.enabled`

Read by `doctor` to decide whether to check the speech-to-text pieces at all.

**Default**: `true` · **Type**: `bool`

### `stt.model`

faster-whisper size. `base` is faster and mishears technical vocabulary badly enough to be a false economy.

**Default**: `"small"` · **Type**: `string` — `tiny` | `base` | `small` | `medium` | `large-v3`

### `stt.language`

Whisper language for dictation.

**Default**: `"en"` · **Type**: `string`

### `stt.device`

ALSA capture device for push-to-talk dictation, from `arecord -L`.

**Default**: `"default"` · **Type**: `string`

!!! danger "Use a name, never an index"

    ALSA card *numbers* reorder on reconnect. A setup pinned to `plughw:4,0` silently started recording from a webcam microphone — digital silence — the day a card moved, and digital silence looks exactly like a broken microphone.

```toml
[stt]
device = "plughw:CARD=Headset,DEV=0"
```

### `stt.node`

PipeWire node for conversation mode, from `pw-record --list-targets`. Blank uses the default source.

**Default**: `""` · **Type**: `string`

### `stt.max_secs`

Recording cap for one dictation, in seconds.

**Default**: `120` · **Type**: `int`

### `stt.glossary`

Handed to Whisper as its initial prompt. This is what stops `kubectl` becoming three words.

**Default**: `""` — meaning the preset's · **Type**: `string`

### `stt.hallucinations`

Phrases Whisper invents over near-silence. A transcript matching one is dropped rather than delivered — a stray "thanks for watching" typed into your session is worse than nothing.

**Default**: `[]` — meaning the preset's · **Type**: `list[string]`

---

## `[listen]`

Conversation mode's end-of-turn detection. A fixed silence threshold forces a choice between cutting people off and being slow, so these layers exist instead.

### `listen.floor_ms`

Minimum silence before the turn model is asked at all. Below this it is certainly still a pause.

**Default**: `700` · **Type**: `float`

### `listen.ceil_ms`

Send anyway past this, whatever the model thinks.

**Default**: `2500` · **Type**: `float`

### `listen.complete`

The probability from smart-turn that counts as a finished phrase. Lower sends sooner and interrupts more.

**Default**: `0.55` · **Type**: `float` — 0 to 1

### `listen.min_speech_ms`

Below this, the utterance is discarded — coughs, key clicks, a chair.

**Default**: `300` · **Type**: `int`

### `listen.max_utterance_s`

Forced send. A backstop against a model that never decides you have finished.

**Default**: `30` · **Type**: `float`

---

## `[mic.watch]`

The watchdog that notices a microphone left open when no HUD is up to warn you. Installed with `claude-voice mic --install`.

### `mic.watch.enabled`

**Default**: `true` · **Type**: `bool`

### `mic.watch.interval`

Seconds between checks. It is a timer firing a oneshot, not a daemon — a daemon would need something watching *it*, and this exists because the thing that was supposed to be watching had died.

**Default**: `60` · **Type**: `int`

### `mic.watch.after`

Held this long, in seconds, before the first notification.

**Default**: `300` · **Type**: `int`

### `mic.watch.repeat`

Minimum seconds between repeat notifications about the same holder.

**Default**: `1800` · **Type**: `int`

### `mic.watch.ignore`

Process names never announced.

**Ships empty on purpose.** An allow-list written in advance hides the one leak you did not predict, and a notice that fires constantly is one you stop reading — which is the same failure as not having it at all.

**Default**: `[]` · **Type**: `list[string]`

---

## `[pronunciation]`

Fix a word by ear. An automated phoneme diff cannot tell you that "main" came out as two syllables, because both renderings are five phonemes.

### `pronunciation.foreign_terms`

Words re-phonemized with [`tts.foreign_voice`](#ttsforeign_voice) and spliced back into the line.

The tier almost every fix belongs in. Do not overcorrect: Spanish already renders *commit*, *deploy*, *refactor*, *token* and *test* the way a developer actually says them, and forcing English on those sounds affected.

**Default**: `[]` — meaning the preset's · **Type**: `list[string]`

### `pronunciation.overrides`

Word → raw espeak IPA, used verbatim. Wins over `foreign_terms` and over everything else.

For words *neither* language gets right — product names, acronyms. Plurals fall back to the singular.

**Default**: `{}` — meaning the preset's · **Type**: `table` of `string` → `string`

```toml
[pronunciation.overrides]
kubectl = "kjuːb kəntɹˈoʊl"     # "kube-control", not "ku-BEKTL"
nginx = "ˈɛndʒɪn ˈɛks"          # "engine-X"
```

`claude-voice pron diag <word>` prints the exact TOML to paste.

---

## `[history]`

The log of what was said out loud, both sides — behind ++h++ in the HUD.

### `history.enabled`

Off removes the panel **and** blinds the acknowledgement, which falls back to seeing only the prompt. That is the trade: a shorter, vaguer line spoken the instant you hit enter.

**Default**: `true` · **Type**: `bool`

### `history.cap`

Entries kept per session; older ones are trimmed. Floored at 20 however low you set it.

**Default**: `400` · **Type**: `int`

### `history.show`

Entries the panel reads back into memory. Lower than `cap` because the panel scrolls a window, not an archive.

**Default**: `200` · **Type**: `int`

### `history.position`

Where the panel sits. At the bottom it becomes a full-width strip, the microphone notice moves onto the divider, and the single last-spoken line goes — the strip already ends with it.

Anything unrecognised reads as `left`.

**Default**: `"left"` · **Type**: `string` — `left` | `right` | `bottom`

### `history.keep_days`

A session silent this long is swept away entirely.

**Default**: `7` · **Type**: `int`

---

## `[hud]`

### `hud.required`

While no window is open, nothing of ours runs at all — nothing spoken, no acknowledgement, no heartbeat, no microphone held, and no instruction added to your prompts.

`false` gives the older behaviour: the voice runs on the hooks alone and the HUD becomes a viewer. Right for a machine you never sit in front of, wrong for a laptop with a microphone in it.

**Default**: `true` · **Type**: `bool`

### `hud.autostart`

Whether `claude-voice run` opens a HUD if none is up. With `hud.required` on, `false` means a session starts silent until you open one yourself.

**Default**: `true` · **Type**: `bool`

### `hud.github`

Whether the repo panel may call `gh` at all.

The only thing in this program besides the acknowledgement that talks to a network. `false` keeps the repository and branch, which are read off disk, and stops asking about pull requests and checks. Narrower than turning the whole panel off.

**Default**: `true` · **Type**: `bool`

### `hud.shell`

Which window the page opens in.

`auto` tries `webview`, then `browser`, then `none`. **`webview` costs about seven times the CPU of `browser`**, because we take WebKit's GPU renderer away to stop it painting a blank white window on NVIDIA — see [Performance](../performance.md#the-window-browser-against-webview). It buys a frameless window in exchange.

**Default**: `"auto"` · **Type**: `string` — `auto` | `webview` | `browser` | `none`

```toml
[hud]
shell = "browser"    # a title bar, and a seventh of the CPU
```

### `hud.title`

Blank uses `general.name`, letterspaced.

**Default**: `""` · **Type**: `string`

### `hud.on_top`

Keep the window above others. Needs XWayland — GNOME's Wayland compositor refuses "keep above" to every toolkit there is, which is why the launcher sets `GDK_BACKEND=x11`.

**Default**: `true` · **Type**: `bool`

### `hud.decorated`

Give the frameless window a title bar back.

**Default**: `false` · **Type**: `bool`

### `hud.devtools`

Enable right-click → Inspect in the webview.

**Default**: `false` · **Type**: `bool`

### `hud.min_width` and `hud.min_height`

The smallest window the shell will make. Below this the three columns stack into one, which only a browser can reach.

Worth knowing that rasterization cost follows pixel *area*, and the geometry is remembered across restarts — so a window enlarged once stays expensive.

**Default**: `720` and `520` · **Type**: `int`

---

## `[hud.panels]`

Which blocks the window draws. All on by default, because a HUD that hides half of itself until you find a config file looks broken.

Off is genuinely off, not hidden: with `repo = false` the branch is not read and `gh` is never called. The one thing a panel switch does not touch is the reactor, which shows the state of the work rather than a block of the window — waiting on subagents still colours it with the list switched off.

Changes take effect when the HUD is next opened.

### `hud.panels.system`

CPU, memory, disk, and the graphics card's load and VRAM, named by its actual board.

**Default**: `true` · **Type**: `bool`

### `hud.panels.repo`

Repository, branch, pull request and the state of its checks.

**Default**: `true` · **Type**: `bool`

### `hud.panels.session`

Where dictation goes, which language speaks, what the microphone is doing.

**Default**: `true` · **Type**: `bool`

### `hud.panels.agents`

The list of running subagents.

**Default**: `true` · **Type**: `bool`

---

## The HUD's labels

Every string the window draws is a config key, which is how a [language pack](../languages.md) translates the interface. All are `string`.

| Key | Default |
|---|---|
| `hud.thinking` | `"T H I N K I N G"` |
| `hud.speaking` | `"S P E A K I N G"` |
| `hud.listening` | `"L I S T E N I N G"` |
| `hud.ready` | `"R E A D Y"` |
| `hud.idle` | `"S T A N D I N G   B Y"` |
| `hud.agents` | `"A G E N T S"` |
| `hud.voice_off` | `"V O I C E   O F F"` |
| `hud.history` | `"H I S T O R Y"` |
| `hud.history_empty` | `"nothing spoken yet"` |
| `hud.history_you` | `"you"` |
| `hud.history_said` | `"said"` |
| `hud.mic_ready` | `"ready to listen"` |
| `hud.mic_hearing` | `"hearing you"` |
| `hud.mic_deaf` | `"nothing is listening"` |

---

## `[preset.<name>]`

Per-language overrides, and the **top layer** — they hold whichever way the language switch is thrown. Any table above can be nested under one.

This is where a personal setting goes when it belongs to one language. A value written at the top level of your config is *outranked by the language pack* while a different preset is active, which is [the inversion](../configuration.md#the-inversion) and the reason a language switch used to look broken while everything else worked.

```toml
[preset.en.tts]
voice_model = "~/.local/share/piper-voices/en_US-lessac-high.onnx"

[preset.es.tts]
voice_model = "~/.local/share/piper-voices/es_MX-claude-high.onnx"

[preset.es.hud]
title = "JARVIS"
```
