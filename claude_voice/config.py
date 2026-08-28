#!/usr/bin/env python3
"""Every knob in one place, so nothing personal is hardcoded in the code.

Layering, lowest priority first:

  1. DEFAULTS below            -- a working English setup, no config file needed
  2. presets/<lang>.toml       -- language pack: voice, espeak codes, phrasing
  3. ~/.config/claude-voice/config.toml   -- the user's own overrides
  4. that file's [preset.<name>] table     -- overrides for one language only

Anything absent from a layer falls through to the one under it, key by key, so
a config file that sets a single value does not wipe out the rest.

Which preset is active is not only the config file's business: BASE/"preset",
a marker file holding a name, is the language switch (`claude-voice lang`, or
`l` in the HUD). It sits above general.preset so the switch never has to edit
hand-written TOML, and absent -- which is the normal case -- the config's own
value stands.

Switching inverts layers 2 and 3, and that is deliberate. A config file
written for Spanish carries Spanish in it: the voice model, the instruction
text, the acknowledgement phrases. Kept on top, those personal values would
keep speaking Spanish inside an English preset, which is the failure that
makes the switch look broken while everything else works. So while the active
preset is NOT the one the config file names, the language pack wins over the
config for the keys it defines -- and only those: a microphone device or a
panel position, which no preset mentions, survives untouched.

To keep a personal setting through a switch, name the language it belongs to:

    [preset.en.tts]
    voice_model = "~/.local/share/piper-voices/en_US-lessac-high.onnx"

That table is the top layer, so it holds whichever way the switch is thrown.

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

# Everything mutable lives here: state, logs, queue, cached acks, tuned values.
# Overridable so several profiles can coexist on one machine.
BASE = Path(os.environ.get("CLAUDE_VOICE_HOME",
                           Path.home() / ".config" / "claude-voice"))

CONFIG = BASE / "config.toml"
# The language switch: a name, or absent. A marker file next to `enabled` and
# `hud-history`, for the same reason those are -- state a keystroke can write
# without a tool ever rewriting the file the user hand-edits.
PRESET_FILE = BASE / "preset"
# Language packs ship inside the package; anything hand-written lives in the
# config directory and shadows a bundled pack of the same name. The install is
# then replaceable without taking someone's own languages with it -- the same
# rule the HUD faces and themes follow: what ships is an example, yours wins.
BUNDLED_PRESETS = HERE / "presets"
USER_PRESETS = BASE / "presets"

# A complete, working configuration. If the user never writes a config file,
# these values run -- in English, with a voice install.sh downloads by default.
DEFAULTS = {
    "general": {
        "name": "Claude",          # shown in the HUD banner
        "preset": "en",            # which presets/<name>.toml to layer in
        # How this preset names its own language, on screen. Written in the
        # language itself -- "Espanol", not "Spanish" -- because it labels the
        # key that switches INTO it, and that key is read by whoever wants it.
        "language": "English",
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
        # Turns of the spoken log the acknowledgement is shown before the
        # prompt it is acknowledging. Without them it can only paraphrase the
        # sentence it was handed, which is how "try it again with the flag"
        # comes back as "Retrying with the flag". 0 sends the prompt alone --
        # exactly the old behaviour -- and every turn is tokens sent, in the
        # one call that has to beat the answer to the speaker.
        "context": 6,
        # Let the call decline to speak. A greeting or a one-line question is
        # answered before an acknowledgement of it would finish playing, so
        # acknowledging it means being talked at twice about nothing. With
        # this on, the model answers SILENT for that and nothing is played --
        # not even the cached phrase. False acknowledges every prompt.
        "skip_quick": True,
        "phrases": [],             # blank -> from the preset
        "system": "",              # blank -> from the preset
        "context_system": "",      # blank -> from the preset; used only with context
        "quick_system": "",        # blank -> from the preset; used only with skip_quick
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
    "mic": {
        # The watchdog: a systemd timer that notices a microphone nobody is
        # watching. The HUD can only warn while it is open, and the failure
        # worth catching is precisely the one where nothing is open.
        "watch": {
            "enabled": True,
            "interval": 60,        # seconds between ticks, for --watch
            "after": 300,          # held this long before the first word
            "repeat": 1800,        # and at most this often after that
            # Process names never worth announcing. Left empty on purpose: an
            # allow-list written in advance hides the one leak you did not
            # predict, and the threshold already absorbs ordinary use.
            "ignore": [],
        },
    },
    "pronunciation": {
        "foreign_terms": [],       # words to phonemize with foreign_voice
        "overrides": {},           # word -> raw espeak IPA, verbatim
    },
    "history": {
        # The spoken log behind the HUD's history pane: what was said out
        # loud, both sides. One log per conversation, so the cap and the
        # retention below are per session. It is also what the acknowledgement
        # reads for context, so turning it off costs that pane AND leaves the
        # acknowledgement seeing only the prompt.
        "enabled": True,
        "cap": 400,                # entries kept per session; older ones trimmed
        "show": 200,               # entries the panel reads back
        "position": "left",        # left, right or bottom of the HUD window
        # A log outlives the turn that produced it, so this clock is days and
        # not turn.py's hours: a conversation you come back to tomorrow still
        # has its history.
        "keep_days": 7,            # a session silent this long is swept away
    },
    "hud": {
        # The HUD is the application: with no window open, the hooks make no
        # sound and start no process, and the microphone daemon and the
        # heartbeat stop themselves. False restores the older behaviour, where
        # the voice runs on hooks alone and the HUD only watches it -- which is
        # the right setting for a machine you never sit in front of, and the
        # wrong one for a laptop with a microphone.
        "required": True,
        # `claude-voice run` opens a HUD when none is open, because a session
        # started with no window comes up mute and nothing on screen says why.
        # At most one is ever opened: the second terminal finds the first.
        # False is for a machine that opens its window some other way.
        "autostart": True,
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
        # The microphone badge under the reactor, while conversation mode is
        # on. The big state word says what I am doing; this says whether the
        # ear is open, which is a different question and the one you ask
        # before you start talking.
        "mic_ready": "ready to listen",
        "mic_hearing": "hearing you",
        "mic_deaf": "nothing is listening",
        # The web HUD (`hud --web`) and the window it opens in.
        #   auto     webview if PyGObject is there, else a browser app window
        #   webview  WebKitGTK, frameless -- the one that looks like the design
        #   browser  Chrome or Chromium in --app mode, its own profile
        #   none     print the address and open nothing
        "shell": "auto",
        "on_top": True,        # keep the window above the rest; needs XWayland
        "decorated": False,    # a title bar would be a second, worse one
        "devtools": False,     # right-click -> Inspect, in the webview
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


# Quality, best first, as Piper names it in the filename.
_QUALITY = {"high": 0, "medium": 1, "low": 2}


def voice_like(model: Path) -> Path:
    """A downloaded voice for the same language, when the named one is absent.

    A preset has to name some voice, but what matters is a voice that speaks
    the language. Refusing to switch to English because en_US-amy-medium.onnx
    specifically was never downloaded -- while en_US-lessac-high.onnx sits in
    the same directory -- is a technicality, not an answer.

    Same locale before same language, then the better quality, then the name,
    so the choice is stable rather than whatever the directory listed first.
    A voice missing its .onnx.json is not a candidate: Piper cannot load it.
    """
    locale = model.name.split("-")[0]              # en_US
    lang = locale.split("_")[0]                    # en
    found = []
    try:
        candidates = list(model.parent.glob("*.onnx"))
    except OSError:
        return model
    for cand in candidates:
        if not (cand.parent / (cand.name + ".json")).exists():
            continue
        cloc = cand.name.split("-")[0]
        if cloc.split("_")[0] != lang:
            continue
        quality = cand.name.removesuffix(".onnx").rsplit("-", 1)[-1]
        found.append((0 if cloc == locale else 1,
                      _QUALITY.get(quality, 3), cand.name, cand))
    return min(found)[3] if found else model


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
    def preset(self) -> str:
        return self.get("general.preset", "en")

    @property
    def language(self) -> str:
        """How this preset names its own language, for the key that picks it."""
        return self.get("general.language", "") or self.preset

    @property
    def voice_model(self) -> Path:
        """The voice that will actually speak: the one asked for if it is on
        disk, otherwise another one for the same language that is."""
        named = self.path("tts.voice_model")
        return named if named.exists() else voice_like(named)

    @property
    def voice_model_named(self) -> Path:
        """What the configuration asked for, substitution or not. This is what
        `--fetch` downloads and what a "missing" message should name."""
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


def preset_path(name: str) -> Path:
    """Where a language pack lives. Yours shadows the one that ships."""
    mine = USER_PRESETS / f"{name}.toml"
    return mine if mine.exists() else BUNDLED_PRESETS / f"{name}.toml"


def presets() -> list:
    """Every language pack on disk, in a stable order -- the cycle `l` walks.

    Both directories, deduplicated by name: a pack shadowing a bundled one is
    the same language, not a second entry in the cycle.
    """
    names = set()
    for d in (BUNDLED_PRESETS, USER_PRESETS):
        try:
            names.update(p.stem for p in d.glob("*.toml"))
        except OSError:
            pass
    return sorted(names)


def configured_preset() -> str:
    """The preset the config file itself names, switch or no switch."""
    user = _read(CONFIG)
    name = (user.get("general", {}) or {}).get(
        "preset", DEFAULTS["general"]["preset"])
    return str(name or DEFAULTS["general"]["preset"])


def active_preset() -> tuple:
    """(name, where it came from): "switch", "config" or "default".

    A marker naming a preset that is not on disk is ignored rather than
    obeyed: a stale switch must not take the voice down, and the config's own
    preset is a working answer.
    """
    try:
        switched = PRESET_FILE.read_text().strip()
    except (OSError, ValueError):
        switched = ""
    if switched and preset_path(switched).exists():
        return switched, "switch"
    name = configured_preset()
    return name, "config" if CONFIG.exists() else "default"


def _compose(preset_name: str, user: dict) -> "Config":
    """The four layers, assembled. See the module docstring for the why."""
    # [preset.<name>] is addressed to one language, so it is not part of the
    # layer that gets stepped over when the language changes.
    per_preset = dict((user.get("preset") or {}).get(preset_name, {}) or {})
    user = {k: v for k, v in user.items() if k != "preset"}
    pack = _read(preset_path(preset_name)) if preset_name else {}

    data = _merge({}, DEFAULTS)
    if preset_name and preset_name != configured_preset():
        # Switched away: the language pack outranks a config file written for
        # the other language.
        data = _merge(data, user)
        data = _merge(data, pack)
    else:
        data = _merge(data, pack)
        data = _merge(data, user)
    data = _merge(data, per_preset)
    # Whatever the layers said, the preset in effect is the one we composed.
    data.setdefault("general", {})["preset"] = preset_name
    return Config(data)


def resolve(preset_name: str) -> Config:
    """What the configuration WOULD be under that preset, without switching.

    The language switch has to know whether the other voice is even on disk
    before it commits, and refusing is only honest if the answer comes from
    the same layering the switch would produce.
    """
    return _compose(preset_name, _read(CONFIG))


_cached = None


def load(reload: bool = False) -> Config:
    """The layers, in effect. Cached; hooks are short-lived anyway.

    reload=True is for the one process that outlives a change: the HUD, whose
    labels have to follow the language switch without being reopened.
    """
    global _cached
    if _cached is not None and not reload:
        return _cached
    _cached = _compose(active_preset()[0], _read(CONFIG))
    return _cached


def show() -> None:
    """`claude-voice config` -- what is actually in effect, and from where."""
    cfg = load()
    print(f"  config file : {CONFIG}{'' if CONFIG.exists() else '  (absent, using defaults)'}")
    print(f"  state dir   : {BASE}")
    name, source = active_preset()
    origin = {"switch": f"switched, {PRESET_FILE.name} file",
              "config": "from the config file",
              "default": "built-in default"}[source]
    others = [p for p in presets() if p != name]
    print(f"  preset      : {name} — {cfg.language} ({origin})"
          + (f"; also on disk: {', '.join(others)}" if others else ""))
    named = cfg.voice_model_named
    print(f"  voice model : {cfg.voice_model}"
          + ("" if cfg.voice_model.exists() else "   MISSING")
          + (f"   (standing in for {named.name}, not downloaded)"
             if cfg.voice_model != named else ""))
    print(f"  speech      : {cfg.primary_voice}"
          + (f" + {cfg.foreign_voice} for {len(cfg.foreign_terms)} terms"
             if cfg.foreign_voice else " (single language)"))
    print(f"  narrate     : {cfg.get('narrate.word_limit')} words, "
          f"max {cfg.get('narrate.max_per_turn')} per turn")
    print(f"  dictation   : {cfg.get('stt.model')} / {cfg.get('stt.language')} "
          f"on {cfg.get('stt.device')}")
    print(f"  history     : {'on' if cfg.get('history.enabled', True) else 'off'} "
          f"({cfg.get('history.position')}), "
          f"last {cfg.get('history.cap')} spoken lines per session, "
          f"kept {cfg.get('history.keep_days')} days")


if __name__ == "__main__":
    show()
