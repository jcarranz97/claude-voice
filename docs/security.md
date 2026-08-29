---
title: Security
---

# Security

The short version: audio never leaves the machine, two features make network calls and both can be switched off, and the HUD's local page is locked down because its buttons open a microphone.

## What leaves the machine

| | What is sent | Where | Off switch |
|---|---|---|---|
| The acknowledgement | the prompt, plus `ack.context` turns of what was said out loud | the Anthropic API | `ack.contextual = false`, or `ack.enabled = false` |
| The repo panel | a `gh pr view` for the current branch | GitHub, through your own `gh` | `hud.github = false` |

`ack.context = 0` keeps the acknowledgement and sends the prompt alone, without any of the conversation.

Nothing else. Synthesis, transcription, voice activity detection and end-of-turn detection all run locally on the CPU. **No audio is transmitted anywhere, ever** — there is no code path that sends a recording off the machine.

## Credentials

The acknowledgement uses `ANTHROPIC_API_KEY` if it is set, and otherwise falls back to the Claude Code OAuth credential already in `~/.claude/.credentials.json`. Nothing new is stored, and nothing is written back to that file.

`ack.contextual = false` makes no call and needs no credential.

## The HUD's local server

The window is a page served from `127.0.0.1` on a random port. Its buttons open a microphone, redirect the voice and stop sessions, and **any page in any browser can aim a navigation at a loopback port** — so the endpoints that do something are behind four checks at once:

| | |
|---|---|
| **POST only** | a top-level navigation cannot reach them |
| **`Host` allowlist** | the header must be exactly the address it bound to |
| **A per-run token** in a custom header | a form submission and a top-level navigation cannot set one, and it is regenerated every run |
| **`Sec-Fetch-Site: same-origin`** | page script cannot forge this; the browser sets it |

No CORS header is ever sent, so a cross-origin `fetch` cannot read a response even if it reached one. The token is compared in constant time.

Read endpoints — the page itself, its three static files, and the event stream — are behind the `Host` check and the token, and serve only files inside the package's `web/` directory.

## The delivery socket

Dictation reaches a session through a unix socket in `$XDG_RUNTIME_DIR/claude-voice/`, created mode `0600` — one per wrapped session, owned by you, unreachable from the network by construction.

Two refusals sit on top of it:

- **Newlines are rejected** in delivered text. What is written into the pty is a line, followed by a carriage return the wrapper sends itself.
- **Delivery is refused unless the target is running `claude`.** In a shell, a bad transcription would execute as a command.

## Nothing runs `sudo`

Not the installer, not the program. System packages are yours to install, which is why they are step one of the install and not a thing a script does on your behalf.

The one thing that writes outside `~/.config/claude-voice/` and `~/.local/share/piper-voices/` is `claude-voice hooks --install`, which merges into `~/.claude/settings.json` and keeps a timestamped copy of what it replaced. A settings file it cannot parse is refused rather than repaired.

`claude-voice mic --install` writes a systemd **user** unit and timer. Both are removed by `--uninstall`.

## Nothing is killed automatically

The microphone watchdog and the monitor name a holder and stop there. ++x++ in the HUD sweeps captures **of ours** and deliberately will not touch anyone else's stream — quitting that application is what releases it.

The one thing that does kill processes is `claude-voice silence`, and it identifies its targets by walking `/proc` and matching the actual script path rather than by pattern, so it cannot take out an editor that happens to have the file open.

## Downloads

Voice models come from the [rhasspy/piper-voices](https://huggingface.co/rhasspy/piper-voices) collection and the turn-detection model from [pipecat-ai/smart-turn-v3](https://huggingface.co/pipecat-ai/smart-turn-v3), both over HTTPS into your own cache directories. Downloads are written to a temporary name and renamed on success, so an interrupted fetch leaves no half-file to be loaded later.

## Reporting something

Open an issue at [github.com/jcarranz97/claude-voice/issues](https://github.com/jcarranz97/claude-voice/issues). If it is a vulnerability rather than a bug, say so in the title and describe the class of problem rather than posting a working exploit.
