---
title: Troubleshooting
---

# Troubleshooting

Start with:

```bash
claude-voice doctor
```

It checks the interpreter, the voice model, the audio session, and whether the hooks are installed and still point at files that exist. Every line carries the command that fixes it, and it exits non-zero if anything failed.

```text
[  ok  ] piper-tts — importable
[  ok  ] voice model — en_US-amy-medium.onnx (63 MB)
[ FAIL ] hook Stop — points at a missing file: /old/path/speak.py
         fix: claude-voice hooks   (the checkout moved — replace the old line)
[ note ] switch — off
         fix: claude-voice on
```

The rest of this page is for when it says everything is fine and you still hear nothing.

## Nothing is spoken

Every turn appends one line to `~/.config/claude-voice/speak.log` saying exactly why:

```text
2026-08-25 11:19:16 fields=[...] len=73 marker=yes on=True audio=True
```

| What it says | What it means |
|---|---|
| `marker=NO` | The model did not write the `<!-- TTS: -->` line. Either the voice was off when you sent the prompt — the instruction is only injected while it is on — or the model judged the turn not worth speaking |
| `on=False` | Run `claude-voice on` |
| `audio=False` | No PipeWire/PulseAudio session. Expected over plain SSH and in systemd services; there is nothing to play through |
| No line at all | The `Stop` hook is not installed, or points at a bad path. `claude-voice hooks --install` adds what is missing — but a hook frozen on an old file path counts as installed, so that one has to be taken out by hand |

If the log looks right and there is still no sound, test the audio path on its own:

```bash
claude-voice say "test one two"
```

That bypasses the switch and the window gate deliberately. If *this* is silent the problem is `aplay`, the device or the voice model, and not the hooks.

## Nothing runs at all, and the HUD is closed

That is the design, not a fault. While no window is open, nothing of ours runs: no voice, no acknowledgement, no heartbeat, no microphone. Open one:

```bash
claude-voice hud
```

`hud.required = false` gives the older behaviour if you want it — see [The HUD is the application](hud.md#the-hud-is-the-application).

## Nothing speaks anywhere, and the HUD is open

Check for a focus left on a window you have since closed:

```bash
claude-voice status
```

A focus pinned to a dead terminal silences everything. The HUD says so on its bottom line, and ++f++ or `claude-voice focus --clear` clears it.

## Edits to the checkout change nothing

The install is a copy, not a link, and it keeps running the code it was built from:

```bash
uv tool install --force --refresh "$HOME/repos/claude-voice"
```

**Both flags.** `--force` alone reinstalls uv's cached wheel and reports success while changing nothing. [Installing again from your own checkout](installation.md#installing-again-from-your-own-checkout) has the one-line check for whether the two differ at all.

## The HUD dies with `NameError` or `AttributeError`

You are running a stale copy from an old path — usually an alias frozen on `python .../hud.py` after the checkout moved. Run `claude-voice hud` instead, or repoint the alias.

## The HUD says `MICROPHONE OPEN, NO OWNER`

A capture process outlived its parent, usually an unclean exit from conversation mode. Press ++x++.

That warning reads the kernel's capture state, not our own bookkeeping, precisely so it still fires when our bookkeeping is what broke. Two things count as open: a capture stream that is *running*, whoever owns it, and a `pw-record` of ours being alive whatever state its stream is in. The second is the orphan the warning exists for, and the one ++x++ can clear.

From a shell: `claude-voice mic --sweep`.

## `mic held open by claude (852955) — not recording`

Another application's *parked* stream. It is not an alarm, but it is not nothing either: this is what lights your desktop's microphone indicator, which counts streams rather than recordings.

Nothing here can close someone else's stream, and ++x++ deliberately will not try. Quitting that application releases it. The line exists so a lit tray icon has an explanation instead of being a thing you learn to ignore.

## Dictation records but nothing arrives

There has to be a session started **through the wrapper**. A `claude` launched directly cannot be reached — that is the whole reason the wrapper exists.

```bash
claude-voice                     # start one this way
claude-voice dictate --can-send  # one line, non-zero exit when nothing can receive
claude-voice dictate --panes     # what is available
```

Then `~/.config/claude-voice/dictate.log`.

If it records silence, the device is wrong. `arecord -L`, and set `stt.device` **by name** — ALSA card numbers reorder on reconnect, and a webcam microphone recording digital silence looks exactly like a broken setup.

## The HUD goes calm while the session is still working

The HUD watches one session — the one ++t++ points at, the same one dictation goes to — and every session keeps its own state, so another window finishing its turn no longer speaks for yours.

If it still happens, `claude-voice sessions` prints what each one is doing, and the HUD's target has to be resolvable. A session started through the wrapper always is; anything else falls back to showing the liveliest one of that project.

## It still speaks the old language after switching

Something above the preset is pinning it.

```bash
claude-voice config     # the preset in effect, and where it came from
claude-voice doctor     # the voice model actually loaded
```

The usual culprit is a `[tts] voice_model` — or an `[instruction] text` — in your own `config.toml`, written for the language you switched away from. Move it under `[preset.<name>]` so it applies to that language only. [The inversion](configuration.md#the-inversion) explains why.

## ++l++ refuses in the HUD

The other language's voice was never downloaded. Switching into a voice that cannot speak is a silent failure, so it declines instead.

```bash
claude-voice lang               # what is on disk and what is missing
claude-voice lang --fetch es    # get it, and cache its acknowledgements
```

## The tick keeps going after the answer

The `Stop` hook is what kills it, so a session that died mid-turn — out of tokens, a hang, ++ctrl+c++ — leaves it running. It caps itself, or `claude-voice silence` ends it now.

## Conversation mode will not start

```bash
claude-voice listen --check
```

That verifies the models and measures their latencies. Then `~/.config/claude-voice/listen.log`, which is where the daemon's stderr goes.

Two things refuse to start it at all: no window open, and no session that can receive text. Both are reported on the HUD's footer rather than failing quietly.

## The window opens white, or behind everything

Both are known and both are handled by the launcher, which sets `WEBKIT_DISABLE_DMABUF_RENDERER=1` (WebKitGTK's newer renderer paints white on NVIDIA and several compositors) and `GDK_BACKEND=x11` (GNOME's Wayland compositor refuses "keep above" to every toolkit there is).

If you are launching a module directly rather than through the CLI, you are missing those. Use `claude-voice hud`.

To rule the webview out entirely:

```bash
claude-voice hud --shell browser
```

## The repo panel is empty or late

The branch is read off disk and is always current. The rest needs `gh`, a repository, and a pull request for that branch — rows with no answer are not drawn.

It is asked on a slow clock in a background thread, so a slow network makes that row late and never makes the window stutter. `hud.github = false` stops it asking.

## Still stuck

`claude-voice monitor --watch` shows what actually holds the microphone and the speakers at this instant, ours or anybody's, which settles most "is it even running" questions.

Then [open an issue](https://github.com/jcarranz97/claude-voice/issues) with the output of `claude-voice doctor` and `claude-voice config`.
