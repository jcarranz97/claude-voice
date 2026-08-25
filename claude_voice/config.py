#!/usr/bin/env python3
"""Every knob in one place, so nothing personal is hardcoded in the code.

Layering, lowest priority first:

  1. DEFAULTS below            -- a working English setup, no config file needed
  2. presets/<lang>.toml       -- language pack: voice, espeak codes, phrasing
  3. ~/.config/claude-voice/config.toml   -- the user's own overrides

Anything absent from a layer falls through to the one under it, key by key, so
a config file that sets a single value does not wipe out the rest.

Why TOML and not JSON: the interesting values here are prose -- the spoken
instruction, the acknowledgement phrases, a dictation glossary. Multi-line
strings in JSON are a wall of \\n. Python 3.11+ reads TOML from the standard
library, so this costs no dependency.

Import it the same way the rest of the modules import each other:

    cfg = load()
    cfg.voice_model      # resolved Path
    cfg.get("tts.length_scale", 1.0)
"""

import os
import sys
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:          # Python 3.10 and older
    tomllib = None

HERE = Path(__file__).resolve().parent
REPO = HERE.parent

# Everything mutable lives here: state, logs, queue, cached acks, tuned values.
# Overridable so several profiles can coexist on one machine.
BASE = Path(os.environ.get("CLAUDE_VOICE_HOME",
                           Path.home() / ".config" / "claude-voice"))

CONFIG = BASE / "config.toml"

# A complete, working configuration. If the user never writes a config file,
# these values run -- in English, with a voice install.sh downloads by default.
DEFAULTS = {
    "general": {
        "name": "Claude",          # shown in the HUD banner
        "preset": "en",            # which presets/<name>.toml to layer in
    },
    "tts": {
        "voice_model": "~/.local/share/piper-voices/en_US-amy-medium.onnx",
        "length_scale": 1.0,       # >1 is slower; butler pacing lives near 1.06
        "primary_voice": "en-us",  # espeak-ng code for the bulk of the text
        "foreign_voice": "",       # blank disables the mixed-phoneme pass
        "max_chars": 400,
    },
    "instruction": {
        # Injected into every prompt while the voice is on. This is what makes
        # the model write the spoken line at all, so it is a config value, not
        # a constant: the register belongs to the user, not to the tool.
        "enabled": True,
        "text": "",                # blank -> built from the preset
    },
    "narrate": {
        "enabled": True,
        "word_limit": 50,          # spoken whole below this; trimmed above
        "max_per_turn": 12,
        "min_words": 3,
    },
    "thinking": {
        "enabled": True,
        "delay": 1.75,             # nothing sounds before this
        "interval": 2.45,
        "style": "soft",           # soft | double | low
        "max_run": 150,
        "agent_interval": 4.0,
        "agent_max_run": 1800,
    },
    "ack": {
        "enabled": True,
        "contextual": True,        # ask a small model what to say; else canned
        "model": "claude-haiku-4-5",
        "max_words": 9,
            "timeout": 3.0,
        "phrases": [],             # blank -> from the preset
        "system": "",              # blank -> from the preset
    },
    "stt": {
        "enabled": True,
        "model": "small",          # base mishears technical vocabulary
        "language": "en",
        "device": "default",       # ALSA device for push-to-talk dictation
        "node": "",                # PipeWire node for conversation mode
        "max_secs": 120,
        "glossary": "",
        "hallucinations": [],      # phrases Whisper invents over near-silence
    },
    "listen": {
        "floor_ms": 700,           # min silence before asking smart-turn
        "ceil_ms": 2500,
        "complete": 0.55,
        "min_speech_ms": 300,
        "max_utterance_s": 30,
    },
    "pronunciation": {
        "foreign_terms": [],       # words to phonemize with foreign_voice
        "overrides": {},           # word -> raw espeak IPA, verbatim
    },
    "history": {
        # The spoken log behind the HUD's history pane: what was said out
        # loud, both sides. Nothing else reads it, so turning it off costs
        # only that pane.
        "enabled": True,
        "cap": 400,                # entries kept on disk; older ones are trimmed
        "show": 200,               # entries the panel reads back
        "position": "left",        # left, right or bottom of the HUD window
    },
    "hud": {
        # Spaced out on purpose: the HUD letterspaces them as a title.
        "title": "",               # blank -> general.name
        "thinking": "T H I N K I N G",
        "speaking": "S P E A K I N G",
        "listening": "L I S T E N I N G",
        "ready": "R E A D Y",
        "idle": "S T A N D I N G   B Y",
        "agents": "A G E N T S",
        "voice_off": "V O I C E   O F F",
        "history": "H I S T O R Y",
        "history_empty": "nothing spoken yet",
        "history_you": "you",
        "history_said": "said",
    },
}


def _merge(base: dict, over: dict) -> dict:
    """Deep merge, key by key. A partial config overrides only what it names."""
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def _read(path: Path) -> dict:
    if not tomllib or not path.exists():
        return {}
    try:
        with path.open("rb") as f:
            return tomllib.load(f)
    except Exception as e:
        # Loud on stderr, but never fatal: a typo in the config must not take
        # the voice down mid-session, and hooks discard stdout anyway.
        print(f"claude-voice: ignoring {path}: {e}", file=sys.stderr)
        return {}


class Config:
    def __init__(self, data: dict):
        self._d = data

    def get(self, dotted: str, default=None):
        node = self._d
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def path(self, dotted: str, default: str = "") -> Path:
        return Path(str(self.get(dotted, default))).expanduser()

    # --- the values used often enough to deserve a name -------------------

    @property
    def name(self) -> str:
        return self.get("general.name", "Claude")

    @property
    def voice_model(self) -> Path:
        return self.path("tts.voice_model")

    @property
    def primary_voice(self) -> str:
        return self.get("tts.primary_voice", "en-us")

    @property
    def foreign_voice(self) -> str:
        return self.get("tts.foreign_voice", "")

    @property
    def length_scale(self) -> float:
        return float(self.get("tts.length_scale", 1.0))

    @property
    def foreign_terms(self) -> set:
        return set(self.get("pronunciation.foreign_terms", []) or [])

    @property
    def overrides(self) -> dict:
        return dict(self.get("pronunciation.overrides", {}) or {})

    @property
    def instruction(self) -> str:
        """The prompt injection that makes the model write the spoken line."""
        text = (self.get("instruction.text", "") or "").strip()
        return text

    def as_dict(self) -> dict:
        return self._d


_cached = None


def load(reload: bool = False) -> Config:
    """DEFAULTS <- preset <- user config. Cached; hooks are short-lived anyway."""
    global _cached
    if _cached is not None and not reload:
        return _cached

    data = _merge({}, DEFAULTS)

    # The user's file picks the preset, so read it once to find out, then
    # layer preset under it and read it again on top.
    user = _read(CONFIG)
    preset_name = (user.get("general", {}) or {}).get(
        "preset", DEFAULTS["general"]["preset"])
    if preset_name:
        data = _merge(data, _read(REPO / "presets" / f"{preset_name}.toml"))
    data = _merge(data, user)

    _cached = Config(data)
    return _cached


def show() -> None:
    """`claude-voice config` -- what is actually in effect, and from where."""
    cfg = load()
    print(f"  config file : {CONFIG}{'' if CONFIG.exists() else '  (absent, using defaults)'}")
    print(f"  state dir   : {BASE}")
    print(f"  preset      : {cfg.get('general.preset')}")
    print(f"  voice model : {cfg.voice_model}"
          f"{'' if cfg.voice_model.exists() else '   MISSING'}")
    print(f"  speech      : {cfg.primary_voice}"
          + (f" + {cfg.foreign_voice} for {len(cfg.foreign_terms)} terms"
             if cfg.foreign_voice else " (single language)"))
    print(f"  narrate     : {cfg.get('narrate.word_limit')} words, "
          f"max {cfg.get('narrate.max_per_turn')} per turn")
    print(f"  dictation   : {cfg.get('stt.model')} / {cfg.get('stt.language')} "
          f"on {cfg.get('stt.device')}")
    print(f"  history     : {'on' if cfg.get('history.enabled', True) else 'off'} "
          f"({cfg.get('history.position')}), "
          f"last {cfg.get('history.cap')} spoken lines")


if __name__ == "__main__":
    show()
