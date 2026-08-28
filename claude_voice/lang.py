#!/usr/bin/env python3
"""The language switch.

  lang.py                    what is speaking now, and what else is on disk
  lang.py <preset>           switch to it
  lang.py --next             the next one in the cycle -- the HUD's `l` key
  lang.py --fetch [preset]   download that preset's voice, and cache its acks

Switching language is switching preset: presets/<name>.toml already carries
everything that changes with it -- the Piper voice, the espeak codes, the
instruction the hook injects, the acknowledgement phrases, the Whisper
language and its glossary, the HUD's own labels. What was missing was a way
to say so without a text editor, which is what this is.

The choice lives in BASE/"preset", a marker file holding a name, read by
config.load() above the config file's own general.preset. Deliberately not in
config.toml: flipping one key there means a tool rewriting hand-written TOML
with comments in it, and that eventually eats something it did not write.

The hooks need no part of this. They are short-lived processes that call
config.load() per invocation, so the next prompt gets the new instruction and
the next dictation the new Whisper language, with nothing to restart. The two
things that DO outlive the switch are the HUD, which reloads its labels in
place, and conversation mode, whose daemon holds its language for the length
of its run -- the HUD restarts it on switching for that reason.

A language with no voice at all refuses. Switching into a preset that cannot
speak would leave silence and no clue why, so the switch declines with the
reason and `--fetch` is the way out. "No voice" means the language, not the
filename: a preset names one .onnx, but any downloaded voice for the same
language will speak it, and config.voice_like() is what finds it. The cycle
skips a language there is nothing to speak it with, so pressing the key never
lands somewhere mute.
"""

import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import config as _config  # noqa: E402

BASE = _config.BASE
PRESET_FILE = _config.PRESET_FILE

VOICE_REPO = "https://huggingface.co/rhasspy/piper-voices/resolve/main"


def _mod(name: str):
    import importlib.util

    spec = importlib.util.spec_from_file_location(name, HERE / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def options() -> list:
    """The cycle, in a stable order: every language pack in presets/."""
    return _config.presets()


def current() -> str:
    return _config.active_preset()[0]


def label(name: str) -> str:
    """How that preset names its own language -- "Espanol", not "Spanish"."""
    try:
        return _config.resolve(name).language
    except Exception:
        return name


def following(name: str = "", speakable: bool = True) -> str:
    """The next preset in the cycle. With two, it is simply the other one.

    speakable drops the ones with no voice on disk: a key that cycles into a
    language nothing can pronounce is a key that refuses for a living.
    """
    name = name or current()
    opts = [p for p in options() if p == name or not speakable or ready(p)[0]]
    if not opts:
        return ""
    try:
        return opts[(opts.index(name) + 1) % len(opts)]
    except ValueError:
        return opts[0]


def ready(name: str) -> tuple:
    """(can it speak, why not). Having a voice is the whole question: every
    other part of a preset is text that ships with the repo.

    Any voice for that language counts, not only the one the preset names --
    see config.voice_like(). What is on your disk is what you get to choose
    between, and a voice you already downloaded is one of those.
    """
    if name not in options():
        return False, f"no preset called {name}"
    cfg = _config.resolve(name)
    if not cfg.voice_model.exists():
        return False, (f"no {cfg.language} voice downloaded — claude-voice lang --fetch {name}")
    return True, ""


def acks_cached(name: str) -> bool:
    return any(_mod("voice").ack_dir(name).glob("*.wav"))


def warm_acks(name: str) -> None:
    """Build that preset's acknowledgement cache, in the background.

    Detached because synthesis takes seconds and this is called from a
    keystroke. Until it lands the acknowledgement is the contextual one,
    synthesized live, or silence -- both of which are better than a keypress
    that appears to hang.
    """
    if acks_cached(name):
        return
    try:
        subprocess.Popen(
            [sys.executable, str(HERE / "voice.py"), "--build-acks", name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        pass


def switch(name: str) -> tuple:
    """Change the language. Returns (ok, what to say about it).

    Silences what is queued on the way through. Switching while a line is
    playing is a deliberate change, not a glitch: an English tail arriving
    after the HUD says Spanish reads as the switch having failed.
    """
    ok, why = ready(name)
    if not ok:
        return False, why
    try:
        PRESET_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = PRESET_FILE.with_suffix(".tmp")
        tmp.write_text(name + "\n")
        os.replace(tmp, PRESET_FILE)
    except OSError as e:
        return False, f"could not write the switch: {e}"

    _config.load(reload=True)
    try:
        _mod("audioq").drain()
    except Exception:
        pass
    warm_acks(name)
    return True, label(name)


def switch_next() -> tuple:
    """What the HUD's `l` does: the next language there is a voice for."""
    now = current()
    nxt = following(now)
    if not nxt or nxt == now:
        missing = [p for p in options() if p != now and not ready(p)[0]]
        if missing:
            return False, (
                f"no voice for {', '.join(label(p) for p in missing)}"
                f" — claude-voice lang --fetch {missing[0]}"
            )
        return False, "only one language pack on disk"
    return switch(nxt)


def _voice_url(model: Path) -> str:
    """Where rhasspy/piper-voices keeps a voice, derived from its filename.

    es_MX-ald-medium.onnx lives at es/es_MX/ald/medium/. The path is the name
    taken apart, which is why nothing here needs a table of voices.
    """
    stem = model.name.removesuffix(".onnx")
    locale, _, rest = stem.partition("-")
    voice, _, quality = rest.rpartition("-")
    if not (locale and voice and quality):
        return ""
    return f"{VOICE_REPO}/{locale.split('_')[0]}/{locale}/{voice}/{quality}/{stem}"


def fetch(name: str) -> bool:
    """Download the Piper voice a preset asks for, and cache its acks.

    The alternative was re-running install.sh to add a second language, which
    rebuilds a virtualenv to place two files.
    """
    if name not in options():
        print(f"  no preset called {name}")
        return False
    # The one the preset NAMES: fetching is how you get that specific voice,
    # even when another one for the language is already standing in for it.
    model = _config.resolve(name).voice_model_named
    if model.exists():
        print(f"  {model.name} already present")
    else:
        base = _voice_url(model)
        if not base:
            print(
                f"  cannot derive a download URL from {model.name} — "
                "fetch it by hand from huggingface.co/rhasspy/piper-voices"
            )
            return False
        import urllib.request

        model.parent.mkdir(parents=True, exist_ok=True)
        for ext in (".onnx", ".onnx.json"):
            dest = model.parent / (model.name.removesuffix(".onnx") + ext)
            print(f"  fetching {dest.name}")
            try:
                # Download beside the target and move it into place, so an
                # interrupted fetch cannot leave a half a voice that exists.
                tmp = dest.with_suffix(dest.suffix + ".part")
                urllib.request.urlretrieve(base + ext, tmp)
                os.replace(tmp, dest)
            except Exception as e:
                print(f"  failed: {e}")
                return False

    print(f"  building the {name} acknowledgements")
    _mod("voice").build_acks(name)
    return True


def show() -> None:
    name, source = _config.active_preset()
    origin = {
        "switch": "switched",
        "config": "from the config file",
        "default": "built-in default",
    }[source]
    for p in options():
        ok, why = ready(p)
        cfg = _config.resolve(p)
        mark = "→" if p == name else " "
        note = f"  ({origin})" if p == name else ""
        if not ok:
            note = f"  — {why}"
        else:
            note += f"  {cfg.voice_model.name}"
            if not acks_cached(p):
                note += "  (acknowledgements not cached yet)"
        print(f"  {mark} {p}  {label(p)}{note}")
    print("\n  claude-voice lang <name> | --next | --fetch <name>")


def main() -> int:
    arg = sys.argv[1] if sys.argv[1:] else ""

    if not arg:
        show()
        return 0
    if arg in ("--next", "next"):
        ok, msg = switch_next()
        print(f"  {msg}" if ok else f"  refused: {msg}")
        return 0 if ok else 1
    if arg in ("--fetch", "fetch"):
        target = sys.argv[2] if sys.argv[2:] else current()
        return 0 if fetch(target) else 1
    if arg in ("-h", "--help", "help"):
        print(__doc__.strip())
        return 0

    ok, msg = switch(arg)
    print(f"  speaking {msg}" if ok else f"  refused: {msg}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
