"""Every check, against a machine that is not this one.

The doctor's whole job is to look at the developer's install, so every place
it looks is replaced here: the settings files, the voice on disk, the tools on
PATH, the sibling modules it imports by bare name. Nothing in this file reads
the real ``~/.claude/settings.json`` -- that file belongs to whoever is running
the suite, and a test that read it would pass or fail depending on their setup.
"""

import collections
import sys
import types

import pytest

import claude_voice.doctor as doctor

Config = doctor._config.Config


# ---------------------------------------------------------------- helpers


def rows(capsys) -> list:
    """(status, what, detail) for every report line printed."""
    out = []
    for line in capsys.readouterr().out.splitlines():
        if line.startswith("[") and line[7:9] == "] ":
            what, _, detail = line[9:].partition(" — ")
            out.append((line[1:7], what, detail))
    return out


def pick(printed: list, what: str) -> tuple:
    for row in printed:
        if row[1] == what:
            return row
    raise AssertionError(f"no line about {what!r} in {[r[1] for r in printed]}")


def status(printed: list, what: str) -> str:
    return pick(printed, what)[0]


def module(name: str, **attrs):
    """A sibling module the doctor imports by bare name."""
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    return m


def boom(*a, **kw):
    raise OSError("not here")


@pytest.fixture(autouse=True)
def counter(monkeypatch):
    """The failure count is module state; every test starts at none."""
    monkeypatch.setattr(doctor, "_failed", 0)


@pytest.fixture
def voice(home):
    """A voice model on disk, with the .onnx.json Piper needs beside it."""

    def _make(name="en_US-amy-medium.onnx", with_config=True, size=63_000_000):
        model = home / name
        model.write_bytes(b"\0" * 8)
        if with_config:
            model.with_suffix(".onnx.json").write_text("{}")
        return model

    return _make


# ------------------------------------------------------------------ tests


class TestReport:
    """One line per finding, and only a failure counts as one."""

    def test_a_finding_prints_its_detail(self, capsys):
        doctor.report(doctor.OK, "aplay", "present")
        assert rows(capsys) == [(doctor.OK, "aplay", "present")]

    def test_a_failure_prints_the_one_command_that_fixes_it(self, capsys):
        doctor.report(doctor.BAD, "aplay", "not found", "install alsa-utils")
        out = capsys.readouterr().out
        assert "fix: install alsa-utils" in out
        assert doctor._failed == 1

    def test_a_note_is_not_a_problem(self, capsys):
        doctor.report(doctor.WARN, "audio session", "none in this environment")
        assert doctor._failed == 0


class TestPython:
    def test_this_interpreter_is_new_enough(self, capsys):
        doctor.check_python()
        assert status(rows(capsys), "interpreter") == doctor.OK

    def test_an_old_interpreter_names_the_reason(self, capsys, monkeypatch):
        old = collections.namedtuple("v", "major minor micro")(3, 10, 0)
        monkeypatch.setattr(sys, "version_info", old)
        doctor.check_python()
        st, _, detail = pick(rows(capsys), "interpreter")
        assert st == doctor.BAD
        assert "tomllib" in detail


class TestTts:
    """Piper, the voice it speaks with, and the config file beside it."""

    @pytest.fixture(autouse=True)
    def _piper(self, monkeypatch):
        # Present by default and never actually imported: the real package is
        # a model runtime, and importing it costs a second of every test.
        monkeypatch.setitem(sys.modules, "piper", module("piper"))

    def _cfg(self, monkeypatch, model, named=None):
        cfg = types.SimpleNamespace(voice_model=model, voice_model_named=named or model)
        monkeypatch.setattr(doctor, "CFG", cfg)

    def test_piper_that_will_not_import_is_the_first_failure(self, capsys, monkeypatch):
        monkeypatch.setitem(sys.modules, "piper", None)
        doctor.check_tts()
        printed = rows(capsys)
        assert status(printed, "piper-tts") == doctor.BAD
        assert len(printed) == 1  # nothing after it is worth saying

    def test_a_voice_on_disk_is_named_with_its_size(self, capsys, monkeypatch, voice):
        model = voice()
        self._cfg(monkeypatch, model)
        doctor.check_tts()
        st, _, detail = pick(rows(capsys), "voice model")
        assert st == doctor.OK
        assert detail.startswith("en_US-amy-medium.onnx (0 MB)")

    def test_a_substituted_voice_says_what_it_stands_in_for(self, capsys, monkeypatch, voice):
        model = voice("en_US-lessac-high.onnx")
        self._cfg(monkeypatch, model, named=model.parent / "en_US-amy-medium.onnx")
        doctor.check_tts()
        assert "standing in for en_US-amy-medium.onnx" in pick(rows(capsys), "voice model")[2]

    def test_a_missing_voice_says_where_to_get_one(self, capsys, monkeypatch, home):
        self._cfg(monkeypatch, home / "nothing.onnx")
        doctor.check_tts()
        assert status(rows(capsys), "voice model") == doctor.BAD

    def test_a_voice_without_its_json_cannot_be_loaded(self, capsys, monkeypatch, voice):
        self._cfg(monkeypatch, voice(with_config=False))
        doctor.check_tts()
        printed = rows(capsys)
        assert status(printed, "voice model") == doctor.OK
        assert status(printed, "voice model config") == doctor.BAD


class TestAudio:
    def test_aplay_on_the_path_is_enough(self, capsys, monkeypatch):
        monkeypatch.setattr(doctor.shutil, "which", lambda tool: "/usr/bin/aplay")
        doctor.check_audio()
        assert status(rows(capsys), "aplay") == doctor.OK

    def test_no_aplay_is_a_failure(self, capsys, monkeypatch):
        monkeypatch.setattr(doctor.shutil, "which", lambda tool: None)
        doctor.check_audio()
        assert status(rows(capsys), "aplay") == doctor.BAD

    def test_a_pipewire_socket_is_a_reachable_session(self, capsys, monkeypatch, home):
        monkeypatch.setattr(doctor.shutil, "which", lambda tool: None)
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(home))
        (home / "pipewire-0").write_text("")
        doctor.check_audio()
        assert status(rows(capsys), "audio session") == doctor.OK

    def test_no_session_is_a_note_because_the_hooks_still_work(self, capsys, monkeypatch, home):
        monkeypatch.setattr(doctor.shutil, "which", lambda tool: None)
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(home))
        doctor.check_audio()
        assert status(rows(capsys), "audio session") == doctor.WARN


class TestConfig:
    """Which config file is in effect, and which languages are ready."""

    @pytest.fixture(autouse=True)
    def _one_preset(self, monkeypatch):
        monkeypatch.setattr(doctor._config, "presets", lambda: ["en"])

    def test_no_config_file_is_a_note_not_a_failure(self, capsys):
        doctor.check_config()
        assert status(rows(capsys), "config") == doctor.WARN

    def test_the_config_file_is_named_when_there_is_one(self, capsys, home):
        (home / "config.toml").write_text("")
        doctor.check_config()
        assert status(rows(capsys), "config") == doctor.OK

    @pytest.mark.parametrize(
        ("source", "said"),
        [
            ("switch", "switched with `claude-voice lang`"),
            ("config", "from the config file"),
            ("default", "built-in default"),
        ],
    )
    def test_the_preset_says_where_it_came_from(self, capsys, monkeypatch, source, said):
        monkeypatch.setattr(doctor._config, "active_preset", lambda: ("en", source))
        doctor.check_config()
        assert said in pick(rows(capsys), "preset")[2]

    def test_a_second_voice_is_named_alongside_the_first(self, capsys, monkeypatch):
        monkeypatch.setattr(
            doctor,
            "CFG",
            Config({"tts": {"primary_voice": "es", "foreign_voice": "en-us"}}),
        )
        doctor.check_config()
        assert "(es + en-us)" in pick(rows(capsys), "preset")[2]

    def test_another_language_with_a_voice_is_ready(self, capsys, monkeypatch, voice):
        model = voice()
        monkeypatch.setattr(doctor._config, "presets", lambda: ["en", "es"])
        monkeypatch.setattr(
            doctor._config,
            "resolve",
            lambda name: types.SimpleNamespace(language="Espanol", voice_model=model),
        )
        doctor.check_config()
        assert status(rows(capsys), "preset es") == doctor.OK

    def test_another_language_without_one_says_how_to_fetch_it(self, capsys, monkeypatch, home):
        monkeypatch.setattr(doctor._config, "presets", lambda: ["en", "es"])
        monkeypatch.setattr(
            doctor._config,
            "resolve",
            lambda name: types.SimpleNamespace(language="Espanol", voice_model=home / "no.onnx"),
        )
        doctor.check_config()
        st, _, detail = pick(rows(capsys), "preset es")
        assert st == doctor.WARN
        assert "no voice downloaded" in detail


class TestHookFiles:
    """Two settings files can carry hooks, and both are read."""

    def test_only_the_ones_that_exist_are_read(self, monkeypatch, home):
        project = home / "project"
        project.mkdir()
        monkeypatch.setattr(doctor.Path, "home", staticmethod(lambda: home))
        monkeypatch.setattr(doctor.Path, "cwd", staticmethod(lambda: project))
        assert doctor._hook_files() == []

        for where in (home, project):
            (where / ".claude").mkdir()
            (where / ".claude" / "settings.json").write_text("{}")
        assert [label for _p, label in doctor._hook_files()] == ["user", "project"]


class TestHooks:
    """Installed, missing, or installed by the other spelling."""

    @pytest.fixture
    def settings(self, home, monkeypatch):
        """Write a settings file and make it the only one the doctor reads."""

        def _write(text, label="user"):
            p = home / "settings.json"
            p.write_text(text if isinstance(text, str) else doctor.json.dumps(text))
            monkeypatch.setattr(doctor, "_hook_files", lambda: [(p, label)])
            return p

        return _write

    def _hooks(self, **commands):
        return {
            "hooks": {
                event: [{"hooks": [{"type": "command", "command": cmd}]}]
                for event, cmd in commands.items()
            }
        }

    def all_four(self, **over):
        commands = {
            "UserPromptSubmit": "claude-voice hook user-prompt-submit",
            "Stop": "claude-voice hook stop",
            "MessageDisplay": "claude-voice hook message-display",
            "SessionStart": "claude-voice hook session-start",
        }
        commands.update(over)
        return self._hooks(**commands)

    def test_the_path_free_form_is_the_good_one(self, capsys, settings):
        settings(self.all_four())
        doctor.check_hooks()
        printed = rows(capsys)
        assert {r[0] for r in printed} == {doctor.OK}
        assert pick(printed, "hook Stop")[2] == "claude-voice hook stop  [user settings]"

    def test_a_module_named_by_path_still_counts_as_installed(self, capsys, settings, home):
        script = home / "speak.py"
        script.write_text("")
        settings(self.all_four(Stop=f"python {script}"))
        doctor.check_hooks()
        st, _, detail = pick(rows(capsys), "hook Stop")
        assert st == doctor.WARN  # it works, and it rots when the checkout moves
        assert detail == "speak.py by path  [user settings]"

    def test_a_path_that_no_longer_exists_is_named(self, capsys, settings, home):
        settings(self.all_four(Stop=f"python {home / 'moved' / 'speak.py'}"))
        doctor.check_hooks()
        st, _, detail = pick(rows(capsys), "hook Stop")
        assert st == doctor.BAD
        assert "points at a missing file" in detail

    def test_nothing_installed_says_so_once(self, capsys, settings):
        settings({"hooks": {}})
        doctor.check_hooks()
        printed = rows(capsys)
        assert printed == [(doctor.BAD, "hooks", "none installed")]

    def test_a_settings_file_that_is_not_json_is_no_hooks(self, capsys, settings):
        settings("{ this was hand-edited")
        doctor.check_hooks()
        assert status(rows(capsys), "hooks") == doctor.BAD

    def test_a_settings_file_with_no_hooks_key_is_no_hooks(self, capsys, settings):
        settings({"model": "opus"})
        doctor.check_hooks()
        assert status(rows(capsys), "hooks") == doctor.BAD

    def test_hooks_for_other_events_are_not_ours(self, capsys, settings):
        settings(self._hooks(PreToolUse="claude-voice hook stop"))
        doctor.check_hooks()
        assert status(rows(capsys), "hooks") == doctor.BAD

    def test_somebody_elses_hook_on_our_event_is_not_ours(self, capsys, settings):
        settings(self._hooks(Stop="notify-send done"))
        doctor.check_hooks()
        assert status(rows(capsys), "hooks") == doctor.BAD

    def test_a_missing_soft_hook_is_a_note_that_says_what_is_lost(self, capsys, settings):
        installed = self.all_four()
        del installed["hooks"]["MessageDisplay"]
        del installed["hooks"]["SessionStart"]
        settings(installed)
        doctor.check_hooks()
        printed = rows(capsys)
        assert status(printed, "hook MessageDisplay") == doctor.WARN
        assert "live narration" in pick(printed, "hook MessageDisplay")[2]
        assert "filed under `default`" in pick(printed, "hook SessionStart")[2]

    def test_a_missing_hard_hook_is_a_failure(self, capsys, settings):
        installed = self.all_four()
        del installed["hooks"]["Stop"]
        settings(installed)
        doctor.check_hooks()
        assert status(rows(capsys), "hook Stop") == doctor.BAD


class TestState:
    """The window, the switch, and everything cached under the state directory."""

    @pytest.fixture(autouse=True)
    def _siblings(self, monkeypatch):
        monkeypatch.setitem(
            sys.modules, "presence", module("presence", windows=list, required=lambda: True)
        )
        monkeypatch.setitem(
            sys.modules, "focus", module("focus", pane=lambda: "", label=lambda: "")
        )

    def test_no_window_means_nothing_speaks(self, capsys):
        doctor.check_state()
        st, _, detail = pick(rows(capsys), "window")
        assert st == doctor.WARN
        assert "nothing speaks" in detail

    def test_an_open_window_is_counted(self, capsys, monkeypatch):
        monkeypatch.setitem(
            sys.modules,
            "presence",
            module("presence", windows=lambda: [1, 2], required=lambda: True),
        )
        doctor.check_state()
        assert pick(rows(capsys), "window")[2] == "2 HUD open"

    def test_a_window_that_is_not_required_is_not_missed(self, capsys, monkeypatch):
        monkeypatch.setitem(
            sys.modules,
            "presence",
            module("presence", windows=list, required=lambda: False),
        )
        doctor.check_state()
        assert "hooks alone" in pick(rows(capsys), "window")[2]

    def test_a_presence_module_that_breaks_says_nothing(self, capsys, monkeypatch):
        monkeypatch.setitem(sys.modules, "presence", module("presence", windows=boom))
        doctor.check_state()
        with pytest.raises(AssertionError):
            pick(rows(capsys), "window")

    def test_the_switch_is_off_until_the_marker_is_there(self, capsys, home):
        doctor.check_state()
        assert pick(rows(capsys), "switch")[2] == "off"
        (home / "enabled").write_text("")
        doctor.check_state()
        assert pick(rows(capsys), "switch") == (doctor.OK, "switch", "ON")

    def test_a_focus_is_reported_because_it_is_invisible_elsewhere(self, capsys, monkeypatch):
        monkeypatch.setitem(
            sys.modules, "focus", module("focus", pane=lambda: "%12", label=lambda: "voice")
        )
        doctor.check_state()
        assert "voice — only that pane speaks" in pick(rows(capsys), "focus")[2]

    def test_a_focus_module_that_breaks_says_nothing(self, capsys, monkeypatch):
        monkeypatch.setitem(sys.modules, "focus", module("focus", pane=boom))
        doctor.check_state()
        with pytest.raises(AssertionError):
            pick(rows(capsys), "focus")

    def test_acknowledgements_are_counted_per_preset(self, capsys, home):
        acks = home / "acks" / doctor.CFG.preset
        acks.mkdir(parents=True)
        (acks / "one.wav").write_bytes(b"")
        doctor.check_state()
        assert "1 built for" in pick(rows(capsys), "cached acknowledgements")[2]

    def test_none_built_says_how_to_build_them(self, capsys):
        doctor.check_state()
        assert status(rows(capsys), "cached acknowledgements") == doctor.WARN

    def test_acknowledgements_turned_off_are_not_missed(self, capsys, monkeypatch):
        monkeypatch.setattr(doctor, "CFG", Config({"ack": {"enabled": False}}))
        doctor.check_state()
        with pytest.raises(AssertionError):
            pick(rows(capsys), "cached acknowledgements")

    def test_the_heartbeat_is_built_or_says_it_is_not(self, capsys, home):
        doctor.check_state()
        assert status(rows(capsys), "heartbeat sounds") == doctor.WARN
        (home / "tick.wav").write_bytes(b"")
        doctor.check_state()
        assert pick(rows(capsys), "heartbeat sounds")[2] == "built"

    def test_a_heartbeat_turned_off_is_not_missed(self, capsys, monkeypatch):
        monkeypatch.setattr(doctor, "CFG", Config({"thinking": {"enabled": False}}))
        doctor.check_state()
        with pytest.raises(AssertionError):
            pick(rows(capsys), "heartbeat sounds")

    def test_turn_state_is_counted_because_it_is_per_session(self, capsys, home):
        doctor.check_state()
        assert "none yet" in pick(rows(capsys), "session state")[2]
        (home / "turn-a.json").write_text("{}")
        doctor.check_state()
        assert pick(rows(capsys), "session state")[2] == "1 session on file"
        (home / "turn-b.json").write_text("{}")
        doctor.check_state()
        assert pick(rows(capsys), "session state")[2] == "2 sessions on file"


class TestInput:
    """Optional: nothing here stops the voice."""

    @pytest.fixture(autouse=True)
    def _present(self, monkeypatch):
        monkeypatch.setattr(doctor, "_importable", lambda name: True)
        monkeypatch.setattr(doctor.shutil, "which", lambda tool: f"/usr/bin/{tool}")

    def test_speech_to_text_turned_off_is_the_whole_answer(self, capsys, monkeypatch):
        monkeypatch.setattr(doctor, "CFG", Config({"stt": {"enabled": False}}))
        doctor.check_input()
        assert rows(capsys) == [(doctor.OK, "speech-to-text", "disabled in config")]

    def test_both_libraries_present_is_one_line(self, capsys):
        doctor.check_input()
        assert status(rows(capsys), "speech-to-text") == doctor.OK

    def test_a_missing_library_is_a_note_with_the_pip_line(self, capsys, monkeypatch):
        monkeypatch.setattr(doctor, "_importable", lambda name: name != "faster_whisper")
        doctor.check_input()
        st, _, detail = pick(rows(capsys), "speech-to-text")
        assert st == doctor.WARN
        assert detail == "missing faster_whisper"

    def test_the_recorders_are_named_with_what_needs_them(self, capsys, monkeypatch):
        monkeypatch.setattr(doctor.shutil, "which", lambda tool: None)
        doctor.check_input()
        printed = rows(capsys)
        assert status(printed, "arecord") == doctor.WARN
        assert "conversation mode" in pick(printed, "pw-record")[2]

    def test_a_named_capture_device_is_looked_for(self, capsys, monkeypatch, fake_proc):
        monkeypatch.setattr(doctor, "CFG", Config({"stt": {"device": "plughw:2,0"}}))
        monkeypatch.setattr(
            doctor.subprocess, "run", lambda *a, **kw: fake_proc(stdout="plughw:2\nnull\n")
        )
        doctor.check_input()
        assert pick(rows(capsys), "capture device") == (doctor.OK, "capture device", "plughw:2,0")

    def test_a_device_that_is_not_there_is_probably_unplugged(self, capsys, monkeypatch, fake_proc):
        monkeypatch.setattr(doctor, "CFG", Config({"stt": {"device": "plughw:2,0"}}))
        monkeypatch.setattr(doctor.subprocess, "run", lambda *a, **kw: fake_proc(stdout="null\n"))
        doctor.check_input()
        st, _, detail = pick(rows(capsys), "capture device")
        assert st == doctor.WARN
        assert detail == "plughw:2,0 is not in `arecord -L`"

    def test_an_arecord_that_hangs_says_nothing_about_the_device(self, capsys, monkeypatch):
        monkeypatch.setattr(doctor, "CFG", Config({"stt": {"device": "plughw:2,0"}}))
        monkeypatch.setattr(doctor.subprocess, "run", boom)
        doctor.check_input()
        with pytest.raises(AssertionError):
            pick(rows(capsys), "capture device")

    def test_the_default_device_is_not_looked_up(self, capsys, monkeypatch, no_subprocess):
        doctor.check_input()
        with pytest.raises(AssertionError):
            pick(rows(capsys), "capture device")


class TestWindow:
    """Which window the web HUD would get. A note, never a failure."""

    def shell(self, exe="", browsers=(), shells=("webview", "browser")):
        return module(
            "hudshell",
            _python_with_gi=lambda: exe,
            BROWSERS=list(browsers),
            SHELLS=list(shells),
        )

    def test_a_hudshell_that_will_not_import_is_a_note(self, capsys, monkeypatch):
        monkeypatch.setitem(sys.modules, "hudshell", None)
        doctor.check_window()
        assert status(rows(capsys), "web HUD") == doctor.WARN

    def test_webkit_gets_the_frameless_window(self, capsys, monkeypatch):
        monkeypatch.setitem(sys.modules, "hudshell", self.shell(exe="/usr/bin/python3"))
        doctor.check_window()
        st, _, detail = pick(rows(capsys), "web HUD window")
        assert st == doctor.OK
        assert "webview" in detail

    def test_a_browser_gets_a_title_bar_and_says_so(self, capsys, monkeypatch):
        monkeypatch.setitem(sys.modules, "hudshell", self.shell(browsers=["chromium"]))
        monkeypatch.setattr(doctor.shutil, "which", lambda tool: "/usr/bin/chromium")
        doctor.check_window()
        st, _, detail = pick(rows(capsys), "web HUD window")
        assert st == doctor.WARN
        assert detail.startswith("chromium app window")

    def test_neither_prints_the_address_instead(self, capsys, monkeypatch):
        monkeypatch.setitem(sys.modules, "hudshell", self.shell(browsers=["chromium"]))
        monkeypatch.setattr(doctor.shutil, "which", lambda tool: None)
        doctor.check_window()
        assert "the address is printed" in pick(rows(capsys), "web HUD window")[2]

    def test_a_shell_that_does_not_exist_is_a_failure(self, capsys, monkeypatch):
        monkeypatch.setitem(sys.modules, "hudshell", self.shell(exe="/usr/bin/python3"))
        monkeypatch.setattr(doctor, "CFG", Config({"hud": {"shell": "carrier pigeon"}}))
        doctor.check_window()
        st, _, detail = pick(rows(capsys), "hud.shell")
        assert st == doctor.BAD
        assert "is not a shell" in detail

    def test_auto_is_always_a_shell(self, capsys, monkeypatch):
        monkeypatch.setitem(sys.modules, "hudshell", self.shell(exe="/usr/bin/python3"))
        monkeypatch.setattr(doctor, "CFG", Config({"hud": {"shell": "auto"}}))
        doctor.check_window()
        with pytest.raises(AssertionError):
            pick(rows(capsys), "hud.shell")


class TestImportable:
    def test_a_module_that_is_there(self):
        assert doctor._importable("json") is True

    def test_a_module_that_is_not(self):
        assert doctor._importable("no_such_module_anywhere") is False

    def test_a_name_that_breaks_the_finder(self, monkeypatch):
        assert doctor._importable("not.a.package.name") is False


class TestMain:
    """The order of the checks, and what the exit code is for."""

    @pytest.fixture
    def checks(self, monkeypatch):
        order = []
        for name in [n for n in dir(doctor) if n.startswith("check_")]:
            monkeypatch.setattr(doctor, name, lambda _n=name: order.append(_n))
        return order

    def test_everything_in_place_is_a_zero(self, capsys, checks):
        assert doctor.main() == 0
        assert "Everything the voice needs is in place." in capsys.readouterr().out

    def test_the_checks_run_in_the_order_things_break(self, checks):
        doctor.main()
        assert checks == [
            "check_python",
            "check_config",
            "check_tts",
            "check_audio",
            "check_hooks",
            "check_state",
            "check_input",
            "check_window",
        ]

    def test_one_problem_is_counted_and_returned(self, capsys, monkeypatch, checks):
        monkeypatch.setattr(doctor, "check_python", lambda: doctor.report(doctor.BAD, "no"))
        assert doctor.main() == 1
        assert "1 problem to fix above." in capsys.readouterr().out

    def test_several_problems_are_plural(self, capsys, monkeypatch, checks):
        def two():
            doctor.report(doctor.BAD, "one")
            doctor.report(doctor.BAD, "two")

        monkeypatch.setattr(doctor, "check_python", two)
        assert doctor.main() == 1
        assert "2 problems to fix above." in capsys.readouterr().out
