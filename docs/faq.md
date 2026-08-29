---
title: FAQ
---

# FAQ

## Do I have to run Claude Code inside tmux?

**No.** Start the session with `claude-voice` instead of `claude` and everything works in whatever terminal you already use — GNOME Terminal, Konsole, kitty, Alacritty, the one in VS Code.

If you *like* tmux, keep it: run the same command inside a pane. A wrapper in a pane is still a wrapper, its pty is inside that pane, and delivery does not go through tmux at all. tmux stops being a requirement without becoming a problem.

| | Works without tmux? | |
|---|:---:|---|
| speaking, narration, the acknowledgement, the heartbeat | :material-check-bold: | works in any terminal, always did |
| the HUD — reactor, meters, history, agents | :material-check-bold: | |
| ++d++ dictate, ++c++ conversation mode | :material-check-bold: | inside a session started with `run` |
| ++f++ focus — mute every session but one | :material-check-bold: | filed under the pty instead of the pane |
| ++t++ switch which session receives dictation | :material-check-bold: | it lists the sessions `run` started |

## Why do I have to type `claude-voice` instead of `claude`?

Because there is no way into a session that is already running — its stdin belongs to the terminal emulator. The wrapper is present at launch and holds the other end of the pty, which is what makes dictation possible at all. [The ear](ear.md#why-a-wrapper) has the full reasoning, including why `xdotool` and `TIOCSTI` are not answers.

Alias it if the extra word bothers you. Speaking works either way — only the ear needs the wrapper.

## Does anything I say leave my machine?

No. Synthesis, transcription, voice activity detection and turn detection all run locally on the CPU, and there is no code path that sends a recording anywhere.

Two features do make network calls, and both switch off: the acknowledgement (the prompt plus some of the spoken log, to a small model) and the HUD's pull-request row (a `gh` call). [Security](security.md) lists exactly what each one sends.

## Does it cost tokens while the voice is off?

No. While the voice is off the hook injects nothing, so the model is never asked to write a spoken line. Nothing is added to your prompts and nothing extra is generated.

While it is on, the cost is the instruction on each prompt, the one line the model writes, and — unless you turn it off — one small model call per prompt for the acknowledgement.

## Why does everything stop when I close the window?

The HUD is the application, not a viewer. While one is open the hooks speak; while none is, nothing of ours runs at all.

The alternative is a machine that installed this once and is now spending tokens and holding a microphone for nobody. Closing does not turn the voice **off** — it suspends it, and opening a window again picks up where the switch left it.

`hud.required = false` gives the older behaviour, which is right for a machine you never sit in front of.

## Can I use it without the HUD?

Yes, with `hud.required = false`. You lose the state display, the keys, dictation's window and the history panel; the voice runs on the hooks alone.

There is also `claude-voice hud --terminal` for a machine with no desktop, which is the better answer most of the time.

## Why does it speak a summary instead of the answer?

Because markdown, diffs and file paths are unlistenable, and reading the response aloud is the thing that gets these setups abandoned in a week.

But a summary is wrong when the question had an exact answer in it, so the shipped instruction overrides its own word limit for a number, a name, a short list or a yes/no. [Design decisions](design.md#a-summary-of-an-answer-is-not-an-answer) covers it.

## Can I change what it says, or how it says it?

Yes — the instruction injected into every prompt is a config value, not a constant. Terse, formal, or a pirate; it is your ear. See [The voice](voice.md#changing-the-register).

## Why did it say nothing at all for that prompt?

Two possibilities, both deliberate. The model wrote `<!-- TTS: SILENT -->` because the turn had nothing worth saying, or the acknowledgement declined because the answer was going to arrive about as fast as an acknowledgement of it.

`ack.skip_quick = false` acknowledges every prompt.

## Which languages are supported?

English and Spanish ship inside the package. Anything Piper has a voice for and Whisper can transcribe can be added as a [language pack](languages.md) in `~/.config/claude-voice/presets/` — a pack is a TOML file, not code.

## Why does a word come out mispronounced?

espeak takes one language per utterance, so a technical term in a non-English voice gets that language's phonemes. [Pronunciation](pronunciation.md) is the workbench for it, and the fix is usually one line.

## Does it work over SSH?

The voice does not — `audio=False` in the log, because there is no audio session to play through. Everything that does not make sound does: `claude-voice hud --terminal`, `history`, `sessions`, `monitor`, `doctor`.

## macOS? Windows?

Not yet. The parts tied to Linux are the ones that touch the machine directly — PipeWire and ALSA, `/proc` and `/sys`, systemd, WebKitGTK. None of it is unportable in principle; none of it is written. See [Platform support](platforms.md).

## Does it work with anything other than Claude Code?

Not yet, and less of it is Claude Code's than you would think. The voice attaches through four hooks; the delivery half is already runtime-agnostic, since the wrapper never inspects what it started — `claude-voice run <anything>` gives that thing the ear.

Everything below the hook layer — synthesis, the ear, the HUD, the state files — is agnostic already. A second runtime is a matter of another way in, not another implementation.

## Is the HUD a browser tab?

No. It is a frameless WebKitGTK window, or a Chromium `--app` window with a profile of its own if the webview packages are not installed. Either way it is a window, not a tab in the browser you are using, and the page is three files off disk with no CDN, no bundler and no node.

## Can I run two HUDs?

Yes — a second `claude-voice hud` opens a second window and both work. What you cannot do is have sessions disagree about which one they belong to: sessions attach to whichever window is open, and `run` only opens one if none is up.

## How do I see what it actually said an hour ago?

```bash
claude-voice history 50          # this conversation
claude-voice history 50 --all    # every session on this machine
```

Or press ++h++ in the HUD. [The spoken log](history.md).

## Why is `uv tool install --force` not enough?

uv caches the wheel it built for a directory, so `--force` reinstalls that cache — your edits are not in it, and it reports success anyway. Add `--refresh`. [Installation](installation.md#installing-again-from-your-own-checkout).

## How do I get rid of it?

```bash
claude-voice mic --uninstall
uv tool uninstall claude-voice
```

That leaves `~/.config/claude-voice/`, `~/.local/share/piper-voices/` and four lines in `~/.claude/settings.json`, all of them yours to delete or keep.
