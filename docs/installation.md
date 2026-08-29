---
title: Installation
---

# Installation

Three steps: system packages, everything else, and turning it on. Only the first needs `sudo`, and it is the one step nothing here does for you.

## 1. System packages

These are your package manager's. A script that runs `sudo` on your behalf is not a thing this project does — the installer checks for them and names the ones you are missing, with the command for your distribution, before it does anything else.

=== "Debian / Ubuntu"

    ```bash
    sudo apt install alsa-utils pipewire-bin python3-gi gir1.2-webkit2-4.1
    ```

=== "Fedora"

    ```bash
    sudo dnf install alsa-utils pipewire-utils python3-gobject webkit2gtk4.1
    ```

=== "Arch"

    ```bash
    sudo pacman -S alsa-utils pipewire python-gobject webkit2gtk-4.1
    ```

Python is **not** among them: [uv](https://docs.astral.sh/uv/) brings its own.

What each one is for, so you can leave out what you do not want:

| Package | For | Needed? |
|---|---|:---:|
| `alsa-utils` | `aplay` to play, `arecord` to record | :material-check-bold: always |
| `pipewire-bin` | `pw-record`, for conversation mode | :material-alert: the ear |
| `python3-gi` + `gir1.2-webkit2-4.1` | the frameless HUD window | :material-minus: optional |

Without the last two the HUD falls back to a Chromium app window, which needs nothing installed and renders identically — it just keeps a title bar. Without `pipewire-bin` the voice still works; conversation mode does not.

!!! info "tmux is deliberately absent from that list"

    Dictation types into a session started with `claude-voice`, which needs nothing installed. See [Do I have to run Claude Code inside tmux?](faq.md#do-i-have-to-run-claude-code-inside-tmux)

Optional, and only for the things that use them:

| Binary | For |
|---|---|
| `notify-send` (libnotify) | the microphone watchdog's notifications |
| `gh` | the HUD's pull-request row |
| `nvidia-smi` | GPU stats on NVIDIA — AMD is read from sysfs |
| `systemctl --user` | the watchdog timer |

## 2. Everything else

```bash
curl -fsSL https://raw.githubusercontent.com/jcarranz97/claude-voice/main/install.sh | bash
```

One command, from any directory, with nothing cloned. It installs `uv` if you do not have it, installs the program, downloads a Piper voice, writes a starter config, synthesizes the cached acknowledgements and the heartbeat sounds, and merges the hooks into `~/.claude/settings.json`.

Everything it does is to your home directory. It never asks for `sudo`, which is why step 1 is a step of yours.

```bash
curl -fsSL .../install.sh | bash -s -- --preset es    # Spanish (es_MX)
curl -fsSL .../install.sh | bash -s -- --no-hooks     # print them, do not install them
curl -fsSL .../install.sh | bash -s -- --editable     # from a clone: the tree itself
```

Run it again whenever you like: an existing config is left alone, a voice already downloaded is not fetched twice, and hooks already installed are reported rather than added again.

### The program on its own

If you would rather do the rest by hand:

```bash
uv tool install claude-voice
```

That is the whole program, voice and ear both. It is an application on your machine, not a checkout: nothing is left pointing at a source tree, and you can use it in any directory.

What it does *not* get you is a voice — Piper does not ship one inside the package, and without one there is nothing to speak with. `claude-voice lang --fetch en` downloads it, which is the part of the script worth having.

??? info "There is nothing behind a flag"

    The ear used to be an `stt` extra, and `claude-voice[stt]` still works so that anything written down against it keeps working — it just installs what the bare name installs.

    The extra bought a smaller install and sold a failure worth more than the disk it saved: extras are not remembered across reinstalls, so one that forgot to name it took the microphone away silently, and the program went on speaking until the next time you pressed ++c++ and it said there was no module named `faster_whisper`.

### From a clone

This installs the working tree instead of the published package, and says which it did:

```bash
git clone https://github.com/jcarranz97/claude-voice
cd claude-voice
./install.sh                 # a copy of the tree, as it is right now
./install.sh --editable      # ... or the tree itself, edits and all
```

`--editable` is the one to use if you are going to change something — see [Contributing](contributing.md).

## 3. The hooks

The installer puts them in. It **merges** rather than pastes: the four entries are added to `~/.claude/settings.json`, everything already in that file stays, and the copy it replaced is kept next to it with a timestamp.

An event already hooked to us is left exactly as it is, so running it again after an upgrade adds only what is new. A settings file it cannot parse is refused rather than repaired — it prints the snippet instead and changes nothing.

Doing it yourself is still a supported path, and the only one for a project's `.claude/settings.json`:

```bash
claude-voice hooks              # print the snippet, paste it yourself
claude-voice hooks --install    # merge it into ~/.claude/settings.json
```

Updating an existing install? Run `--install` again — the snippet gains hooks over time, and a missing one costs you a feature rather than breaking the voice. `claude-voice doctor` names the one you are missing and what it does.

The four hooks, what each does, and the legacy forms `doctor` will warn you about are in the [hooks reference](reference/hooks.md).

## 4. Turning it on

```bash
claude-voice on              # off is the default, always
claude-voice                 # start a session; the HUD opens with it
```

The installer does not do this one. Off is the default the whole program is built around — while it is off the hook injects nothing, so a machine that installed this and never asked for it spends no tokens and makes no sound.

## Upgrading

```bash
uv tool upgrade claude-voice
claude-voice hooks --install    # add any hook the new version brought
claude-voice doctor             # check what moved
```

### Installing again from your own checkout

`uv tool install` copies the code into the tool's own environment. It is not a link back to the source tree, and that is the thing that catches you out the first time you edit the checkout and nothing changes. The installed program keeps running the code it was built from until you replace it, and replacing it takes **both** flags:

```bash
uv tool install --force --refresh "$HOME/repos/claude-voice"   # wherever yours lives
```

!!! danger "`--refresh` is the one everybody leaves out"

    uv caches the wheel it built for a directory, so `--force` on its own reinstalls that cached wheel — your edits are not in it.

    What makes this worth a warning is that nothing complains: the command prints `Installed 1 executable: claude-voice` and exits zero, having installed the same code as before. Without `--force` you get the identical misleading success.

None of this applies to an `--editable` install, which is a pointer at your working tree rather than a copy of it.

**So which one is installed?** Not a question the version answers: it stays at whatever `pyproject.toml` says across every commit, so `uv tool list` prints the same number before and after. Compare the files:

```bash
diff -rq ~/repos/claude-voice/claude_voice \
  ~/.local/share/uv/tools/claude-voice/lib/python3*/site-packages/claude_voice \
  | grep -v __pycache__
```

Silence means the install matches your working tree, and the command above is the fix for anything else. It compares against the files on disk and nothing more — whether *those* are current is what `git status` and a `git fetch` are for.

## Verifying the install

```bash
claude-voice doctor
```

It checks the interpreter, the voice model, the audio session, whether the hooks are installed and still point at files that exist, and reports the optional speech-to-text pieces as notes rather than failures:

```text
[  ok  ] piper-tts — importable
[  ok  ] voice model — en_US-amy-medium.onnx (63 MB)
[ FAIL ] hook Stop — points at a missing file: /old/path/speak.py
         fix: claude-voice hooks   (the checkout moved — replace the old line)
[ note ] switch — off
         fix: claude-voice on
```

It exits non-zero if anything is `FAIL`. Every line carries the command that fixes it, and [Troubleshooting](troubleshooting.md) covers what it cannot see.

## Uninstalling

```bash
claude-voice mic --uninstall           # stop the watchdog timer, if you installed it
uv tool uninstall claude-voice
```

That leaves three things behind, all of them yours to delete or keep: `~/.config/claude-voice/` (config, your presets, state and the spoken logs), `~/.local/share/piper-voices/` (the voice models), and the four hook entries in `~/.claude/settings.json`.
