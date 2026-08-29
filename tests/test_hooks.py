"""Merging the hooks into a settings file.

The file being merged into is somebody's own, usually with things in it that
have nothing to do with us, so every test here is really the same test: what
was already there is still there afterwards.
"""

import json

import pytest

from claude_voice import hooks


@pytest.fixture
def settings(tmp_path):
    return tmp_path / ".claude" / "settings.json"


def read(path):
    return json.loads(path.read_text())


def commands(data, event):
    return [h["command"] for g in data["hooks"][event] for h in g["hooks"]]


class TestFirstInstall:
    def test_it_writes_a_settings_file_that_was_not_there(self, settings):
        r = hooks.install(settings)
        assert r.created and r.backup is None
        assert sorted(r.added) == sorted(hooks.EVENTS)
        assert commands(read(settings), "Stop") == ["claude-voice hook stop"]

    def test_the_prompt_hook_keeps_its_matcher(self, settings):
        hooks.install(settings)
        group = read(settings)["hooks"]["UserPromptSubmit"][0]
        assert group["matcher"] == ""
        # And only that one: an absent matcher is not an empty one.
        assert "matcher" not in read(settings)["hooks"]["Stop"][0]

    def test_an_empty_file_is_not_a_broken_file(self, settings):
        settings.parent.mkdir(parents=True)
        settings.write_text("   \n")
        assert hooks.install(settings).added
        assert read(settings)["hooks"]["Stop"]


class TestKeepingWhatIsThere:
    def test_other_settings_survive(self, settings):
        settings.parent.mkdir(parents=True)
        settings.write_text(json.dumps({"model": "opus", "env": {"FOO": "1"}}))
        hooks.install(settings)
        d = read(settings)
        assert d["model"] == "opus" and d["env"] == {"FOO": "1"}

    def test_somebody_elses_hook_on_the_same_event_survives(self, settings):
        settings.parent.mkdir(parents=True)
        theirs = {"hooks": [{"type": "command", "command": "notify-send done"}]}
        settings.write_text(json.dumps({"hooks": {"Stop": [theirs]}}))
        hooks.install(settings)
        assert commands(read(settings), "Stop") == [
            "notify-send done",
            "claude-voice hook stop",
        ]

    def test_the_file_it_replaced_is_kept(self, settings):
        settings.parent.mkdir(parents=True)
        settings.write_text('{"model": "opus"}')
        r = hooks.install(settings)
        assert r.backup.exists() and read(r.backup)["model"] == "opus"


class TestRunningItTwice:
    def test_the_second_run_changes_nothing(self, settings):
        hooks.install(settings)
        first = settings.read_text()
        r = hooks.install(settings)
        assert r.added == [] and sorted(r.already) == sorted(hooks.EVENTS)
        assert settings.read_text() == first
        assert r.backup is None  # nothing changed, so nothing to keep

    def test_an_older_install_by_path_counts_as_installed(self, settings):
        """The two shapes are the same hook; adding ours would run it twice."""
        settings.parent.mkdir(parents=True)
        old = {"hooks": [{"type": "command", "command": "/usr/bin/python /old/speak.py"}]}
        settings.write_text(json.dumps({"hooks": {"Stop": [old]}}))
        r = hooks.install(settings)
        assert "Stop" in r.already
        assert len(commands(read(settings), "Stop")) == 1

    def test_only_the_missing_event_is_added(self, settings):
        hooks.install(settings)
        d = read(settings)
        del d["hooks"]["MessageDisplay"]
        settings.write_text(json.dumps(d))
        assert hooks.install(settings).added == ["MessageDisplay"]


class TestRefusing:
    def test_json_it_cannot_parse_is_left_alone(self, settings):
        settings.parent.mkdir(parents=True)
        settings.write_text('{"model": "opus",}')
        with pytest.raises(ValueError):
            hooks.install(settings)
        assert settings.read_text() == '{"model": "opus",}'

    def test_a_hooks_key_of_the_wrong_shape_is_refused(self, settings):
        settings.parent.mkdir(parents=True)
        settings.write_text(json.dumps({"hooks": ["not", "an", "object"]}))
        with pytest.raises(ValueError):
            hooks.install(settings)

    def test_a_file_that_is_not_an_object_is_refused(self, settings):
        settings.parent.mkdir(parents=True)
        settings.write_text("[1, 2, 3]")
        with pytest.raises(ValueError):
            hooks.install(settings)

    def test_junk_in_an_event_is_stepped_over_not_read(self, settings):
        """A list of anything is still a list; only our own entries are read."""
        settings.parent.mkdir(parents=True)
        settings.write_text(json.dumps({"hooks": {"Stop": ["nonsense"]}}))
        assert "Stop" in hooks.install(settings).added
        groups = read(settings)["hooks"]["Stop"]
        assert groups[0] == "nonsense"  # left exactly as found
        assert groups[-1]["hooks"][0]["command"] == "claude-voice hook stop"


class TestCommandLine:
    def test_the_bare_word_prints_the_snippet(self, capsys):
        assert hooks.main([]) == 0
        out = capsys.readouterr().out
        assert '"SessionStart"' in out
        assert "claude-voice hook stop" in out

    def test_install_says_what_it_did(self, settings, capsys):
        assert hooks.main(["--settings", str(settings)]) == 0
        out = capsys.readouterr().out
        assert "added:" in out and "Stop" in out
        assert read(settings)["hooks"]["Stop"]

    def test_a_broken_file_fails_without_a_traceback(self, settings, capsys):
        settings.parent.mkdir(parents=True)
        settings.write_text("{oops")
        assert hooks.main(["--settings", str(settings)]) == 1
        assert "hooks:" in capsys.readouterr().err

    def test_an_unknown_flag_is_a_usage_error(self, capsys):
        assert hooks.main(["--wat"]) == 2
        assert "unknown flag" in capsys.readouterr().err

    def test_settings_without_a_path_is_a_usage_error(self, capsys):
        assert hooks.main(["--settings"]) == 2
        assert "needs a path" in capsys.readouterr().err

    def test_help_explains_rather_than_installing(self, capsys):
        assert hooks.main(["--help"]) == 0
        assert "--install" in capsys.readouterr().out

    def test_the_second_run_says_there_is_nothing_to_do(self, settings, capsys):
        hooks.main(["--settings", str(settings)])
        capsys.readouterr()
        assert hooks.main(["--settings", str(settings)]) == 0
        out = capsys.readouterr().out
        assert "nothing to do" in out and "already installed" in out

    def test_it_says_what_it_kept(self, settings, capsys):
        settings.parent.mkdir(parents=True)
        settings.write_text('{"model": "opus"}')
        hooks.main(["--settings", str(settings)])
        assert "kept the previous file as" in capsys.readouterr().out
