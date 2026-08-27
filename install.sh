#!/usr/bin/env bash
# Set up claude-voice: the package, a Piper voice, a config, and the caches.
#
#   ./install.sh                 English, default voice
#   ./install.sh --preset es     Spanish (es_MX)
#
# Nothing here is required. `uv tool install claude-voice` gets you the whole
# program, ear included; this script exists to also fetch a voice and warm the
# caches, which is the difference between installed and ready.
#
# It does NOT install system packages. It checks for them and names the ones
# you are missing, with the command for your distribution -- running `sudo` on
# somebody else's behalf is not a thing a setup script should do.
#
# It does NOT edit ~/.claude/settings.json. Hooks are yours to install:
# run `claude-voice hooks` afterwards and paste the snippet it prints.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VOICES="$HOME/.local/share/piper-voices"
CONFIG_DIR="${CLAUDE_VOICE_HOME:-$HOME/.config/claude-voice}"

PRESET="en"
while [ $# -gt 0 ]; do
  case "$1" in
    --preset) PRESET="$2"; shift 2 ;;
    # --no-stt used to install without the ear. There is no such install any
    # more: an environment that had it and an environment that did not looked
    # identical until the microphone was needed.
    --no-stt) echo "note: --no-stt is gone; speech-to-text always installs" >&2; shift ;;
    *) echo "unknown flag: $1" >&2; exit 2 ;;
  esac
done

[ -f "$HERE/claude_voice/presets/$PRESET.toml" ] || { echo "no such preset: $PRESET" >&2; exit 2; }

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

# Not fatal, either of them. Dictation without tmux and the frameless window
# without WebKitGTK both have a working fallback -- but a silent downgrade is
# how you end up wondering why the HUD has a title bar, or why `d` refuses.
command -v tmux >/dev/null || cat <<'EOF'
  note: no tmux — dictation has nowhere to deliver text, so `d` and `c` will
        refuse. The voice works without it.
        Debian/Ubuntu: sudo apt install tmux
EOF
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
uv tool install --force --refresh "$HERE"

# --- the voice model ---------------------------------------------------------
MODEL_URL_BASE="https://huggingface.co/rhasspy/piper-voices/resolve/main"
case "$PRESET" in
  es) MODEL="es_MX-ald-medium"; MODEL_PATH="es/es_MX/ald/medium" ;;
  *)  MODEL="en_US-amy-medium"; MODEL_PATH="en/en_US/amy/medium" ;;
esac

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

say "Done."
cat <<EOF

  1. Install the hooks:   claude-voice hooks
     (paste the snippet into ~/.claude/settings.json)
     Already had them? Print it again anyway — the snippet gains hooks over
     time, and \`claude-voice doctor\` names the one you are missing.

  2. Turn the voice on:   claude-voice on
  3. Watch it work:       claude-voice hud

  A second language, later, without reinstalling anything:
      claude-voice lang --fetch es    # download its voice and cache its acks
      claude-voice lang es            # or press l in the HUD

  Check it over any time with:   claude-voice doctor
  Worth an alias, you will open it a lot:
      echo "alias hud='claude-voice hud'" >> ~/.bashrc

  The voice stays off until step 2, and off is the default forever after
  \`claude-voice off\`. While it is off the hook injects nothing, so you spend
  no tokens on spoken summaries nobody hears.
EOF
