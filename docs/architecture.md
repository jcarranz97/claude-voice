---
title: Architecture
---

# Architecture

How the pieces fit, for anyone reading the source or debugging something the [troubleshooting](troubleshooting.md) page does not cover.

## The three processes

Nothing here is a service. There are three kinds of process and they find each other through files in `~/.config/claude-voice/`.

<div class="grid cards" markdown>

-   **The hooks** — short-lived

    Claude Code runs one per event and it exits in milliseconds. They read the config fresh each time, which is why a config change needs no restart.

-   **The HUD** — long-lived, one

    A window and a small local server. Its existence is what licenses everything else to run.

-   **The daemons** — long-lived, spawned

    The audio player, the heartbeat, conversation mode, the wrapper's pty pump. Each is started detached by something short-lived and stops on its own when the window goes.

</div>

They coordinate through the filesystem rather than through a bus, because the coordinating processes come and go faster than a connection could be established, and because the state has to survive all of them dying.

## A turn, end to end

```text
you hit enter
  │
  ├─ UserPromptSubmit hook
  │    ├─ gate: window open? switch on? session not muted? focus allows?
  │    ├─ spawn ack        ─────────────┐
  │    ├─ spawn heartbeat  ───────────┐ │
  │    ├─ mark session "thinking"     │ │
  │    └─ return the instruction      │ │
  │                                   │ │
  ├─ MessageDisplay hook (×n)         │ │
  │    └─ clean the prose, enqueue ───┼─┤
  │                                   │ │
  └─ Stop hook                        │ │
       ├─ kill the heartbeat  ────────┘ │
       ├─ extract the last TTS marker   │
       ├─ synthesize                    │
       ├─ enqueue, flushing this        │
       │  session's pending items ──────┤
       └─ mark session "ready"          │
                                        ▼
                              one queue, one locked player
                                        │
                                        ├─ measure the envelope
                                        ├─ spawn aplay
                                        ├─ publish state + envelope + t0
                                        └─ append to the spoken log
```

The gate in the first hook is the whole of the on/off design: fail it and nothing is spawned, nothing is injected, and the turn costs exactly what it would have cost without this program installed.

## Text to speech

1. **Extract.** The *last* `<!-- TTS: -->` comment in the message. Last, not first, so a response that quotes the marker while explaining it does not get its example spoken.
2. **Phonemize.** The whole line in `tts.primary_voice`, for prosody and word boundaries. Then, per word: an explicit `pronunciation.overrides` entry wins outright; otherwise a word in `pronunciation.foreign_terms` is re-phonemized in `tts.foreign_voice` and spliced in.
3. **Synthesize.** Piper, on the CPU, at `tts.length_scale`.
4. **Enqueue.** A sequence number under a lock, then the file.
5. **Play.** One player process holding a lock. It measures the envelope, spawns `aplay`, and *then* publishes the state — so the timestamp it writes is the moment sound actually starts.

## How the reactor knows how loud

Two directions, two mechanisms, because only one of them is hard.

| | Mouth | Ear |
|---|---|---|
| Known in advance | yes — it is a finished file | no |
| Published as | one envelope + the start time | a bare float, ~25×/s |
| Read as | interpolated off the wall clock | the last value, stale after half a second |
| Can drift | no | not applicable |

A window opened mid-sentence catches up on the right syllable, because it computes the same function of the same clock that every other window does. The ear cannot work that way, so it is smoothed instead: fast attack, slow decay, the way an ear behaves rather than the way a graph does.

Both are advisory. A window that cannot read either still animates, blind.

## The wrapper

```text
claude-voice
  └─ openpty ─ fork ─ setsid ─ TIOCSCTTY ─ execvp("claude")
       │
       ├─ registry file: pid, child, pty path, cwd, socket
       ├─ AF_UNIX socket, mode 0600, in $XDG_RUNTIME_DIR
       └─ select() on [pty master, socket, stdin]
             ├─ stdin  → master   (you typing)
             ├─ master → stdout   (what Claude draws)
             └─ socket → master, then \r after 150 ms
```

The delay before the carriage return is so the TUI has taken the text before the newline arrives.

The pty path is the join key: `claude-voice run` knows the pty it started the session on, and Claude Code lists every live session under `~/.claude/sessions/`, so a wrapped session is matched to its conversation exactly, from the first moment — including for the dictated line that opens the conversation.

That is also why the fork is hand-rolled rather than `pty.fork()`: it needs the slave's name.

## Conversation mode

```text
pw-record ─ 32 ms frames
  ├─ publish the level          (for the reactor)
  ├─ gated? (are we speaking?)  → skip
  ├─ Silero VAD                 0.60 on / 0.35 off, 500 ms preroll
  └─ on silence ≥ floor_ms
       ├─ ask smart-turn every 200 ms
       ├─ p ≥ complete  → send
       ├─ past ceil_ms  → send anyway
       └─ else keep listening
             │
             └─ faster-whisper (CPU, int8, glossary as initial prompt)
                  ├─ drop known hallucinations
                  └─ deliver over the wrapper's socket
```

Every three seconds it re-checks that a deliverable session still exists, which is what makes the hold-and-resume behaviour work without anyone pressing a key.

## The HUD, in two surfaces

```text
                     hudcore
        (state, labels, actions, refusals)
                   │           │
        hudweb.py  │           │  hud.py
   ThreadingHTTPServer         curses, 20 fps
   127.0.0.1:<random>
        │
        ├─ one producer thread, 4 Hz
        ├─ SSE at /events, plus named level events at 20 Hz
        └─ POST /act → hudcore.act(name)
              │
        hudshell.open_window()
              ├─ WebKitGTK, frameless      (preferred)
              ├─ Chromium --app, own profile
              └─ print the URL
```

Both surfaces call `hudcore.act(name)` for every key. There is one implementation of "turn the voice off", and one implementation of the reason it might refuse.

**The connection is the window.** The page holds the event stream open; when the last stream drops for more than a few seconds, the server exits, and on the way out — if it was the last window — it stops conversation mode, silences the queue and sweeps orphaned captures.

## Presence, focus and sessions

Three separate questions, three separate files, deliberately not one:

| Question | Answered by | Grain |
|---|---|---|
| Is anyone watching? | live pidfiles, one per window | the machine |
| Is the voice on? | a marker file | the machine |
| May *this* session speak? | the focus file plus a per-session mute | the terminal |
| What is this session doing? | one state file per session | the session |

The focus is filed under the terminal — a tmux pane id, or the controlling pty — rather than the session id, so it survives a session restart. See [Sessions and focus](sessions.md).

## What leaves the machine

Two things, both switchable:

| | What is sent | Off switch |
|---|---|---|
| The acknowledgement | the prompt, and `ack.context` turns of the spoken log | `ack.contextual = false`, or `ack.context = 0` for the prompt alone |
| The repo panel | a `gh` call about the current branch | `hud.github = false` |

Synthesis, transcription, voice activity detection and turn detection all run locally on the CPU. No audio leaves the machine, ever.

## Models

| | | Where from |
|---|---|---|
| TTS | Piper | `~/.local/share/piper-voices/`, fetched from Hugging Face |
| STT | faster-whisper, `small` by default | its own cache |
| VAD | Silero v6 | read out of faster-whisper's assets — no torch |
| Turn-taking | smart-turn v3 | one ONNX file from Hugging Face, features computed with faster-whisper's own extractor to avoid a `transformers` dependency |
