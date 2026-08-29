---
title: Performance
---

# Performance

Two choices in this program cost real time and real CPU, and in both cases the obvious-looking option is not the cheap one. This page is the measurements behind them, so the trade is a number rather than an impression.

## The choices, and what you get by default

| Setting | Options | Default | Costs |
|---|---|---|---|
| `tts.provider` | `piper` · `chatterbox` | **`piper`** | Chatterbox is 1–2 s slower per line and 640 MB on disk, and can act |
| `hud.shell` | `auto` · `webview` · `browser` · `none` | **`auto`**, which tries `browser` first | `webview` costs ~7× the CPU, and buys a frameless window |
| `tags.enabled` | `true` · `false` | **`true`** | nothing — tags are only written when a provider can hear them |
| `tts.threads` | any int | **`8`** | measured optimum; 4 is slower and 32 thrashes |

Every configuration key, with its own default, is in [Settings](reference/settings.md).

!!! note "How these were taken"

    One machine — 32 cores, 29 GB, no GPU acceleration in use — with a load average under 4, so nothing here is contention. CPU figures are instantaneous, sampled from `/proc/<pid>/stat` over a four to five second window, not the lifetime average `ps` reports by default. Memory is PSS rather than RSS, so shared libraries are not counted several times over.

    Your numbers will differ. The *ratios* are the part worth carrying across machines.

## The voice: Piper against Chatterbox

| | Piper — **the default** | Chatterbox Turbo |
|---|---|---|
| `tts.provider` | `"piper"` | `"chatterbox"` |
| A short line (21 chars) | **~800 ms** | 1.1–1.5 s |
| A median line (76 chars) | ~800 ms | 2.6–2.8 s |
| A long line (303 chars) | ~900 ms | 14–15 s |
| Cold start | 596 ms | 1.8 s |
| On disk | 63 MB | 488 MB weights + 153 MB runtime |
| Emotion | none, ever | 19 inline tags |

### Piper barely notices how long the line is

That is the surprising row. 21 characters and 81 characters both take about the same time, because the work is not the speech:

| | |
|---|---|
| `PiperVoice.load()` | **596 ms** |
| Phonemization | ~1 ms |
| Inference, 21 → 81 chars | 116 → 294 ms |

**Three quarters of Piper's cost is loading the model**, and it is paid on every single line, because every hook is a fresh process that imports, loads and exits. Nothing caches across turns.

Two things follow. Piper is effectively a flat ~800 ms per line whatever you say, so its cost scales with how *often* it speaks rather than how much. And there is about 600 ms of latency available to anyone who makes the model resident — which is the single largest easy win left in this program, and is not done.

### Chatterbox scales with length, and wants to stay loaded

Chatterbox is a token-by-token model, so time grows with the line. That is why the 303-character worst case is 14 seconds and why a cap is worth having: your spoken lines average 90 characters and the median is 76, so the common case is 1–3 seconds, but the tail is long.

Its 1.8 s cold load is why `session()` is a cached object rather than a function. Through the CLI, in a fresh process, a line costs about 3.5 s — cold load plus synthesis. Held warm, the same line is 1.1–1.5 s. A resident process is not an optimisation here; it is the difference between usable and not.

### The reference clip is a permanent latency setting

Chatterbox clones a reference, and the length of that clip becomes prompt tokens on **every subsequent generation**, forever:

| reference | prompt tokens | per line |
|---|---|---|
| A short clip of your own Piper voice | 172 | 0.9–1.5 s |
| A 20-second audiobook narrator | 250 | 1.4–2.2 s |

Which is one reason [the clone is built from your own Piper voice](voice.md#the-expressive-provider): it is shorter, and it happened to sound better too.

### Tags fire about five times in six

Measured over six seeds per tag, counting a hit as at least 0.25 s of extra voiced audio against the untagged line:

| tag | hits |
|---|---|
| `[laugh]` `[chuckle]` `[sigh]` `[groan]` `[clear throat]` | 5/6 |
| `[cough]` | 4/6 |
| `[gasp]` | 3/6 |

**This depends entirely on the decoding strategy.** With greedy `argmax` decoding the tags largely no-op — an emotion token is rarely the single most likely next token, so it is never chosen, and `[chuckle]` and `[clear throat]` produce silence. Temperature sampling takes the same weights from mostly-broken to the table above. We sample.

### Choosing

`piper` if you want the answer as fast as possible, or you are on a small machine. `chatterbox` if you would rather wait an extra second or two and have the voice able to sigh. It falls back to Piper whenever it cannot run, so choosing it is not a risk — the worst case is that it sounds flat.

## The window: `browser` against `webview`

| | `webview` (WebKitGTK) | `browser` (Chromium `--app`) — **what `auto` picks** |
|---|---|---|
| `hud.shell` | `"auto"` or `"webview"` | `"browser"` |
| CPU, idle **and** busy | **97% of a core** | **14–15%** total |
| Memory (PSS) | 0.38 GB | 0.44 GB |
| Processes | 1 | 12 |
| Frameless | yes | no — keeps a title bar |

**Seven times less CPU for 60 MB more memory.**

### Why the frameless window is so expensive

Two decisions compound, and each is reasonable alone.

**It renders in software.** The launcher sets `WEBKIT_DISABLE_DMABUF_RENDERER=1`, because without it WebKitGTK paints a blank white window on NVIDIA and on several compositors. The cost of that workaround is that every pixel is rasterized by the CPU instead of the GPU.

**The animation never idles.** The reactor is rebuilt as a 180-point SVG path, with the bar meter and one path per running subagent, at 60 fps — at `STANDING BY` exactly as while speaking. Most of those frames redraw a picture identical to the one before.

Chromium's own breakdown shows the mechanism rather than just the result: of its 14%, about 5.6% is a `gpu-process` doing on the GPU precisely the rasterization WebKit is doing on the CPU.

### It scales with window size

Rasterization cost follows pixel area, and the HUD remembers its geometry across restarts. A window enlarged once stays enlarged:

| window | megapixels | relative cost |
|---|---|---|
| 720×520 (the minimum) | 0.37 | 1× |
| 1230×853 | 1.05 | ~2.8× |

So "the HUD got slower" can have no cause other than a window that was resized weeks ago.

### Choosing

`browser`, which is what `auto` gives you, unless you specifically want the frameless window and have the headroom. On a laptop this is a battery question, not only a speed one — the HUD is on screen all day by design, so its idle cost is the one cost you pay continuously.

```toml
[hud]
shell = "browser"
```

`auto` picks `browser` first, so this is what you already have. `webview` is one word away if you want the frameless window and have the headroom.

The animation loop redrawing a stationary shape is a bug rather than a fact of life, and fixing it would make the frameless window cheap again. Until then, the title bar is the price of a seventh of the CPU.

## The ear

Conversation mode holds three models resident — faster-whisper, Silero VAD and smart-turn — which costs about **0.63 GB** for as long as it runs, and roughly **3% of a core** while listening.

That is the design rather than an accident: the models load once and stay, which is what lets the ear answer in milliseconds. Pressing ++c++ to close conversation mode returns the memory immediately.

Per frame, the two decision layers are cheap — Silero is about 0.07 ms per 32 ms frame, and smart-turn about 25 ms, asked only during silences. Neither is why the ear costs anything; holding the models is.

## Measuring it yourself

Numbers on this page are from one machine and will not be yours. The methods are short enough to repeat.

**Which processes are ours, and what they cost:**

```bash
claude-voice monitor --watch     # what holds the microphone and speakers
claude-voice sessions            # what each session is doing
```

**Instantaneous CPU** — note that `ps` reports a *lifetime average*, which for a long-lived HUD hides everything interesting:

```bash
top -b -n 2 -d 4 -p "$(pgrep -d, -f 'hudweb|hudshell|WebKitWeb|chrome.*hud-profile')" | tail -20
```

**Time a spoken line**, both providers:

```bash
time claude-voice say "Done, the tests pass."
```

**Whether `/tmp` is RAM on your machine**, which changes what scratch files cost:

```bash
df -h /tmp     # a tmpfs line means anything written there is memory, not disk
```
