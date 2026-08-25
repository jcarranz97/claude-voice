#!/usr/bin/env python3
"""Check the install and say what is wrong, in the order it would break.

  claude-voice doctor

Every check prints what it found and, when it fails, the one command that fixes
it. Optional pieces are reported as notes, not failures: dictation and
conversation mode are extras, and plenty of people only want the voice.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import config as _config                              # noqa: E402

CFG = _config.load()
BASE = _config.BASE

OK, BAD, WARN = "  ok  ", " FAIL ", " note "
_failed = 0


def report(status: str, what: str, detail: str = "", fix: str = "") -> None:
    global _failed
    if status == BAD:
        _failed += 1
    print(f"[{status}] {what}" + (f" — {detail}" if detail else ""))
    if fix:
        print(f"         fix: {fix}")


def check_python() -> None:
    v = sys.version_info
    where = sys.executable
    if v >= (3, 11):
        report(OK, "interpreter", f"{v.major}.{v.minor} at {where}")
    else:
        report(BAD, "interpreter", f"{v.major}.{v.minor} — need 3.11+ for tomllib",
               "point ~/.config/claude-voice/python at a newer interpreter")


def check_tts() -> None:
    try:
        import piper  # noqa: F401
    except ImportError:
        report(BAD, "piper-tts", "not importable by this interpreter",
               f"{sys.executable} -m pip install piper-tts")
        return
    report(OK, "piper-tts", "importable")

    model = CFG.voice_model
    if model.exists():
        mb = model.stat().st_size / 1e6
        report(OK, "voice model", f"{model.name} ({mb:.0f} MB)")
    else:
        report(BAD, "voice model", f"missing: {model}",
               "download a voice from huggingface.co/rhasspy/piper-voices, "
               "or fix tts.voice_model in your config")

    cfgfile = model.with_suffix(model.suffix + ".json")
    if model.exists() and not cfgfile.exists():
        report(BAD, "voice model config", f"missing: {cfgfile.name}",
               "download the matching .onnx.json next to the .onnx")


def check_audio() -> None:
    if shutil.which("aplay"):
        report(OK, "aplay", "present")
    else:
        report(BAD, "aplay", "not found", "install alsa-utils")

    rt = os.environ.get("XDG_RUNTIME_DIR")
    live = rt and any((Path(rt) / s).exists() for s in ("pipewire-0", "pulse/native"))
    if live:
        report(OK, "audio session", "PipeWire or PulseAudio is reachable")
    else:
        report(WARN, "audio session", "none in this environment",
               "expected over plain SSH and in systemd services — "
               "nothing will play here, but the hooks still fail silent")


def check_config() -> None:
    if _config.CONFIG.exists():
        report(OK, "config", str(_config.CONFIG))
    else:
        report(WARN, "config", "absent, running on defaults",
               f"write {_config.CONFIG} to change voice, language or device")
    report(OK, "preset", f"{CFG.get('general.preset')} "
           f"({CFG.primary_voice}"
           + (f" + {CFG.foreign_voice}" if CFG.foreign_voice else "") + ")")


def _hook_files() -> list:
    """(path, label) for every settings file that could carry hooks."""
    out = []
    for p, label in ((Path.home() / ".claude" / "settings.json", "user"),
                     (Path.cwd() / ".claude" / "settings.json", "project")):
        if p.exists():
            out.append((p, label))
    return out


def check_hooks() -> None:
    wanted = {"UserPromptSubmit": "voice.py", "Stop": "speak.py",
              "MessageDisplay": "narrate.py"}
    found = {}
    for p, label in _hook_files():
        try:
            d = json.loads(p.read_text())
        except Exception:
            continue
        for event, groups in (d.get("hooks") or {}).items():
            for g in groups:
                for h in g.get("hooks", []):
                    cmd = h.get("command", "")
                    for ev, script in wanted.items():
                        if ev == event and script in cmd:
                            found[ev] = (cmd, label)

    if not found:
        report(BAD, "hooks", "none installed",
               "claude-voice hooks   (then paste into ~/.claude/settings.json)")
        return

    for ev, script in wanted.items():
        if ev not in found:
            optional = ev == "MessageDisplay"
            report(WARN if optional else BAD, f"hook {ev}",
                   "not installed" + (" (optional: live narration)" if optional else ""),
                   "" if optional else "claude-voice hooks")
            continue
        cmd, label = found[ev]
        # The path in the hook is absolute and frozen at install time, so a
        # moved checkout is the single most common way this breaks.
        target = next((tok for tok in cmd.split()
                       if tok.endswith(".py")), "")
        if target and not Path(target).exists():
            report(BAD, f"hook {ev}", f"points at a missing file: {target}",
                   "claude-voice hooks   (the checkout moved)")
        else:
            report(OK, f"hook {ev}",
                   f"{Path(target).name if target else cmd[:40]}  [{label} settings]")


def check_state() -> None:
    on = (BASE / "enabled").exists()
    report(OK if on else WARN, "switch", "ON" if on else "off",
           "" if on else "claude-voice on")

    acks = list((BASE / "acks").glob("*.wav")) if (BASE / "acks").is_dir() else []
    if acks:
        report(OK, "cached acknowledgements", f"{len(acks)} built")
    elif CFG.get("ack.enabled", True):
        report(WARN, "cached acknowledgements", "none built",
               "claude-voice build-acks")

    if (BASE / "tick.wav").exists():
        report(OK, "heartbeat sounds", "built")
    elif CFG.get("thinking.enabled", True):
        report(WARN, "heartbeat sounds", "not built (they build on first use)",
               "claude-voice build-ticks")


def check_input() -> None:
    """Optional: everything here is only needed for dictation and listening."""
    if not CFG.get("stt.enabled", True):
        report(OK, "speech-to-text", "disabled in config")
        return

    missing = [m for m in ("faster_whisper", "onnxruntime")
               if not _importable(m)]
    if missing:
        report(WARN, "speech-to-text", f"missing {', '.join(missing)}",
               f"{sys.executable} -m pip install faster-whisper onnxruntime "
               "huggingface_hub   (skip if you only want the voice)")
    else:
        report(OK, "speech-to-text", "faster-whisper and onnxruntime present")

    for tool, why in (("arecord", "push-to-talk dictation"),
                      ("pw-record", "conversation mode"),
                      ("tmux", "delivering text into a Claude session")):
        if shutil.which(tool):
            report(OK, tool, "present")
        else:
            report(WARN, tool, f"not found — needed for {why}")

    dev = CFG.get("stt.device", "default")
    if dev != "default" and shutil.which("arecord"):
        try:
            names = subprocess.run(["arecord", "-L"], capture_output=True,
                                   text=True, timeout=5).stdout
            if dev.split(",")[0] in names:
                report(OK, "capture device", dev)
            else:
                report(WARN, "capture device", f"{dev} is not in `arecord -L`",
                       "the headset may be unplugged, or the name changed")
        except Exception:
            pass


def _importable(name: str) -> bool:
    import importlib.util
    try:
        return importlib.util.find_spec(name) is not None
    except Exception:
        return False


def main() -> int:
    print(f"\nclaude-voice doctor    state: {BASE}\n")
    check_python()
    check_config()
    check_tts()
    check_audio()
    check_hooks()
    check_state()
    print()
    check_input()

    print()
    if _failed:
        print(f"  {_failed} problem{'s' if _failed != 1 else ''} to fix above.\n")
        return 1
    print("  Everything the voice needs is in place.\n")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
