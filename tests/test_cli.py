"""Dispatch: which module a word reaches, and with what argv.

The entry point hands the process over with ``execv``, so a test that let it
run would be replaced by the module it dispatched to and never report. The
fake below records the handover and raises instead, which is also the only
faithful stand-in: ``execv`` does not return either.
"""

import sys
from pathlib import Path

import pytest

import claude_voice.cli as cli


class Handed(Exception):
    """The handover happened. Carries nothing; the recorder has the argv."""


class Result:
    """What one command line did: handed over, or printed and returned."""

    def __init__(self, code, handed, out, err):
        self.code = code
        self.handed = handed
        self.out = out
        self.err = err

    @property
    def target(self) -> Path:
        assert self.handed, "nothing was handed over"
        return Path(self.handed[0][1][1])

    @property
    def module(self) -> str:
        return self.target.name

    @property
    def argv(self) -> list:
        return self.handed[0][1][2:]

    @property
    def interpreter(self) -> str:
        return self.handed[0][0]


@pytest.fixture
def invoke(monkeypatch, capsys):
    """Run ``main()`` on an argv, catching the handover instead of doing it."""

    def _run(*argv):
        handed = []

        def fake_execv(path, args):
            handed.append((path, list(args)))
            raise Handed

        monkeypatch.setattr(cli.os, "execv", fake_execv)
        monkeypatch.setattr(sys, "argv", ["claude-voice", *argv])
        code = None
        try:
            code = cli.main()
        except Handed:
            pass
        cap = capsys.readouterr()
        return Result(code, handed, cap.out, cap.err)

    return _run


class TestRunning:
    """The bare name starts a session; anything flag-shaped is the child's."""

    def test_the_bare_name_runs_claude(self, invoke):
        r = invoke()
        assert r.module == "run.py"
        assert r.argv == []
        assert r.code is None  # execv does not come back

    def test_a_leading_flag_belongs_to_the_child(self, invoke):
        assert invoke("--resume").argv == ["--resume"]
        assert invoke("--model", "opus").argv == ["--model", "opus"]

    def test_run_is_the_same_thing_spelled_out(self, invoke):
        r = invoke("run", "claude", "--model", "opus")
        assert r.module == "run.py"
        assert r.argv == ["claude", "--model", "opus"]

    def test_the_process_is_handed_to_this_interpreter(self, invoke):
        r = invoke()
        assert r.interpreter == sys.executable
        assert r.handed[0][1][0] == sys.executable

    def test_the_module_is_the_one_next_to_the_cli(self, invoke):
        assert invoke().target.parent == cli.HERE


class TestSwitch:
    """The words that turn the voice on and off all live in voice.py."""

    def test_status_carries_only_the_rest(self, invoke):
        r = invoke("status", "--json")
        assert (r.module, r.argv) == ("voice.py", ["--json"])

    @pytest.mark.parametrize("word", ["on", "off", "focus", "mute", "solo", "silence"])
    def test_a_switch_word_carries_its_own_name(self, invoke, word):
        r = invoke(word)
        assert (r.module, r.argv) == ("voice.py", [word])

    def test_focus_keeps_its_flag(self, invoke):
        assert invoke("focus", "--clear").argv == ["focus", "--clear"]


class TestRoutes:
    """Every table entry reaches its module with its prefix in front."""

    @pytest.mark.parametrize("cmd", sorted(cli.ROUTES))
    def test_the_route_is_taken(self, invoke, cmd):
        module, prefix = cli.ROUTES[cmd]
        r = invoke(cmd, "one", "two")
        assert (r.module, r.argv) == (module, [*prefix, "one", "two"])

    def test_ack_is_always_dry(self, invoke):
        # There is no prompt on a command line, so the acknowledgement is read
        # back rather than spoken -- and that is not the caller's to change.
        assert invoke("ack", "hello").argv == ["--dry-run", "hello"]


class TestHud:
    """Two surfaces, one HUD. The window is the default."""

    def test_the_window_is_the_default(self, invoke):
        r = invoke("hud")
        assert (r.module, r.argv) == ("hudweb.py", [])

    def test_the_window_keeps_its_own_flags(self, invoke):
        assert invoke("hud", "--url").argv == ["--url"]

    def test_web_is_still_accepted_and_dropped(self, invoke):
        r = invoke("hud", "--web", "--url")
        assert (r.module, r.argv) == ("hudweb.py", ["--url"])

    @pytest.mark.parametrize("flag", ["--terminal", "--tty", "--curses"])
    def test_a_terminal_flag_picks_the_curses_hud(self, invoke, flag):
        r = invoke("hud", flag, "--once")
        assert (r.module, r.argv) == ("hud.py", ["--once"])


class TestHooks:
    """What Claude Code calls, and the module that installs it."""

    def test_the_word_reaches_the_hooks_module(self, invoke):
        r = invoke("hooks", "--install")
        assert (r.module, r.argv) == ("hooks.py", ["--install"])

    @pytest.mark.parametrize("event", sorted(cli.HOOKS))
    def test_each_event_reaches_its_module(self, invoke, event):
        module, prefix = cli.HOOKS[event]
        r = invoke("hook", event, "--extra")
        assert (r.module, r.argv) == (module, [*prefix, "--extra"])

    def test_an_unknown_event_is_refused(self, invoke):
        r = invoke("hook", "on-tuesday")
        assert r.code == 2
        assert r.handed == []
        assert "unknown hook: on-tuesday" in r.err
        assert "session-start" in r.err

    def test_no_event_at_all_says_so(self, invoke):
        r = invoke("hook")
        assert r.code == 2
        assert "(none)" in r.err


class TestUsage:
    """Help is printed, and an unknown word points at it."""

    @pytest.mark.parametrize("word", ["-h", "--help", "help"])
    def test_help_prints_the_usage(self, invoke, word):
        r = invoke(word)
        assert r.code == 0
        assert r.handed == []
        assert r.out.startswith("claude-voice — local voice")

    def test_an_unknown_command_points_at_the_help(self, invoke):
        r = invoke("singalong")
        assert r.code == 2
        assert r.handed == []
        assert "unknown command: singalong" in r.err
        assert "claude-voice --help" in r.err
