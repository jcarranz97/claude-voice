#!/usr/bin/env bash
# Set up claude-voice: the package, a Piper voice, a config, the caches and
# the hooks. One command, from anywhere, with no checkout:
#
#   curl -fsSL https://raw.githubusercontent.com/jcarranz97/claude-voice/main/install.sh | bash
#   curl -fsSL .../install.sh | bash -s -- --preset es      Spanish (es_MX)
#
# Run from a clone instead and it installs that working tree rather than the
# published package, which is what you want while changing something:
#
#   ./install.sh                 English, default voice
#   ./install.sh --preset es     Spanish (es_MX)
#
# It does NOT install system packages. It checks for them and names the ones
# you are missing, with the command for your distribution -- running `sudo` on
# somebody else's behalf is not a thing a setup script should do, and a script
# people pipe into bash is the last place to start.
#
# Working on it? `./install.sh --editable` from your clone installs the
# checkout itself, so an edit is live with nothing to reinstall.
#
# It DOES install the hooks, into ~/.claude/settings.json, merged rather than
# pasted: the four entries are added and everything already in the file is
# kept. `--no-hooks` skips it and prints the snippet to paste yourself.
#
# It does NOT turn the voice on. That is one word afterwards -- `claude-voice
# on` -- and it stays a thing you ask for.

set -euo pipefail

# Piped from curl there is no file and no directory to be in, so SOURCE is
# something like /dev/fd/63. A checkout is the case that can be proven -- a
# pyproject.toml next to the script -- and everything else installs from PyPI.
SOURCE="${BASH_SOURCE[0]:-}"
HERE=""
if [ -n "$SOURCE" ] && [ -f "$SOURCE" ]; then
  HERE="$(cd "$(dirname "$SOURCE")" && pwd)"
  [ -f "$HERE/pyproject.toml" ] && [ -d "$HERE/claude_voice" ] || HERE=""
fi

VOICES="$HOME/.local/share/piper-voices"
CONFIG_DIR="${CLAUDE_VOICE_HOME:-$HOME/.config/claude-voice}"

PRESET="en"
HOOKS=1
EDITABLE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --preset) PRESET="$2"; shift 2 ;;
    --no-hooks) HOOKS=0; shift ;;
    # For working on it: the installed tool runs the checkout itself, so an
    # edit is live with nothing to reinstall. Only means anything from a
    # checkout -- there is nothing to point at when this came down a pipe.
    --editable) EDITABLE=1; shift ;;
    # --no-stt used to install without the ear. There is no such install any
    # more: an environment that had it and an environment that did not looked
    # identical until the microphone was needed.
    --no-stt) echo "note: --no-stt is gone; speech-to-text always installs" >&2; shift ;;
    *) echo "unknown flag: $1" >&2; exit 2 ;;
  esac
done

if [ "$EDITABLE" = "1" ] && [ -z "$HERE" ]; then
  echo "--editable needs a checkout to point at; run it from your clone" >&2
  exit 2
fi

# The preset is resolved to its voice here, before anything is installed: a
# name with no voice behind it is an error rather than a quiet fall back to
# English, and `--preset ex` typed for `es` should cost nothing to find out.
case "$PRESET" in
  es) MODEL="es_MX-ald-medium"; MODEL_PATH="es/es_MX/ald/medium" ;;
  en) MODEL="en_US-amy-medium"; MODEL_PATH="en/en_US/amy/medium" ;;
  *)  echo "no such preset: $PRESET (en, es)" >&2; exit 2 ;;
esac

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }

# --- system dependencies -----------------------------------------------------
# Python is deliberately not among these: uv brings its own.
say "Checking system tools"
missing=()
command -v aplay >/dev/null || missing+=("aplay")
command -v pw-record >/dev/null || missing+=("pw-record")
command -v arecord   >/dev/null || missing+=("arecord")
if [ ${#missing[@]} -gt 0 ]; then
  echo "  missing: ${missing[*]}"
  echo "  Debian/Ubuntu:  sudo apt install alsa-utils pipewire-bin"
  echo "  Fedora:         sudo dnf install alsa-utils pipewire-utils"
  echo "  Arch:           sudo pacman -S alsa-utils pipewire"
  exit 1
fi
echo "  ok"

# Not fatal. The frameless window without WebKitGTK has a working fallback --
# but a silent downgrade is how you end up wondering why the HUD has a title
# bar. tmux is not checked for any more: dictation types into a session
# started with `claude-voice`, and needs nothing installed.
if ! python3 -c "import gi; gi.require_version('Gtk','3.0'); gi.require_version('WebKit2','4.1'); from gi.repository import Gtk, WebKit2" 2>/dev/null; then
  cat <<'EOF'
  note: no WebKitGTK for Python — the HUD will open in a Chromium app window
        instead of a frameless one. Renders the same, keeps a title bar.
        Debian/Ubuntu: sudo apt install python3-gi gir1.2-webkit2-4.1
        Fedora:        sudo dnf install python3-gobject webkit2gtk4.1
        Arch:          sudo pacman -S python-gobject webkit2gtk-4.1
EOF
fi

# --- uv ----------------------------------------------------------------------
say "Checking uv"
if command -v uv >/dev/null; then
  echo "  already installed: $(uv --version)"
else
  echo "  not found — installing from astral.sh"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  # The installer puts it here and edits your shell rc, which does not help
  # the rest of *this* script.
  export PATH="$HOME/.local/bin:$PATH"
  command -v uv >/dev/null || { echo "  uv still not on PATH — open a new shell and re-run" >&2; exit 1; }
  echo "  ok: $(uv --version)"
fi

# --- the package -------------------------------------------------------------
say "Installing claude-voice"
echo "  voice and ear (faster-whisper and onnxruntime — this one is big)"
if [ -n "$HERE" ]; then
  # A checkout installs itself. --refresh is not optional: uv caches the wheel
  # it built for a directory, so --force alone reinstalls the cached one and
  # reports success having changed nothing.
  if [ "$EDITABLE" = "1" ]; then
    echo "  editable, from this checkout: $HERE"
    uv tool install --force --refresh --editable "$HERE"
  else
    echo "  from this checkout: $HERE"
    uv tool install --force --refresh "$HERE"
  fi
else
  echo "  from PyPI"
  uv tool install --force claude-voice
fi

# uv puts it here, and the PATH edit it makes to your shell rc does not help
# the rest of this script.
export PATH="$HOME/.local/bin:$PATH"
command -v claude-voice >/dev/null || {
  echo "  claude-voice is not on PATH — add ~/.local/bin to it and re-run" >&2
  exit 1
}

# --- the voice model ---------------------------------------------------------
MODEL_URL_BASE="https://huggingface.co/rhasspy/piper-voices/resolve/main"
say "Fetching the Piper voice ($MODEL)"
mkdir -p "$VOICES"
if [ -f "$VOICES/$MODEL.onnx" ]; then
  echo "  already present"
else
  for ext in onnx onnx.json; do
    curl -fL --progress-bar -o "$VOICES/$MODEL.$ext" \
      "$MODEL_URL_BASE/$MODEL_PATH/$MODEL.$ext"
  done
  echo "  ok"
fi

# --- config ------------------------------------------------------------------
say "Writing the config"
mkdir -p "$CONFIG_DIR" "$CONFIG_DIR/presets"
if [ -f "$CONFIG_DIR/config.toml" ]; then
  echo "  $CONFIG_DIR/config.toml exists — leaving it alone"
else
  cat > "$CONFIG_DIR/config.toml" <<EOF
# claude-voice — your overrides. Anything left out falls back to the language
# pack for $PRESET, then to the built-in defaults.
# Run \`claude-voice config\` to see what is actually in effect.

[general]
preset = "$PRESET"
name = "Claude"          # shown in the HUD banner

# The voice model is NOT pinned here on purpose. The language pack already
# names it, and a pin in this file sits above the pack -- so switching
# language would change everything except the voice doing the speaking.
# Pin one per language instead, if you want your own:
#
# [preset.$PRESET.tts]
# voice_model = "$VOICES/$MODEL.onnx"

# [narrate]
# word_limit = 50        # spoken whole below this
# max_per_turn = 12

# [stt]
# device = "default"     # \`arecord -L\` to list; prefer a name over an index
# node = ""              # \`pw-record --list-targets\` for conversation mode
EOF
  echo "  wrote $CONFIG_DIR/config.toml"
fi
echo "  your own language packs go in $CONFIG_DIR/presets/"

# --- warm the caches ---------------------------------------------------------
say "Building the acknowledgement and tick sounds"
claude-voice build-acks  || true
claude-voice build-ticks || true

# --- the hooks ---------------------------------------------------------------
# Merged, not pasted: the four entries are added and whatever else is hooked
# in that file stays. Already installed, it says so and changes nothing.
if [ "$HOOKS" = "1" ]; then
  say "Installing the hooks"
  claude-voice hooks --install || {
    echo "  could not merge them — paste them yourself:" >&2
    claude-voice hooks >&2 || true
  }
else
  say "Hooks: skipped (--no-hooks)"
  claude-voice hooks
fi

say "Done."
cat <<EOF

  Two words away from working:

      claude-voice on         turn the voice on (off is the default, always)
      claude-voice            start a session — the HUD opens with it

  A second language, later, without reinstalling anything:
      claude-voice lang --fetch es    # download its voice and cache its acks
      claude-voice lang es            # or press l in the HUD

  Check it over any time with:   claude-voice doctor
  Worth an alias, you will open it a lot:
      echo "alias hud='claude-voice hud'" >> ~/.bashrc

  While the voice is off the hook injects nothing, so you spend no tokens on
  spoken summaries nobody hears.
EOF
