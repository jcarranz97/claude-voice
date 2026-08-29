---
title: Platform support
---

# Platform support

## Operating systems

| OS | Supported | Notes |
|---|:---:|---|
| :material-linux: **Linux** | :material-check-bold: | PipeWire for capture, PulseAudio or ALSA for playback; X11 and Wayland both |
| :material-apple: **macOS** | :material-close-thick: | not yet — no CoreAudio capture path, and no window |
| :material-microsoft-windows: **Windows** | :material-close-thick: | not yet — same, plus no systemd for the microphone watchdog |

Linux only for now, and not by preference. The parts that are tied to it are the ones that touch the machine directly: PipeWire and ALSA for capture, `/proc` and `/sys` for the system and GPU meters, systemd for the microphone watchdog, and WebKitGTK for the window.

None of that is unportable in principle; none of it is written yet.

## Agent runtimes

| Runtime | Supported | Notes |
|---|:---:|---|
| :material-robot: **Claude Code** | :material-check-bold: | hooks for the voice, a wrapped pty for dictation |
| **OpenCode** | :material-progress-wrench: | planned |
| other agent runtimes | :material-progress-wrench: | planned |

The voice attaches through Claude Code's four hooks. Dictation delivers into the pty that `claude-voice run` holds open, and that half is **already runtime-agnostic** — the wrapper never inspects what it started, so `claude-voice run <anything>` gives that thing the ear.

Nothing below the hook layer is Claude Code's either: the synthesis, the ear, the HUD and the state files are all agnostic already. A second runtime is a matter of another way in, not another implementation.

## Requirements

| | |
|---|---|
| :material-linux: OS | Linux with PipeWire (PulseAudio works for playback) |
| :material-robot: Runtime | Claude Code |
| :material-language-python: Python | none of your own — `uv` provisions 3.11+ (`tomllib`) |
| :material-console: System | `aplay`, and for input `arecord` + `pw-record` |
| :material-account-voice: TTS | [Piper](https://github.com/OHF-Voice/piper1-gpl) — local, neural, CPU |
| :material-ear-hearing: STT | [faster-whisper](https://github.com/SYSTRAN/faster-whisper) — local, CPU |
| :material-timer: Turn-taking | [smart-turn-v3](https://huggingface.co/pipecat-ai/smart-turn-v3) — local, CPU |
| :material-window-maximize: Window | WebKitGTK via the system PyGObject; falls back to a Chromium app window, which needs nothing installed |
| :material-view-split-vertical: tmux | optional and unused by anything here |
| :material-plus: Optional | an Anthropic credential for contextual acknowledgements |

The full package lists per distribution are in [Installation](installation.md#1-system-packages).

## Python versions

3.11, 3.12 and 3.13 are tested in CI. 3.11 is the floor, for `tomllib`.

You do not install any of them: `uv` provisions an interpreter for the tool's own environment.

## Hardware

Everything runs on the CPU. There is no GPU requirement and no GPU code path — the graphics card appears in this program exactly once, as a row in the HUD's system meters.

Piper synthesis of a one-line summary is faster than real time on any machine that can run a browser. faster-whisper's `small` model is the heaviest thing here; drop `stt.model` to `base` if transcription is slow, at the cost of mishearing technical vocabulary.

## Versioning

The project is at `0.1.0` and pre-1.0. Configuration keys and CLI subcommands may change between releases; when one does, the old spelling is kept working where that is cheap — `claude-voice[stt]` and the legacy `solo` alias are both examples.

Breaking changes are called out in the release notes. `claude-voice doctor` is the thing to run after an upgrade: it names a hook that a newer version added, and a hook frozen on an old form.
