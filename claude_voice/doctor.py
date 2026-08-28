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
import config as _config  # noqa: E402

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
        report(
            BAD,
            "interpreter",
            f"{v.major}.{v.minor} — need 3.11+ for tomllib",
            "uv tool install --force --python 3.12 claude-voice",
        )


def check_tts() -> None:
    try:
        import piper  # noqa: F401
    except ImportError:
        report(
            BAD,
            "piper-tts",
            "not importable by this interpreter",
            f"{sys.executable} -m pip install piper-tts",
        )
        return
    report(OK, "piper-tts", "importable")

    model, named = CFG.voice_model, CFG.voice_model_named
    if model.exists():
        mb = model.stat().st_size / 1e6
        stand = f" — standing in for {named.name}" if model != named else ""
        report(OK, "voice model", f"{model.name} ({mb:.0f} MB){stand}")
    else:
        report(
            BAD,
            "voice model",
            f"missing: {model}",
            "download a voice from huggingface.co/rhasspy/piper-voices, "
            "or fix tts.voice_model in your config",
        )

    cfgfile = model.with_suffix(model.suffix + ".json")
    if model.exists() and not cfgfile.exists():
        report(
            BAD,
            "voice model config",
            f"missing: {cfgfile.name}",
            "download the matching .onnx.json next to the .onnx",
        )


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
        report(
            WARN,
            "audio session",
            "none in this environment",
            "expected over plain SSH and in systemd services — "
            "nothing will play here, but the hooks still fail silent",
        )


def check_config() -> None:
    if _config.CONFIG.exists():
        report(OK, "config", str(_config.CONFIG))
    else:
        report(
            WARN,
            "config",
            "absent, running on defaults",
            f"write {_config.CONFIG} to change voice, language or device",
        )
    name, source = _config.active_preset()
    where = {
        "switch": "switched with `claude-voice lang`",
        "config": "from the config file",
        "default": "built-in default",
    }[source]
    report(
        OK,
        "preset",
        f"{name} — {CFG.language}, {where} "
        f"({CFG.primary_voice}" + (f" + {CFG.foreign_voice}" if CFG.foreign_voice else "") + ")",
    )

    # The other languages on disk, and whether pressing `l` would work. A
    # switch that refuses is only obvious once you press it.
    for other in (p for p in _config.presets() if p != name):
        cfg = _config.resolve(other)
        if cfg.voice_model.exists():
            report(OK, f"preset {other}", f"{cfg.language} — ready, {cfg.voice_model.name}")
        else:
            report(
                WARN,
                f"preset {other}",
                f"{cfg.language}: no voice downloaded for it",
                f"claude-voice lang --fetch {other}",
            )


def _hook_files() -> list:
    """(path, label) for every settings file that could carry hooks."""
    out = []
    for p, label in (
        (Path.home() / ".claude" / "settings.json", "user"),
        (Path.cwd() / ".claude" / "settings.json", "project"),
    ):
        if p.exists():
            out.append((p, label))
    return out


def check_hooks() -> None:
    # Two shapes count as installed: the console script, which carries no
    # paths and survives a reinstall, and the older form naming a module file
    # directly. Both work; only the second can rot when a checkout moves.
    wanted = {
        "UserPromptSubmit": ("voice.py", "user-prompt-submit"),
        "Stop": ("speak.py", "stop"),
        "MessageDisplay": ("narrate.py", "message-display"),
        "SessionStart": ("thinking.py", "session-start"),
    }
    # What is lost when one of the two soft ones is missing. Neither stops the
    # voice, so neither is worth a red line -- but "not installed" alone tells
    # nobody whether it matters.
    soft = {
        "MessageDisplay": "optional: live narration",
        "SessionStart": "the first dictated line of a conversation is filed under `default`",
    }
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
                    for ev, (script, slug) in wanted.items():
                        if ev != event:
                            continue
                        if script in cmd or f"hook {slug}" in cmd:
                            found[ev] = (cmd, label)

    if not found:
        report(
            BAD,
            "hooks",
            "none installed",
            "claude-voice hooks   (then paste into ~/.claude/settings.json)",
        )
        return

    for ev, (_script, slug) in wanted.items():
        if ev not in found:
            why = soft.get(ev)
            report(
                WARN if why else BAD,
                f"hook {ev}",
                "not installed" + (f" ({why})" if why else ""),
                "claude-voice hooks",
            )
            continue
        cmd, label = found[ev]
        # A path frozen at install time is the single most common way this
        # breaks, so say so specifically rather than reporting it missing.
        target = next((tok for tok in cmd.split() if tok.endswith(".py")), "")
        if target and not Path(target).exists():
            report(
                BAD,
                f"hook {ev}",
                f"points at a missing file: {target}",
                "claude-voice hooks   (the checkout moved)",
            )
        elif target:
            report(
                WARN,
                f"hook {ev}",
                f"{Path(target).name} by path  [{label} settings]",
                "claude-voice hooks   (the path-free form survives moves)",
            )
        else:
            report(OK, f"hook {ev}", f"claude-voice hook {slug}  [{label} settings]")


def check_state() -> None:
    # First, because it outranks the switch: with no window open nothing of
    # ours runs at all, and every other line here is then beside the point.
    try:
        import presence as _presence

        n = len(_presence.windows())
        if not _presence.required():
            report(OK, "window", "not required — the voice runs on hooks alone")
        elif n:
            report(OK, "window", f"{n} HUD open")
        else:
            report(
                WARN, "window", "no HUD open — nothing speaks, nothing listens", "claude-voice hud"
            )
    except Exception:
        pass

    on = (BASE / "enabled").exists()
    report(OK if on else WARN, "switch", "ON" if on else "off", "" if on else "claude-voice on")

    # A focus is invisible from every window except the one holding it, so a
    # session that has gone quiet for no apparent reason is exactly the thing
    # somebody runs doctor about.
    try:
        import focus as _focus

        held = _focus.pane()
        if held:
            report(
                OK,
                "focus",
                f"{_focus.label() or held} — only that pane speaks",
                "claude-voice focus --clear   (to give every session its voice back)",
            )
    except Exception:
        pass

    # Per preset: the cache is indexed by position, so one directory shared
    # between languages says one phrase and logs another.
    ack_dir = BASE / "acks" / CFG.preset
    acks = list(ack_dir.glob("*.wav")) if ack_dir.is_dir() else []
    if acks:
        report(OK, "cached acknowledgements", f"{len(acks)} built for {CFG.preset}")
    elif CFG.get("ack.enabled", True):
        report(
            WARN,
            "cached acknowledgements",
            f"none built for {CFG.preset}",
            f"claude-voice build-acks {CFG.preset}",
        )

    if (BASE / "tick.wav").exists():
        report(OK, "heartbeat sounds", "built")
    elif CFG.get("thinking.enabled", True):
        report(
            WARN,
            "heartbeat sounds",
            "not built (they build on first use)",
            "claude-voice build-ticks",
        )

    # Turn state is per session. Saying how many are on file makes the shared
    # state that used to cause this class of bug visible when it misbehaves.
    turns = list(BASE.glob("turn-*.json"))
    report(
        OK,
        "session state",
        f"{len(turns)} session{'s' if len(turns) != 1 else ''} on file"
        if turns
        else "none yet (written on the next turn)",
    )


def check_input() -> None:
    """Optional: everything here is only needed for dictation and listening."""
    if not CFG.get("stt.enabled", True):
        report(OK, "speech-to-text", "disabled in config")
        return

    missing = [m for m in ("faster_whisper", "onnxruntime") if not _importable(m)]
    if missing:
        report(
            WARN,
            "speech-to-text",
            f"missing {', '.join(missing)}",
            f"{sys.executable} -m pip install faster-whisper onnxruntime "
            "huggingface_hub   (skip if you only want the voice)",
        )
    else:
        report(OK, "speech-to-text", "faster-whisper and onnxruntime present")

    for tool, why in (("arecord", "push-to-talk dictation"), ("pw-record", "conversation mode")):
        if shutil.which(tool):
            report(OK, tool, "present")
        else:
            report(WARN, tool, f"not found — needed for {why}")

    dev = CFG.get("stt.device", "default")
    if dev != "default" and shutil.which("arecord"):
        try:
            names = subprocess.run(
                ["arecord", "-L"], capture_output=True, text=True, timeout=5
            ).stdout
            if dev.split(",")[0] in names:
                report(OK, "capture device", dev)
            else:
                report(
                    WARN,
                    "capture device",
                    f"{dev} is not in `arecord -L`",
                    "the headset may be unplugged, or the name changed",
                )
        except Exception:
            pass


def check_window() -> None:
    """Which window `hud --web` would get, and what it would cost to do better.

    A note, never a failure: the terminal HUD needs none of this, and the
    browser fallback needs nothing installed either. What this answers is the
    question you would otherwise answer by opening it and squinting -- whether
    you are getting the frameless webview or a window with a title bar.
    """
    sys.path.insert(0, str(HERE))
    try:
        import hudshell
    except Exception as e:
        return report(WARN, "web HUD", f"unavailable ({e})")

    want = str(CFG.get("hud.shell", "auto") or "auto")
    exe = hudshell._python_with_gi()
    browser = next((b for b in hudshell.BROWSERS if shutil.which(b)), "")

    if exe:
        report(OK, "web HUD window", f"webview, WebKitGTK via {exe}")
    elif browser:
        report(
            WARN,
            "web HUD window",
            f"{browser} app window — a title bar, and about three times the memory",
            "sudo apt install python3-gi gir1.2-webkit2-4.1",
        )
    else:
        report(
            WARN,
            "web HUD window",
            "none — the address is printed instead",
            "sudo apt install python3-gi gir1.2-webkit2-4.1",
        )

    if want != "auto" and want not in hudshell.SHELLS:
        report(
            BAD,
            "hud.shell",
            f"{want!r} is not a shell",
            f"set hud.shell to one of: auto, {', '.join(hudshell.SHELLS)}",
        )


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
    check_window()

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
