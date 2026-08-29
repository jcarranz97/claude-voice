---
title: Settings
---

# Settings

Every key `claude-voice` reads, its type and its default.

The file is `~/.config/claude-voice/config.toml`. Values fall back key by key through [four layers](../configuration.md#the-four-layers), so anything omitted here still has a working value.

```bash
claude-voice config      # what is in effect, and where each value came from
```

## `[general]`

| Key | Type | Default | |
|---|---|---|---|
| `name` | string | `"Claude"` | the HUD banner |
| `preset` | string | `"en"` | which language pack to layer in |
| `language` | string | `"English"` | how the preset names its own language, written *in* that language — this is what ++l++ labels itself with |

## `[tts]`

| Key | Type | Default | |
|---|---|---|---|
| `provider` | string | `"piper"` | which engine speaks: `piper` (flat, fast, always there) or `chatterbox` (expressive, ~1–2 s slower). Chatterbox falls back to Piper whenever it cannot run — see [The expressive provider](../voice.md#the-expressive-provider) |
| `threads` | int | `8` | CPU threads for Chatterbox. Measured optimum on a 32-core machine: 4 is slower, 32 thrashes |
| `voice_model` | path | `~/.local/share/piper-voices/en_US-amy-medium.onnx` | the Piper voice. A missing model falls back to another of the same language on disk, preferring `high` over `medium` over `low` |
| `length_scale` | float | `1.0` | `>1` is slower; butler pacing lives near `1.06` |
| `primary_voice` | string | `"en-us"` | the espeak-ng voice that phonemizes the whole line |
| `foreign_voice` | string | `""` | a second espeak voice, spliced in for `pronunciation.foreign_terms`. Blank disables the mixed-phoneme pass |
| `max_chars` | int | `400` | the spoken line is truncated here |

## `[tags]`

Emotion tags the spoken line may carry. They only reach a provider that understands them; everything else has them stripped before synthesis, because Piper reads an unknown tag aloud as ordinary words.

| Key | Type | Default | |
|---|---|---|---|
| `enabled` | bool | `true` | may the model write tags at all. Off still strips them |
| `vocabulary` | list[string] | `[]` | blank uses the built-in list of 19. Narrowing it narrows both what may be written and what is recognised for stripping |
| `instruction` | string | `""` | blank uses the built-in wording, which asks for restraint. This is where you ask for more — a per-language value, like the rest of the register |

The built-in vocabulary is `laugh`, `chuckle`, `sigh`, `groan`, `gasp`, `cough`, `sniff`, `shush`, `clear throat`, `sarcastic`, `dramatic`, `angry`, `happy`, `crying`, `fear`, `surprised`, `whispering`, `narration`, `advertisement`.

`[narration]` is the neutral one — a real trained token meaning plain delivery. Keeping it in the list is what makes "a tag on every line" workable without forcing a feeling onto lines that have none.

## `[instruction]`

| Key | Type | Default | |
|---|---|---|---|
| `enabled` | bool | `true` | inject the prompt addition at all. `false` means the model is never told to write the spoken line |
| `text` | string | `""` | blank builds it from the language pack |

## `[narrate]`

| Key | Type | Default | |
|---|---|---|---|
| `enabled` | bool | `true` | mid-turn narration |
| `word_limit` | int | `50` | spoken whole below this; trimmed to a two-sentence lead-in above |
| `max_per_turn` | int | `12` | notices per turn |
| `min_words` | int | `3` | below this, discarded as noise |

## `[thinking]`

The heartbeat.

| Key | Type | Default | |
|---|---|---|---|
| `enabled` | bool | `true` | |
| `delay` | float | `1.75` | nothing sounds before this, so short turns stay silent |
| `interval` | float | `2.45` | seconds between ticks. Decays past 45s by ×1.6 every half minute, to a cap of 8s |
| `style` | string | `"soft"` | `soft` \| `double` \| `low`. Run `claude-voice build-ticks` after changing |
| `max_run` | float | `150` | hard cap on the tick, in seconds |
| `agent_interval` | float | `4.0` | tick spacing while subagents run; does not decay |
| `agent_max_run` | float | `1800` | absolute elapsed cap while agents run |

## `[ack]`

The line spoken the instant you hit enter.

| Key | Type | Default | |
|---|---|---|---|
| `enabled` | bool | `true` | the acknowledgement at all |
| `contextual` | bool | `true` | ask a small model. `false` always plays a cached phrase and makes no call |
| `model` | string | `"claude-haiku-4-5"` | the model asked |
| `max_words` | int | `9` | a line longer than `max_words + 4` is discarded |
| `timeout` | float | `3.0` | client timeout; no retries. Past it, the cached phrase plays |
| `context` | int | `6` | turns of the spoken log sent with the prompt. `0` sends the prompt alone |
| `skip_quick` | bool | `true` | let the call answer `SILENT` and play nothing at all |
| `phrases` | list[string] | `[]` | blank takes them from the language pack. Index matters: it maps to `ackNN.wav` in the cache |
| `system` | string | `""` | blank takes it from the language pack |
| `context_system` | string | `""` | appended only when history was sent |
| `quick_system` | string | `""` | appended only when `skip_quick` is on |

!!! note "This is the one part that leaves the machine"

    Along with `hud.github`. `contextual = false` removes the call; `context = 0` sends the prompt without any of the conversation.

## `[stt]`

| Key | Type | Default | |
|---|---|---|---|
| `enabled` | bool | `true` | read by `doctor` to decide whether to check the speech-to-text pieces |
| `model` | string | `"small"` | faster-whisper size. `base` mishears technical vocabulary |
| `language` | string | `"en"` | Whisper language |
| `device` | string | `"default"` | ALSA device for push-to-talk. Use a **name** from `arecord -L`, never an index |
| `node` | string | `""` | PipeWire node for conversation mode, from `pw-record --list-targets`. Blank uses the default source |
| `max_secs` | int | `120` | recording cap for one dictation |
| `glossary` | string | `""` | handed to Whisper as an initial prompt |
| `hallucinations` | list[string] | `[]` | phrases Whisper invents over near-silence; a transcript matching one is dropped |

## `[listen]`

Conversation mode's end-of-turn detection.

| Key | Type | Default | |
|---|---|---|---|
| `floor_ms` | float | `700` | minimum silence before the turn model is asked at all |
| `ceil_ms` | float | `2500` | send anyway past this |
| `complete` | float | `0.55` | the probability that counts as a finished phrase |
| `min_speech_ms` | int | `300` | below this, discarded — coughs, key clicks |
| `max_utterance_s` | float | `30` | forced send |

## `[mic.watch]`

| Key | Type | Default | |
|---|---|---|---|
| `enabled` | bool | `true` | |
| `interval` | int | `60` | seconds between checks |
| `after` | int | `300` | held this long before the first notification |
| `repeat` | int | `1800` | minimum seconds between repeats |
| `ignore` | list[string] | `[]` | process names never announced. Empty on purpose — see [the watchdog](../microphone.md#thresholds) |

## `[pronunciation]`

| Key | Type | Default | |
|---|---|---|---|
| `foreign_terms` | list[string] | `[]` | words re-phonemized with `tts.foreign_voice` |
| `overrides` | table string→string | `{}` | word → raw espeak IPA, used verbatim. Wins over `foreign_terms` |

## `[history]`

| Key | Type | Default | |
|---|---|---|---|
| `enabled` | bool | `true` | the spoken log. Off also blinds the acknowledgement, which falls back to the prompt alone |
| `cap` | int | `400` | entries kept per session (floor of 20) |
| `show` | int | `200` | entries the panel reads back |
| `position` | string | `"left"` | `left` \| `right` \| `bottom`; anything else reads as left |
| `keep_days` | int | `7` | a session silent this long is swept away |

## `[hud]`

| Key | Type | Default | |
|---|---|---|---|
| `required` | bool | `true` | while no window is open, nothing of ours runs. `false` gives the older hooks-only behaviour |
| `autostart` | bool | `true` | `claude-voice run` opens a HUD if none is up |
| `github` | bool | `true` | may the repo panel call `gh` at all |
| `title` | string | `""` | blank uses `general.name`, letterspaced |
| `shell` | string | `"auto"` | `auto` \| `webview` \| `browser` \| `none` |
| `on_top` | bool | `true` | keep the window above others; needs XWayland |
| `decorated` | bool | `false` | give the window a title bar |
| `devtools` | bool | `false` | right-click → Inspect in the webview |
| `min_width` | int | `720` | |
| `min_height` | int | `520` | below this the three columns stack into one, which only a browser can reach |

### `[hud.panels]`

Which blocks the window draws. All `true` by default; off is genuinely off, not hidden.

| Key | Type | |
|---|---|---|
| `system` | bool | cpu, memory, disk, gpu |
| `repo` | bool | repository, branch, pull request, checks. `false` stops the branch being read at all |
| `session` | bool | where dictation goes, language, microphone |
| `agents` | bool | the list of running subagents. The reactor still says `AGENTS` |

### The HUD's labels

Every string the window draws is a config key, which is how a [language pack](../languages.md) translates the interface.

| Key | Default |
|---|---|
| `thinking` | `"T H I N K I N G"` |
| `speaking` | `"S P E A K I N G"` |
| `listening` | `"L I S T E N I N G"` |
| `ready` | `"R E A D Y"` |
| `idle` | `"S T A N D I N G   B Y"` |
| `agents` | `"A G E N T S"` |
| `voice_off` | `"V O I C E   O F F"` |
| `history` | `"H I S T O R Y"` |
| `history_empty` | `"nothing spoken yet"` |
| `history_you` | `"you"` |
| `history_said` | `"said"` |
| `mic_ready` | `"ready to listen"` |
| `mic_hearing` | `"hearing you"` |
| `mic_deaf` | `"nothing is listening"` |

## `[preset.<name>]`

Per-language overrides, and the top layer — they hold whichever way the language switch is thrown. Any table above can be nested under one:

```toml
[preset.en.tts]
voice_model = "~/.local/share/piper-voices/en_US-lessac-high.onnx"

[preset.es.tts]
voice_model = "~/.local/share/piper-voices/es_MX-claude-high.onnx"

[preset.es.hud]
title = "JARVIS"
```

This is where a personal setting goes when it belongs to one language. A value written at the top level of your config file is *outranked by the language pack* while a different preset is active — see [the inversion](../configuration.md#the-inversion).
