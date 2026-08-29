---
title: Talking to Claude
---

# Tutorial 2 — Talking to Claude

You have a voice. This one gives it an ear: first push-to-talk dictation, then continuous conversation mode, where you stop touching the keyboard entirely.

You need a microphone, and you need [tutorial 1](first-voice.md) finished — or at least a working `claude-voice say`.

## Step 1 — Start a session the ear can reach

This is the one requirement that catches everybody, so it is first:

```bash
claude-voice
```

!!! warning "A `claude` you started directly cannot be dictated into"

    There is no supported way to push text into a program that is already running: its stdin belongs to the terminal emulator. `claude-voice` forks the real command onto a pty it holds the other end of, and writing into that end is indistinguishable from typing — because it is the same file your keyboard's bytes travel down.

    So if dictation records and nothing arrives, this is almost always why. Restart the session through `claude-voice`. [The ear](../ear.md#why-a-wrapper) has the long version, including why `xdotool` and friends are not the answer.

Confirm there is somewhere to send text:

```bash
claude-voice dictate --can-send
```

It answers in one line and exits non-zero when nothing can receive text, which makes it the right thing to put in a script.

## Step 2 — Dictate one prompt

Put focus on the HUD window and press ++d++.

1. Press ++d++ — recording starts, and the reactor turns to the microphone.
2. Say something with work in it: *"run the tests and tell me what fails"*.
3. Press ++d++ again — recording stops, the audio is transcribed locally with faster-whisper, and the text is typed into your Claude session.

The text lands in the session's prompt box exactly as though you had typed it, and Claude answers out loud.

Without the window, the same thing from a shell:

```bash
claude-voice dictate --toggle     # start recording; run again to stop and send
```

??? question "It typed the wrong words"

    Transcription is imperfect, and the fix is usually the device rather than the model. `arecord -L` lists your capture devices; put the one you want in the config **by name**:

    ```toml
    [stt]
    device = "plughw:CARD=Headset,DEV=0"
    ```

    ALSA card *numbers* reorder on reconnect — a setup pinned to `plughw:4,0` starts recording from a webcam microphone the day a card moves, and digital silence looks exactly like a broken microphone.

## Step 3 — Pick which session hears you

With one session there is nothing to choose. With three:

```bash
claude-voice dictate --panes          # list what text can be sent to
claude-voice dictate --pane wrap:12   # pick one
```

In the window, ++t++ cycles through them, and the HUD's session panel names the one currently aimed at.

The voice follows the ear on purpose. ++f++ hands the voice to whichever session dictation is already pointing at, so the window you talk to and the window that answers out loud are one window rather than two settings that happen to agree. [Sessions and focus](../sessions.md) is the whole story.

## Step 4 — Conversation mode

Press ++c++, and stop pressing keys.

The microphone stays open. Silero VAD runs on every 32 ms frame, and when it hears you stop, a small turn-detection model is asked whether the phrase *sounds finished* — because a fixed silence threshold forces a choice between cutting people off and being slow. Then it transcribes, sends, and goes back to listening.

While the voice is speaking, the ear gates itself, so it does not transcribe its own output.

The reactor tells you which of three states you are in, and this is the part worth learning:

| What you see | What it means |
|---|---|
| Dashed ring, badge `ready to listen` | Armed, nothing arriving. The microphone is open and waiting |
| Dashed ring, reactor moving with your voice | It is hearing you right now, and how loudly |
| `NOT LISTENING`, flat meter, `⚠ no Claude Code session` | Held — see below |

Press ++c++ again to stop.

## Step 5 — Watch it hold, and recover

Close the Claude session while conversation mode is running. Within about three seconds the HUD says:

```text
⚠ no Claude Code session — conversation on hold
```

It **holds** rather than stopping. Voice activity is still detected — so speaking into a dead setup looks visibly different from speaking into a live one — but nothing is transcribed, because the result has nowhere to go. Start a session again and it resumes on its own, from the next sentence. You never have to remember to switch listening back on.

This matters more than it sounds. In conversation mode you are not looking at the Claude window at all, and without this, a dead setup and an unheard sentence are the same thing: silence.

## Step 6 — Read back what it heard

Press ++h++ to open the spoken log beside the reactor:

```text
 14:02  you › run the tests again
 14:02 said ‹ Checking the suite.
 14:04 said ‹ Two failures.
 14:05  you › fix them
```

Newest at the bottom; ++arrow-up++ / ++arrow-down++ or ++j++ / ++k++ to scroll; ++g++ / ++shift+g++ for the ends. ++h++ again puts it away, and it reopens the way you left it.

This is the panel conversation mode exists for. A misheard sentence is invisible until the answer comes back about the wrong thing — here you can see what it actually heard, one line after you said it.

## Step 7 — Check nothing was left holding the microphone

A claim about a microphone is worth checking rather than believing:

```bash
claude-voice monitor --watch
```

It answers from the machine's side — what has a claim on the capture device and the speakers at this instant, ours or anybody's. Quit the HUD and watch the list empty; `pw-record` is the microphone itself, and it should be gone within about three seconds.

Nothing there kills anything. It is a window, not a broom — `claude-voice mic --sweep` clears a capture of ours left behind, and other people's processes stay theirs to close.

## What you have now

- Dictation into a running session, with ++d++ or `claude-voice dictate --toggle`.
- Conversation mode with real end-of-turn detection, on ++c++.
- A way to see what it heard, and a way to check what holds the microphone.

## Next

[Tutorial 3 — Living in the HUD](the-hud.md), which is the window you have been pressing keys in without being told what the rest of it says.
