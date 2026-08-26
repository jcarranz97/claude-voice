#!/usr/bin/env bash
# Set up claude-voice: virtualenv, Piper voice, config, and a CLI on PATH.
#
#   ./install.sh                 English, default voice
#   ./install.sh --preset es     Spanish (es_MX)
#   ./install.sh --no-stt        skip speech-to-text (smaller install, no mic)
#
# It does NOT edit ~/.claude/settings.json. Hooks are yours to install:
# run `claude-voice hooks` afterwards and paste the snippet it prints.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SHARE="$HOME/.local/share/claude-voice"
VENV="$SHARE/venv"
VOICES="$HOME/.local/share/piper-voices"
CONFIG_DIR="${CLAUDE_VOICE_HOME:-$HOME/.config/claude-voice}"
BINDIR="$HOME/.local/bin"

PRESET="en"
WITH_STT=1
while [ $# -gt 0 ]; do
  case "$1" in
    --preset) PRESET="$2"; shift 2 ;;
    --no-stt) WITH_STT=0; shift ;;
    *) echo "unknown flag: $1" >&2; exit 2 ;;
  esac
done

[ -f "$HERE/presets/$PRESET.toml" ] || { echo "no such preset: $PRESET" >&2; exit 2; }

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }

# --- system dependencies -----------------------------------------------------
say "Checking system tools"
missing=()
command -v aplay     >/dev/null || missing+=("alsa-utils (aplay)")
command -v python3   >/dev/null || missing+=("python3")
[ "$WITH_STT" = 1 ] && { command -v pw-record >/dev/null || missing+=("pipewire-utils (pw-record)"); }
[ "$WITH_STT" = 1 ] && { command -v arecord   >/dev/null || missing+=("alsa-utils (arecord)"); }
if [ ${#missing[@]} -gt 0 ]; then
  echo "  missing: ${missing[*]}"
  echo "  Debian/Ubuntu:  sudo apt install alsa-utils pipewire-audio-client-libraries python3-venv"
  echo "  Fedora:         sudo dnf install alsa-utils pipewire-utils python3"
  echo "  Arch:           sudo pacman -S alsa-utils pipewire python"
  exit 1
fi
echo "  ok"

python3 - <<'EOF' || { echo "  claude-voice needs Python 3.11+ (for tomllib)"; exit 1; }
import sys
raise SystemExit(0 if sys.version_info >= (3, 11) else 1)
EOF

# --- virtualenv --------------------------------------------------------------
say "Creating the virtualenv at $VENV"
mkdir -p "$SHARE"
[ -d "$VENV" ] || python3 -m venv "$VENV"
"$VENV/bin/pip" install --quiet --upgrade pip
echo "  installing piper-tts"
"$VENV/bin/pip" install --quiet piper-tts anthropic
if [ "$WITH_STT" = 1 ]; then
  echo "  installing faster-whisper + onnxruntime (this one is big)"
  "$VENV/bin/pip" install --quiet faster-whisper onnxruntime huggingface_hub numpy
fi
echo "  ok"

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
mkdir -p "$CONFIG_DIR"
if [ -f "$CONFIG_DIR/config.toml" ]; then
  echo "  $CONFIG_DIR/config.toml exists — leaving it alone"
else
  cat > "$CONFIG_DIR/config.toml" <<EOF
# claude-voice — your overrides. Anything left out falls back to
# presets/$PRESET.toml, then to the built-in defaults.
# Run \`claude-voice config\` to see what is actually in effect.

[general]
preset = "$PRESET"
name = "Claude"          # shown in the HUD banner

# The voice model is NOT pinned here on purpose. presets/$PRESET.toml already
# names it, and a pin in this file sits above the preset -- so switching
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
# Record the interpreter so the CLI finds it without an env var. An existing
# venv can be reused by editing this one line.
echo "$VENV/bin/python" > "$CONFIG_DIR/python"

# --- CLI on PATH -------------------------------------------------------------
say "Linking the CLI"
mkdir -p "$BINDIR"
ln -sf "$HERE/bin/claude-voice" "$BINDIR/claude-voice"
chmod +x "$HERE/bin/claude-voice"
echo "  $BINDIR/claude-voice -> $HERE/bin/claude-voice"
case ":$PATH:" in
  *":$BINDIR:"*) ;;
  *) echo "  NOTE: $BINDIR is not on your PATH — add it to your shell rc" ;;
esac

# --- warm the caches ---------------------------------------------------------
say "Building the acknowledgement and tick sounds"
CLAUDE_VOICE_PYTHON="$VENV/bin/python" "$HERE/bin/claude-voice" build-acks || true
CLAUDE_VOICE_PYTHON="$VENV/bin/python" "$HERE/bin/claude-voice" build-ticks || true

say "Done."
cat <<EOF

  1. Install the hooks:   claude-voice hooks
     (paste the snippet into ~/.claude/settings.json)
     Already had them? Print it again anyway — the snippet gains hooks over
     time, and \`claude-voice doctor\` names the one you are missing.

  2. Turn the voice on:   claude-voice on
  3. Watch it work:       claude-voice hud   (in a spare terminal)

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
