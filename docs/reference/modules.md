---
title: Modules
---

# Modules

The map of what lives where. Useful when reading the source, and when `doctor` or a traceback names a file.

```text
claude_voice/
  cli.py                the only entry point you need
  config.py             layered configuration
  hooks.py              the settings.json snippet, and the merge that installs it
  lang.py               the language switch: preset in, preset out
  voice.py              the switch; the UserPromptSubmit hook
  focus.py              which pane owns the voice when several are open
  presence.py           is a window open; nothing of ours runs while none is
  monitor.py            what holds the microphone and the speakers, anyone's
  speak.py              synthesis, phoneme mixing; the Stop hook
  narrate.py            mid-turn progress; the MessageDisplay hook
  ack.py                the instant acknowledgement
  audioq.py             one sound at a time, in order
  thinking.py           the heartbeat, subagent detection, pane → session
  turn.py               per-session state, one file each
  level.py              how loud the voice is, and the ear
  spokenlog.py          the log of what was said out loud, both sides
  hudcore.py            what the HUD knows; nothing about how it is drawn
  hud.py                the status window, in the terminal
  hudweb.py             the same window, served to a browser engine
  hudshell.py           the frameless window it opens in
  web/                  the page: one html, one css, one js, no build step
  repo.py               the branch, its pull request and its checks
  mic.py                who holds the microphone; the watchdog timer
  run.py                the pty wrapper: the bare `claude-voice`
  dictate.py            push-to-talk, and delivery into a session
  listen.py             conversation mode: VAD + turn detection
  pron.py               pronunciation workbench
  doctor.py             check the install and say what is wrong
  presets/              language packs that ship
```

## Which subcommand reaches which module

The CLI is a dispatch table and an `exec`, so argv, exit codes and signal handling stay identical to running the module file directly.

| Subcommand | Module |
|---|---|
| *(bare)*, `run` | `run.py` |
| `on`, `off`, `focus`, `mute`, `silence`, `status`, `build-acks` | `voice.py` |
| `hud` | `hudweb.py`, or `hud.py` with `--terminal` |
| `sessions` | `turn.py` |
| `history` | `spokenlog.py` |
| `say`, `hook stop` | `speak.py` |
| `ack` | `ack.py` |
| `narrate`, `hook message-display` | `narrate.py` |
| `queue` | `audioq.py` |
| `agents`, `build-ticks`, `hook session-start` | `thinking.py` |
| `hook user-prompt-submit` | `voice.py` |
| `dictate` | `dictate.py` |
| `listen` | `listen.py` |
| `mic` | `mic.py` |
| `monitor` | `monitor.py` |
| `lang` | `lang.py` |
| `pron` | `pron.py` |
| `config` | `config.py` |
| `hooks` | `hooks.py` |
| `doctor` | `doctor.py` |

## The one structural surprise

Modules do not import each other as a package. Each inserts its own directory on `sys.path` and imports its siblings by bare name, or loads one by file path.

That is because **every module here is also a hook entry point**, run directly as a script by Claude Code. The package has to work when a single file is executed with no package context, and `__init__.py` is deliberately side-effect free for the same reason.

It is why `E402` — imports not at the top of the file — is switched off in the lint configuration: the sibling import has to follow the `sys.path` insert.

## Where the state lives

`config.py` owns the directory; everything else asks it. The full inventory is in [Keys and files](keys-and-files.md).

## Tests

One test file per module, in `tests/`, with a shared `conftest.py` that redirects `CLAUDE_VOICE_HOME` to a throwaway directory **before a single module is imported** and empties it between tests.

CI gates coverage at 95% of the package across Python 3.11, 3.12 and 3.13. [Contributing](../contributing.md) covers what the harness does and the three rules every test follows.
