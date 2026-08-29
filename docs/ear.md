---
title: The ear
---

# The ear

Two ways to get spoken text into a running Claude Code session: **dictation**, which records while you hold a key, and **conversation mode**, which leaves the microphone open and works out when you have finished talking.

Both need a session started through the wrapper. That requirement is first because it is the one that catches everybody.

## The wrapper

```bash
claude-voice                        # then talk to it
claude-voice --model opus           # arguments go straight through
claude-voice run claude             # the same thing, spelled out
claude-voice --sessions             # the wrapped sessions that are live
```

A `claude` you started directly cannot be dictated into. There is no way in.

### Why a wrapper

There is no supported way to push text into a session that is already running. Its stdin belongs to the terminal emulator, which holds the pty master, and writing to `/dev/pts/N` paints the screen rather than feeding the program.

The alternatives, and why none of them is the answer:

| | |
|---|---|
| `TIOCSTI` | The old trick. The kernel disabled it in 6.2 — and even alive it only ever reached the caller's *own* controlling terminal, which a dictation process never is |
| `ydotool`, `wtype`, `xdotool` | They type into whichever window has focus, which is not a session and cannot be checked. They also want uinput permissions |
| Terminal remote control | Real but narrow: WezTerm out of the box, kitty and Konsole with configuration, nothing at all from GNOME Terminal, Alacritty or foot |

So the text has to come from something that was present at launch. `claude-voice run` forks the real command onto a pty it holds the master of and pumps bytes both ways; writing into that master is indistinguishable from typing, because it is the same file the keyboard's bytes travel down. It is what tmux does, minus living in tmux.

The cost is a longer word on the command line — `claude-voice` where you used to type `claude`. Alias it away if you like.

!!! info "The bare name is the session"

    `claude-voice` with nothing after it is `claude-voice run claude`. Anything beginning with a dash belongs to claude, since no subcommand here starts with one: `claude-voice --resume`, `claude-voice -c`, `claude-voice --model opus`.

    Everything else is a verb of ours — `on`, `off`, `status`, `hud`, `dictate`, `doctor` — typed in full. Arguments are handed to the child untouched, so anything Claude Code grows later works without this knowing it exists. That is why the wrapper has no flags of its own beyond `--sessions`: one more would collide the day the child grew the same name.

    `run` is the long form and takes any command at all: `claude-voice run <anything>` gives that thing the ear, and `claude-voice run -- claude --sessions` is how you pass down the one name that is taken.

`run` also opens a HUD if none is open, so the second and third terminals attach to the first one's window. There is only ever one.

## Dictation

Press ++d++ in the HUD, talk, press ++d++ again. The recording stops, faster-whisper transcribes it locally, and the text is typed into the session.

From a shell:

```bash
claude-voice dictate --toggle       # start recording / stop and send
claude-voice dictate --panes        # list what text can be sent to
claude-voice dictate --pane wrap:12 # pick one (or press t in the HUD)
claude-voice dictate --can-send     # is there a session to send to? exits non-zero if not
claude-voice dictate --status
```

The recording device is ALSA:

```toml
[stt]
device = "plughw:CARD=Headset,DEV=0"   # arecord -L
model = "small"                        # base mishears technical vocabulary
language = "en"
max_secs = 120
```

!!! danger "Use a device *name*, never an index"

    ALSA card numbers reorder on reconnect. A setup pinned to `plughw:4,0` silently started recording from a webcam microphone — digital silence — the day a card moved, and digital silence looks exactly like a broken microphone.

### Delivery is refused unless the target is Claude

Sending is refused unless the target session is running `claude`. In a shell, a bad transcription would execute as a command.

With no such session, both modes are **disabled rather than silently useless**. The microphone is not opened at all, the HUD replaces its footer with `⚠ no Claude Code session — dictation disabled`, and pressing ++d++ or ++c++ flashes the same reason instead of recording into a void.

Otherwise a dead setup and an unheard sentence look identical: silence.

## Conversation mode

Press ++c++, and stop pressing keys. The microphone stays open, and each finished sentence is transcribed and sent on its own.

```bash
claude-voice listen           # the daemon, run directly
claude-voice listen --check   # verify the models and measure latencies
```

While the voice is speaking, the ear gates itself, so it does not transcribe its own output.

### End-of-turn detection

A fixed silence threshold forces a choice between cutting people off and being slow. LiveKit's open benchmark measures 21.7% mid-sentence cuts at 600 ms, and you need 1600 ms to reach 5% — which is a second and a half of dead air after every sentence.

So there are three layers:

1. **Silero VAD**, on every 32 ms frame, with hysteresis at 0.60 on and 0.35 off and a 500 ms preroll so the first syllable is not clipped. About 0.07 ms per frame.
2. **smart-turn v3**, asked during a silence — not "has it been quiet long enough" but "does that phrase *sound* finished". About 25 ms.
3. **The clock**, as a backstop.

```toml
[listen]
floor_ms = 700          # minimum silence before smart-turn is asked at all
ceil_ms = 2500          # send anyway past this
complete = 0.55         # the probability that counts as finished
min_speech_ms = 300     # below this, discard — coughs, key clicks
max_utterance_s = 30    # forced send
```

Both models run locally on the CPU: Silero is read out of faster-whisper's own assets, and smart-turn is a single ONNX file from Hugging Face. Neither needs torch.

### The three states, and the fourth

The reactor tells you which you are in, and this is the part worth learning:

| | |
|---|---|
| Dashed ring, `ready to listen` | Armed, nothing arriving |
| Dashed ring, reactor moving | Hearing you, and how loudly |
| No ring | Conversation mode is off |
| `NOT LISTENING`, flat meter, `⚠ no Claude Code session — conversation on hold` | Held — see below |

The `ready to listen` state is the one that used to be invisible: microphone open, nothing arriving, indistinguishable from the mode being off. The microphone badge has its own colour, because the ear being open is not a state of Claude's, and confusing the two is how you end up talking to a window that stopped listening ten minutes ago.

### Holding, rather than stopping

Close the session under a running conversation mode and within about three seconds it **holds**. It stops transcribing, the reactor says `NOT LISTENING`, the inward wave stops, the meter goes flat, and the banner reads `⚠ no Claude Code session — conversation on hold` — which becomes `you are talking to nothing` while you are actually mid-sentence.

Voice activity is still detected. That is the whole point: speaking into a dead setup has to look different from speaking into a live one. But nothing is transcribed, because the result has nowhere to go.

Open a session again and it resumes on its own, from the next sentence. You never have to remember to switch listening back on, and nothing needs restarting — the HUD re-checks every couple of seconds, and every key press checks afresh.

### Transcription quality

```toml
[stt]
glossary = "Terms: Docker, kubectl, npm install, pytest, Claude Code, commit, branch, git rebase, systemd, journalctl."
hallucinations = ["thanks for watching", "subscribe", "www.", "subtitles by"]
```

The glossary is handed to Whisper as an initial prompt, which is what stops `kubectl` becoming three words. The hallucination list is dropped outright: Whisper invents those phrases over near-silence, and a stray "thanks for watching" delivered into a session is worse than nothing.

Each [language pack](languages.md) ships its own glossary and hallucination list.

!!! tip "Read back what it heard"

    Transcription is imperfect, and a misheard sentence is invisible until the answer comes back about the wrong thing. Press ++h++ for the [spoken log](history.md) — it records the dictated line as it is delivered, so you can see what actually went in, one line after you said it.

## Choosing which session hears you

With one session there is nothing to pick. With several:

```bash
claude-voice dictate --panes          # list them
claude-voice dictate --pane wrap:12   # aim at one
claude-voice dictate --next           # cycle
```

++t++ in the HUD cycles, and carries the [focus](sessions.md) along with it, so the window you talk to and the window that answers out loud are one window rather than two settings that happen to agree.

## Checking the microphone is actually released

A claim about a microphone is worth checking rather than believing:

```bash
claude-voice monitor --watch
```

It answers from the machine's side — what has a claim on the capture device and the speakers at this instant, ours or anybody's. Quit the HUD and watch the list empty; `pw-record` is the microphone itself, and it should be gone within about three seconds.

For a capture that outlived its parent, and for the watchdog that catches one when no window is open to warn you, see [The microphone watchdog](microphone.md).
