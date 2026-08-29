---
title: The spoken log
---

# The spoken log

The bottom line of the HUD shows the last spoken line and nothing else — which tells you it just said *something*, and is useless the moment the next line replaces it.

The log is the fix. Press ++h++, or read it in a terminal:

```bash
claude-voice history 20              # the last 20 lines of this conversation
claude-voice history 20 --all        # ... of every session on this machine
claude-voice history --session <id>  # a named one
```

## The panel

```text
                          C L A U D E
                             VOICE ON
  m: turn OFF and silence · d: dictate · c: conversation · h: hide history · q: quit
                          │
       H I S T O R Y      │
  ↑↓ scroll  ·  g/G: ends │              ·  ·  ·
                          │         ○              ○
 14:02  you › run the     │      ○     T H I N K I N G    ○
             tests again  │         ○              ○
 14:02 said ‹ Checking    │              ·  ·  ·
             the suite.   │
 14:04 said ‹ Two         │        ▁▂▃▅▆▇█▇▆▅▃▂▁▂▃▅▆▇█
             failures.    │
 14:05  you › fix them    │
```

It is a panel, not a mode: the reactor keeps spinning beside it, so you never trade the state you are watching for the log you are reading.

Newest at the bottom. ++arrow-up++ / ++arrow-down++ or ++j++ / ++k++ scroll, ++g++ and ++shift+g++ jump to the ends, ++q++ still quits the HUD. ++h++ puts it away.

The panel reopens the way you left it — whether it was showing is remembered in `~/.config/claude-voice/hud-history`, so ++h++ is a preference you set once rather than a key you press every time you open a HUD.

## Where it sits

```toml
[history]
enabled = true
position = "left"     # left (default), right, or bottom
cap = 400             # lines kept per session; older ones trimmed
show = 200            # lines the panel reads back
keep_days = 7         # a session silent this long is swept away
```

At the bottom it is a full-width strip under the reactor, and two things change to buy back the rows: the microphone notice moves onto the divider, which reads better as a labelled rule anyway, and the single last-spoken line goes — the strip directly below it already ends with that line.

When the window cannot hold both — under about 74 columns beside the reactor, or too few rows under it — the panel takes the whole window instead. There, and only there, ++q++ closes the panel rather than quitting.

## What is in it, and what is not

**Spoken lines only** — not the conversation. And they are logged as they are *played*, not read back out of the transcript, because the transcript does not have them:

- only the final `<!-- TTS: -->` line reaches the transcript at all;
- narration is derived on the fly from the response;
- the acknowledgement is a separate model call;
- and a dictated sentence arrives indistinguishable from a typed one.

So the two places where sound is actually produced each append a line: the audio queue when something is enqueued for playing, and the dictation delivery when text is sent into a session. In the order things were actually heard.

The file is `~/.config/claude-voice/spoken-<session-id>.jsonl`, and `claude-voice history` prints the same log in the terminal.

## One log per conversation

The panel shows the session it is watching — the one ++t++ switches, the one dictation reaches.

Shared, it was not a dialogue at all: with two windows open you got a question from one and an answer from the other, interleaved by the clock, with nothing on screen saying they belonged to different conversations.

`claude-voice history --all` interleaves them on purpose, which is the right answer to "what did this machine say in the last hour" and the only way to read a log written before the split.

!!! note "Resuming starts a new log"

    Resuming a conversation with `--continue` or `--resume` comes back with a new session id, so the panel starts blank even though the conversation did not. The old log is still there — `claude-voice history --session <id>` — but it does not follow you across the resume.

## Why it matters in conversation mode

It matters most when you are not looking at the Claude window at all. Transcription is imperfect, and a misheard sentence is invisible until the answer comes back about the wrong thing. The log shows what actually went in, one line after you said it.

## What turning it off costs

```toml
[history]
enabled = false
```

The panel goes — and with it the context the acknowledgement reads, which falls back to seeing only the prompt. That is the trade: a shorter, vaguer line spoken the instant you hit enter. See [The acknowledgement](voice.md#the-acknowledgement).

The log is capped and trimmed per session, and a session that has been silent for `keep_days` is swept away, so leaving it on does not grow without bound.
