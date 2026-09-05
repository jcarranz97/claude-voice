"""Finding plugins, and surviving them.

Every test here is really the same test twice over: a manifest is read but
code is not, and a plugin that misbehaves costs its own turn and nothing
else. The window has to survive anything an author does.
"""

import textwrap

import pytest

import claude_voice.plug as plug


@pytest.fixture(autouse=True)
def fresh(home, monkeypatch):
    """No index, no imports, no strikes carried between tests."""
    plug.reset()
    monkeypatch.setattr(plug, "INSTALLED", home / "plugins")
    yield
    plug.reset()


def write_plugin(base, name, manifest, code="", filename="panel.py"):
    d = base / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "plugin.toml").write_text(textwrap.dedent(manifest))
    if code:
        (d / filename).write_text(textwrap.dedent(code))
    return d


def a_machine(**kw):
    """What sysstat.system_stats() answers, on a machine with round numbers."""
    got = {
        "cpu": 12.0,
        "mem": 40.0,
        "mem_used": 2**30,
        "mem_total": 2**31,
        "disk": 50.0,
        "disk_free": 2**30,
        "load": [0.5, 0.2, 0.1],
        "gpu": {},
    }
    got.update(kw)
    return got


PANEL = """
    [plugin]
    name = "%s"

    [provides]
    panel = ["%s"]

    [entrypoints]
    panel = "panel:panel"
"""


class TestWhatShipsAndWhatYouInstall:
    def test_the_bundled_ones_are_found(self):
        assert {"system", "github"} <= set(plug.index())

    def test_yours_shadows_the_one_that_ships(self, home):
        write_plugin(
            home / "plugins",
            "system",
            PANEL % ("system", "system"),
            "def panel(ctx):\n    return {'title': 'mine', 'rows': [{'label': 'a'}]}\n",
        )
        plug.reset()
        assert plug.index()["system"]["_bundled"] is False
        assert [p["title"] for p in plug.panels() if p["plugin"] == "system"] == ["mine"]

    def test_a_manifest_naming_something_else_is_not_a_plugin(self, home):
        write_plugin(home / "plugins", "mine", PANEL % ("notmine", "x"))
        plug.reset()
        assert "mine" not in plug.index()
        assert "notmine" not in plug.index()

    def test_an_unreadable_manifest_is_reported_not_raised(self, home):
        d = home / "plugins" / "broken"
        d.mkdir(parents=True)
        (d / "plugin.toml").write_text("this is not toml {{{")
        plug.reset()
        assert "broken" not in plug.index()
        assert "broken" in plug.errors()


class TestNothingIsImportedToAnswerAQuestion:
    def test_listing_does_not_import_the_plugin(self, home):
        write_plugin(
            home / "plugins", "boom", PANEL % ("boom", "boom"), "raise RuntimeError('imported')\n"
        )
        plug.reset()
        assert "boom" in plug.index()  # the manifest was read
        assert plug.provides("panel")  # and so was what it offers

    def test_two_plugins_may_name_their_module_the_same_thing(self, home):
        for name, title in (("one", "first"), ("two", "second")):
            write_plugin(
                home / "plugins",
                name,
                PANEL % (name, name),
                f"def panel(ctx):\n    return {{'title': '{title}', 'rows': [{{'label': 'x'}}]}}\n",
            )
        plug.reset()
        got = {p["plugin"]: p["title"] for p in plug.panels()}
        assert got["one"] == "first"
        assert got["two"] == "second"


class TestTheEnableTable:
    def test_a_plugin_absent_from_the_table_takes_its_manifest_default(self, write_config):
        write_config("")
        plug.reset()
        assert plug.enabled("system") is True

    def test_the_table_wins(self, write_config):
        write_config("[plugins.enabled]\nsystem = false\n")
        plug.reset()
        assert plug.enabled("system") is False
        # Off is off: the panel is not drawn and the numbers are not read.
        assert "system" not in [p["plugin"] for p in plug.panels()]

    def test_the_network_switch_is_the_plugins_own_setting(self, write_config, monkeypatch):
        """Switching the panel off and asking GitHub nothing are two
        different wishes, and both survived the move."""
        import claude_voice.repo as repo

        write_config("[plugins.github]\nnetwork = false\n")
        monkeypatch.setattr(repo, "CFG", plug.cfg())
        assert repo.enabled() is False
        assert plug.enabled("github") is True

    def test_a_plugin_nobody_installed_is_not_enabled(self):
        assert plug.enabled("nothing-like-this") is False


class TestAPluginThatMisbehaves:
    def test_one_that_raises_costs_its_own_turn_and_nothing_else(self, home, write_config):
        write_plugin(
            home / "plugins",
            "bad",
            PANEL % ("bad", "bad"),
            "def panel(ctx):\n    raise ValueError('no')\n",
        )
        plug.reset()
        got = plug.panels()
        assert "bad" not in [p["plugin"] for p in got]
        assert "system" in [p["plugin"] for p in got]  # the rest still drew
        assert "ValueError" in plug.errors()["bad"]

    def test_three_strikes_and_it_is_set_aside(self, home, capsys):
        write_plugin(
            home / "plugins",
            "bad",
            PANEL % ("bad", "bad"),
            "def panel(ctx):\n    raise ValueError('no')\n",
        )
        plug.reset()
        for _ in range(plug.STRIKES):
            plug.call("bad", "panel", None)
        assert "bad" in plug.blocked()
        assert plug.enabled("bad") is False
        assert "disabling plugin bad" in capsys.readouterr().err

    def test_one_that_dawdles_is_disqualified(self, home, monkeypatch):
        write_plugin(
            home / "plugins",
            "slow",
            PANEL % ("slow", "slow"),
            "def panel(ctx):\n    return {'title': 's', 'rows': []}\n",
        )
        plug.reset()
        ticks = iter([0.0, 99.0] * 8)
        monkeypatch.setattr(plug.time, "monotonic", lambda: next(ticks))
        plug.call("slow", "panel", None)
        assert "over" in plug.errors()["slow"]

    def test_a_missing_entry_point_is_an_error_not_a_crash(self, home):
        write_plugin(
            home / "plugins",
            "empty",
            """
            [plugin]
            name = "empty"

            [provides]
            panel = ["empty"]
        """,
        )
        plug.reset()
        assert plug.call("empty", "panel", None) is None
        assert "empty" in plug.errors()

    def test_a_panel_with_nothing_to_say_takes_no_space(self, home):
        write_plugin(
            home / "plugins",
            "quiet",
            PANEL % ("quiet", "quiet"),
            "def panel(ctx):\n    return {}\n",
        )
        plug.reset()
        assert "quiet" not in [p["plugin"] for p in plug.panels()]


class TestTheRowsAPanelReturns:
    """What reaches a renderer. Built by the registry rather than passed
    through, so that a surface can read every key on every row."""

    def _one(self, home, returns):
        write_plugin(
            home / "plugins",
            "one",
            PANEL % ("one", "one"),
            f"def panel(ctx):\n    return {returns}\n",
        )
        plug.reset()
        return next(p for p in plug.panels() if p["plugin"] == "one")

    def test_a_row_carries_every_key_whether_it_asked_to_or_not(self, home):
        got = self._one(home, "{'title': 't', 'rows': [{'label': 'x', 'value': '1'}]}")
        assert got["rows"] == [
            {
                "label": "x",
                "short": "x",
                "value": "1",
                "meter": None,
                "state": "",
                "detail": "",
                "action": "",
                "key": "",
            }
        ]

    def test_a_key_nobody_documented_does_not_reach_a_renderer(self, home):
        got = self._one(home, "{'rows': [{'label': 'x', 'onclick': 'rm -rf /'}]}")
        assert "onclick" not in got["rows"][0]

    def test_the_panel_carries_its_tiles_its_note_and_its_mark(self, home):
        got = self._one(
            home,
            "{'title': 't', 'mark': 'github', 'note': 'a card',"
            " 'rows': [{'label': 'x'}], 'tiles': [{'label': 'load', 'value': '0.5'}]}",
        )
        assert got["mark"] == "github"
        assert got["note"] == "a card"
        assert got["tiles"] == [{"label": "load", "value": "0.5"}]

    def test_a_panel_that_asked_for_none_of_them_still_has_them_all(self, home):
        got = self._one(home, "{'rows': [{'label': 'x'}]}")
        assert got["tiles"] == [] and got["note"] == "" and got["mark"] == ""
        # And a title, because a block has a heading whatever the plugin says.
        assert got["title"] == "one"


class TestWhichWindowAPluginDrawsIn:
    """The terminal and the browser are not equally capable, and a plugin
    that cannot serve one says so rather than shipping a sad version."""

    def test_silence_means_both(self, home):
        write_plugin(home / "plugins", "any", PANEL % ("any", "any"))
        plug.reset()
        assert plug.windows("any") == ["browser", "terminal"]

    def test_a_declared_window_is_the_only_one_it_is_asked_for(self, home):
        write_plugin(
            home / "plugins",
            "wide",
            PANEL % ("wide", "wide") + '\n[requires]\nwindows = ["browser"]\n',
            "def panel(ctx):\n    return {'rows': [{'label': 'x'}]}\n",
        )
        plug.reset()
        # No repository here, so the GitHub panel has nothing to say either
        # way; the one being tested is the one that declared a window.
        assert "wide" in [p["plugin"] for p in plug.panels(window="browser")]
        assert "wide" not in [p["plugin"] for p in plug.panels(window="terminal")]

    def test_a_window_nobody_has_heard_of_is_not_one(self, home):
        write_plugin(
            home / "plugins",
            "odd",
            PANEL % ("odd", "odd") + '\n[requires]\nwindows = ["hologram"]\n',
        )
        plug.reset()
        assert plug.windows("odd") == []

    def test_the_bundled_two_say_where_they_belong(self):
        # Five meters and four tiles need a rail; the terminal has one row.
        assert plug.windows("system") == ["browser"]
        assert plug.windows("github") == ["browser", "terminal"]


class TestWherePanelsSit:
    """Placement is the plugin's suggestion and the config's decision."""

    def test_the_manifest_places_it_when_nobody_else_does(self):
        assert (plug.slot("system"), plug.order("system")) == ("left", 10)
        assert (plug.slot("github"), plug.order("github")) == ("right", 20)

    def test_a_plugin_with_no_opinion_lands_on_the_right(self, home):
        write_plugin(home / "plugins", "bare", PANEL % ("bare", "bare"))
        plug.reset()
        assert (plug.slot("bare"), plug.order("bare")) == ("right", 50)

    def test_your_config_outranks_the_manifest(self, write_config):
        write_config("[plugins.system]\nslot = 'right'\norder = 90\n")
        plug.reset()
        assert (plug.slot("system"), plug.order("system")) == ("right", 90)


class TestWhatAPluginIsHanded:
    def test_it_gets_its_own_settings_and_nobody_elses(self, home, write_config):
        write_config("[plugins.mine]\ncolour = 'blue'\n")
        write_plugin(
            home / "plugins",
            "mine",
            PANEL % ("mine", "mine"),
            "def panel(ctx):\n"
            "    return {'title': ctx.get('colour', 'none'),"
            " 'rows': [{'label': 'x'}]}\n",
        )
        plug.reset()
        write_config("[plugins.mine]\ncolour = 'blue'\n")
        got = {p["plugin"]: p["title"] for p in plug.panels()}
        assert got["mine"] == "blue"

    def test_the_path_it_is_given_is_the_watched_session(self, home):
        write_plugin(
            home / "plugins",
            "where",
            PANEL % ("where", "where"),
            "def panel(ctx):\n    return {'title': ctx.path, 'rows': [{'label': 'x'}]}\n",
        )
        plug.reset()
        got = {p["plugin"]: p["title"] for p in plug.panels("/somewhere")}
        assert got["where"] == "/somewhere"


class TestTheCommand:
    def test_list_names_every_plugin_and_its_state(self, capsys):
        assert plug.main(["list"]) == 0
        out = capsys.readouterr().out
        assert "system" in out and "github" in out and "bundled" in out

    def test_info_says_where_it_came_from(self, capsys):
        assert plug.main(["info", "system"]) == 0
        assert "the wheel" in capsys.readouterr().out

    def test_info_about_nothing_is_a_refusal(self, capsys):
        assert plug.main(["info", "nope"]) == 2
        assert "no such plugin" in capsys.readouterr().err

    def test_enable_prints_the_line_rather_than_editing_your_config(self, capsys):
        assert plug.main(["enable", "github"]) == 0
        out = capsys.readouterr().out
        assert "[plugins.enabled]" in out and "github = true" in out

    def test_a_verb_nobody_defined_is_a_refusal(self, capsys):
        assert plug.main(["fly"]) == 2
        assert "unknown" in capsys.readouterr().err

    def test_help_is_help(self, capsys):
        assert plug.main(["--help"]) == 0
        assert "usage:" in capsys.readouterr().out


class TestTheBundledPanels:
    """The two that moved. Both are formatters over code that already existed,
    so what matters is that they say nothing rather than something wrong."""

    def _github(self, monkeypatch, info):
        fn = plug._entry("github", "panel")
        monkeypatch.setattr(fn.__globals__["_repo"], "info", lambda w: info)
        return fn(plug._Ctx("github", "/anywhere"))

    def test_a_directory_that_is_not_a_repository_draws_nothing(self, monkeypatch):
        assert self._github(monkeypatch, {}) == {}

    def test_a_branch_with_no_pull_request_is_the_repository_and_the_branch(self, monkeypatch):
        got = self._github(monkeypatch, {"branch": "main", "name": "claude-voice"})
        assert [r["label"] for r in got["rows"]] == ["repo", "branch"]
        assert got["mark"] == "github"

    def test_a_detached_head_is_a_warning(self, monkeypatch):
        got = self._github(monkeypatch, {"branch": "abc1234", "detached": True})
        row = got["rows"][1]
        assert row["state"] == "warn" and row["value"] == "abc1234 (detached)"

    def test_a_pull_request_brings_its_title_and_its_checks(self, monkeypatch):
        got = self._github(
            monkeypatch,
            {
                "branch": "main",
                "pr": {
                    "number": 38,
                    "state": "open",
                    "title": "Make the panels plugins",
                    "checks": {"state": "running", "running": 2},
                },
            },
        )
        rows = {r["label"]: r for r in got["rows"]}
        assert rows["pull request"]["value"] == "#38 · open"
        assert rows["pull request"]["short"] == "pr"
        assert rows["pull request"]["detail"] == "Make the panels plugins"
        # Open is where a pull request lives, not news about it.
        assert rows["pull request"].get("state") is None
        assert rows["checks"]["value"] == "● 2 running"
        assert rows["checks"]["state"] == "busy"

    def test_a_draft_says_draft_and_a_merge_is_the_good_news(self, monkeypatch):
        draft = {"number": 1, "state": "open", "draft": True, "checks": {}}
        got = self._github(monkeypatch, {"branch": "main", "pr": draft})
        assert got["rows"][2]["value"] == "#1 · draft"

        merged = {"number": 1, "state": "merged", "checks": {}}
        got = self._github(monkeypatch, {"branch": "main", "pr": merged})
        assert got["rows"][2]["state"] == "ok"

    def test_the_failing_checks_are_named_in_the_detail(self, monkeypatch):
        checks = {"state": "failing", "fail": 2, "pass": 5, "failing": ["lint", "tests"]}
        got = self._github(
            monkeypatch,
            {"branch": "main", "pr": {"number": 3, "state": "open", "checks": checks}},
        )
        row = got["rows"][3]
        assert row["value"] == "✗ 2 failing · 5 passing"
        assert row["detail"] == "lint, tests"
        assert row["state"] == "warn"

    def test_no_gh_says_so_once_rather_than_looking_broken(self, monkeypatch):
        got = self._github(monkeypatch, {"branch": "main", "gh": False})
        assert [r["value"] for r in got["rows"]][-1] == "no gh"

    def test_the_system_panel_draws_the_rows_the_hud_had(self, monkeypatch):
        fn = plug._entry("system", "panel")
        monkeypatch.setattr(
            fn.__globals__["sysstat"],
            "system_stats",
            lambda: a_machine(
                gpu={
                    "busy": 7.0,
                    "vram": 3.0,
                    "vram_used": 2e9,
                    "vram_total": 8e9,
                    "name": "Radeon 780M",
                }
            ),
        )
        got = fn(plug._Ctx("system"))
        assert [r["label"] for r in got["rows"]] == ["cpu", "ram", "disk", "gpu", "vram"]
        assert got["rows"][2]["state"] is None
        assert got["note"] == "Radeon 780M"

    def test_the_absolutes_behind_the_percentages_are_tiles(self, monkeypatch):
        fn = plug._entry("system", "panel")
        monkeypatch.setattr(
            fn.__globals__["sysstat"],
            "system_stats",
            lambda: a_machine(
                gpu={"busy": 7.0, "vram": 3.0, "vram_used": 2**31, "vram_total": 2**33}
            ),
        )
        got = fn(plug._Ctx("system"))
        assert got["tiles"] == [
            {"label": "memory", "value": "1.0 / 2.0 GB"},
            {"label": "free", "value": "1.0 GB"},
            {"label": "vram", "value": "2.0 / 8.0 GB"},
            {"label": "load", "value": "0.50 0.20 0.10"},
        ]

    def test_a_full_disk_is_the_one_row_that_is_alarming(self, monkeypatch):
        fn = plug._entry("system", "panel")
        monkeypatch.setattr(
            fn.__globals__["sysstat"],
            "system_stats",
            lambda: a_machine(disk=97.0),
        )
        got = fn(plug._Ctx("system"))
        assert got["rows"][2]["state"] == "warn"

    def test_a_machine_with_no_gpu_has_no_gpu_rows(self, monkeypatch):
        fn = plug._entry("system", "panel")
        monkeypatch.setattr(
            fn.__globals__["sysstat"],
            "system_stats",
            lambda: a_machine(gpu={"busy": None, "vram": None}),
        )
        got = fn(plug._Ctx("system"))
        assert [r["label"] for r in got["rows"]] == ["cpu", "ram", "disk"]
        assert [t["label"] for t in got["tiles"]] == ["memory", "free", "load"]
        assert got["note"] == ""

    def test_the_system_panel_says_nothing_when_the_numbers_are_gone(self, monkeypatch):
        fn = plug._entry("system", "panel")
        monkeypatch.setattr(fn.__globals__["sysstat"], "system_stats", dict)
        assert fn(plug._Ctx("system")) == {}

    def test_a_bundled_plugin_carries_the_application_version(self):
        assert plug.version("system") == plug._VERSION

    def test_surfaces_is_not_something_a_plugin_provides(self, home):
        write_plugin(
            home / "plugins",
            "claims",
            """
            [plugin]
            name = "claims"

            [provides]
            panel = ["claims"]
            surfaces = ["panel"]
        """,
        )
        plug.reset()
        assert plug.contributes("claims") == "panel"
