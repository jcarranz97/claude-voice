"""The heartbeat: who is alive, which pane is which session, and the tick loop.

Nothing here is allowed to sleep or to reach a real ``aplay``. The loop is
driven by a fake clock installed over the module's ``time``, so a two-minute
run costs the same as a two-second one and the gaps it chose can be read back
off the recorder instead of waited out.
"""

import json
import os
import types

import pytest

import claude_voice.thinking as thinking


class Clock:
    """A stand-in for ``time``: sleeping moves the hands instead of waiting."""

    def __init__(self, start: float = 1000.0):
        self.now = start
        self.slept = []

    def time(self):
        return self.now

    def sleep(self, secs):
        self.slept.append(secs)
        self.now += secs


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(thinking, "time", c)
    return c


@pytest.fixture
def projects(home, monkeypatch):
    """Redirect ``AGENT_ROOT`` into the test home.

    It defaults to the real ``~/.claude/projects``, and a test reading the
    developer's own transcripts answers differently on every machine.
    """
    root = home / "projects"
    root.mkdir()
    monkeypatch.setattr(thinking, "AGENT_ROOT", root)
    return root


def agent_file(root, project: str, session: str, name: str = "agent-one") -> "os.PathLike":
    d = root / project / session / "subagents"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{name}.jsonl"
    p.write_text("{}\n")
    return p


class TestTiming:
    """`timing` prefers the tuned file, so a change lands without a restart."""

    def test_falls_back_to_the_configured_defaults(self):
        assert thinking.timing() == (thinking.DELAY, thinking.INTERVAL)

    def test_the_tuned_file_wins(self, home):
        thinking.TUNE.write_text(json.dumps({"delay": 0.5, "interval": 9.0}))
        assert thinking.timing() == (0.5, 9.0)

    def test_a_corrupt_tuned_file_is_ignored(self, home):
        thinking.TUNE.write_text("not json")
        assert thinking.timing() == (thinking.DELAY, thinking.INTERVAL)

    def test_tune_writes_what_show_reads_back(self, home, capsys):
        thinking.tune(0.25, 3.5)
        assert thinking.timing() == (0.25, 3.5)
        assert "0.25" in capsys.readouterr().out


class TestLastLine:
    """Transcripts weigh hundreds of KB, so only the tail is ever read."""

    def test_returns_the_final_non_empty_line(self, home):
        p = home / "t.jsonl"
        p.write_text('{"a": 1}\n{"b": 2}\n\n')
        assert thinking._last_line(p) == b'{"b": 2}'

    def test_an_empty_file_gives_an_empty_line(self, home):
        p = home / "t.jsonl"
        p.write_text("")
        assert thinking._last_line(p) == b""

    def test_only_the_tail_is_read(self, home):
        p = home / "t.jsonl"
        p.write_text("x" * 5000 + "\nlast\n")
        assert thinking._last_line(p, nbytes=16) == b"last"


class TestMidTool:
    """An agent waiting on a long tool writes nothing and must not read dead."""

    def test_an_open_tool_call_counts_as_working(self, home):
        p = home / "t.jsonl"
        p.write_text(json.dumps({"message": {"content": [{"type": "tool_use"}]}}))
        assert thinking._mid_tool(p) is True

    def test_plain_text_does_not(self, home):
        p = home / "t.jsonl"
        p.write_text(json.dumps({"message": {"content": [{"type": "text"}]}}))
        assert thinking._mid_tool(p) is False

    def test_a_missing_message_does_not(self, home):
        p = home / "t.jsonl"
        p.write_text(json.dumps({"type": "summary"}))
        assert thinking._mid_tool(p) is False

    def test_unreadable_json_does_not(self, home):
        p = home / "t.jsonl"
        p.write_text("half a line")
        assert thinking._mid_tool(p) is False


class TestSlug:
    """The project directory is the path with the slashes swapped out."""

    def test_slashes_become_dashes(self):
        assert thinking._slug("/home/x/repos/y") == "-home-x-repos-y"

    def test_a_trailing_slash_makes_no_difference(self):
        assert thinking._slug("/home/x/") == thinking._slug("/home/x")


class TestLastAiTitle:
    """The title is rewritten as the conversation goes; only the last one counts."""

    def test_the_last_title_line_wins(self, home):
        p = home / "s.jsonl"
        p.write_text(
            '{"type":"ai-title","aiTitle":"first","sessionId":"s1"}\n'
            '{"type":"user"}\n'
            '{"type":"ai-title","aiTitle":"second","sessionId":"s1"}\n'
        )
        assert thinking._last_ai_title(p)["aiTitle"] == "second"

    def test_a_transcript_without_one_is_empty(self, home):
        p = home / "s.jsonl"
        p.write_text('{"type":"user"}\n')
        assert thinking._last_ai_title(p) == {}

    def test_a_missing_file_is_empty(self, home):
        assert thinking._last_ai_title(home / "nope.jsonl") == {}

    def test_a_corrupt_title_line_is_empty(self, home):
        p = home / "s.jsonl"
        p.write_text('{"type":"ai-title", broken\n')
        assert thinking._last_ai_title(p) == {}


class TestPaneBinding:
    """The exact join between a terminal and the conversation running in it."""

    def test_a_pane_id_loses_its_percent_sign(self):
        assert thinking._pane_file("%12").name == "pane-12.json"

    def test_an_empty_pane_has_no_file(self):
        assert thinking._pane_file("") is None
        assert thinking._pane_file("%%%") is None

    def test_binding_records_the_session_and_the_directory(self, home):
        thinking.bind("s1", pane="%3", cwd="/tmp/project")
        assert thinking.bound_session("%3") == "s1"
        assert thinking.bound_session("%3", cwd="/tmp/project") == "s1"

    def test_the_pane_comes_from_the_environment_when_unnamed(self, home, monkeypatch):
        monkeypatch.setenv("TMUX_PANE", "%7")
        thinking.bind("s2", cwd="/tmp/p")
        assert thinking.bound_session("%7") == "s2"

    def test_outside_tmux_there_is_nothing_to_bind(self, home):
        thinking.bind("s1")  # no pane, no $TMUX_PANE
        assert thinking.pane_files() == []
        thinking.bind("", pane="%3")
        assert thinking.pane_files() == []

    def test_an_unusable_pane_id_binds_nothing(self, home):
        thinking.bind("s1", pane="%%%")
        assert thinking.pane_files() == []

    def test_a_failed_write_is_swallowed(self, home, monkeypatch):
        def boom(*a, **kw):
            raise OSError("read-only")

        monkeypatch.setattr(thinking, "os", types.SimpleNamespace(environ=os.environ, replace=boom))
        thinking.bind("s1", pane="%3")  # a hook must not fail over a binding
        assert thinking.bound_session("%3") == ""

    def test_an_unbound_pane_answers_nothing(self, home):
        assert thinking.bound_session("%9") == ""
        assert thinking.bound_session("") == ""

    def test_a_corrupt_binding_answers_nothing(self, home):
        (home / "pane-4.json").write_text("{ not json")
        assert thinking.bound_session("%4") == ""

    def test_a_pane_that_moved_directory_answers_nothing(self, home):
        thinking.bind("s1", pane="%3", cwd="/tmp/one")
        assert thinking.bound_session("%3", cwd="/tmp/two") == ""

    def test_stale_bindings_are_swept(self, home, monkeypatch):
        thinking.bind("s1", pane="%3", cwd="/tmp/one")
        f = thinking.pane_files()[0]
        os.utime(f, (0, 0))
        thinking.sweep_panes()
        assert thinking.pane_files() == []

    def test_sweeping_survives_a_file_that_vanished(self, home, monkeypatch):
        monkeypatch.setattr(thinking, "pane_files", lambda: [home / "pane-gone.json"])
        thinking.sweep_panes()  # the OSError is the point: it must not raise

    def test_listing_panes_survives_an_unreadable_base(self, monkeypatch):
        def boom(*a):
            raise OSError

        monkeypatch.setattr(thinking, "BASE", types.SimpleNamespace(glob=boom))
        assert thinking.pane_files() == []


class TestSessionFor:
    """Which conversation runs in a pane: the binding first, the title second."""

    def test_the_binding_is_the_exact_answer(self, home, projects):
        thinking.bind("bound-session", pane="%3", cwd="/tmp/p")
        assert thinking.session_for("/tmp/p", "some title", "%3") == "bound-session"

    def test_a_window_that_has_not_spoken_matches_nothing(self, home, projects):
        assert thinking.session_for("/tmp/p", "", "%3") == ""
        assert thinking.session_for("/tmp/p", "✳ ", "%3") == ""

    def test_a_project_with_no_transcripts_matches_nothing(self, home, projects):
        assert thinking.session_for("/tmp/absent", "a title") == ""

    def test_the_title_finds_the_transcript_that_carries_it(self, home, projects):
        d = projects / thinking._slug("/tmp/p")
        d.mkdir(parents=True)
        (d / "other.jsonl").write_text('{"type":"ai-title","aiTitle":"elsewhere"}\n')
        (d / "wanted.jsonl").write_text(
            '{"type":"ai-title","aiTitle":"the title","sessionId":"s-42"}\n'
        )
        assert thinking.session_for("/tmp/p", "✳ the title") == "s-42"

    def test_a_title_line_without_a_session_falls_back_to_the_filename(self, home, projects):
        d = projects / thinking._slug("/tmp/p")
        d.mkdir(parents=True)
        (d / "by-name.jsonl").write_text('{"type":"ai-title","aiTitle":"the title"}\n')
        assert thinking.session_for("/tmp/p", "the title") == "by-name"

    def test_no_transcript_carries_that_title(self, home, projects):
        d = projects / thinking._slug("/tmp/p")
        d.mkdir(parents=True)
        (d / "a.jsonl").write_text('{"type":"ai-title","aiTitle":"another"}\n')
        assert thinking.session_for("/tmp/p", "the title") == ""

    def test_an_unreadable_project_matches_nothing(self, home, projects, monkeypatch):
        d = projects / thinking._slug("/tmp/p")
        d.mkdir(parents=True)
        monkeypatch.setattr(thinking.Path, "glob", _raise_oserror)
        assert thinking.session_for("/tmp/p", "the title") == ""


def _raise_oserror(*a, **kw):
    raise OSError


class TestSessionsIn:
    """The narrowing for a pane the title lookup cannot name."""

    def test_liveliest_first(self, home, projects):
        d = projects / thinking._slug("/tmp/p")
        d.mkdir(parents=True)
        (d / "old.jsonl").write_text("{}")
        (d / "new.jsonl").write_text("{}")
        os.utime(d / "old.jsonl", (0, 0))
        assert thinking.sessions_in("/tmp/p") == ["new", "old"]

    def test_no_directory_is_no_sessions(self, home, projects):
        assert thinking.sessions_in("") == []
        assert thinking.sessions_in("/tmp/absent") == []

    def test_an_unreadable_project_is_no_sessions(self, home, projects, monkeypatch):
        d = projects / thinking._slug("/tmp/p")
        d.mkdir(parents=True)
        monkeypatch.setattr(thinking.Path, "glob", _raise_oserror)
        assert thinking.sessions_in("/tmp/p") == []


class TestAgentsLive:
    """Who is writing right now, inferred from the transcripts they touch."""

    def test_a_fresh_transcript_is_a_live_agent(self, home, projects):
        agent_file(projects, "-tmp-p", "s1")
        assert thinking.agents_live("s1") == ["agent-one"]
        assert thinking.agents_running("s1") == 1

    def test_the_description_beside_it_names_the_agent(self, home, projects):
        p = agent_file(projects, "-tmp-p", "s1")
        p.with_suffix(".meta.json").write_text(json.dumps({"description": "Hunting a bug"}))
        assert thinking.agents_live("s1") == ["Hunting a bug"]

    def test_a_corrupt_description_falls_back_to_the_filename(self, home, projects):
        p = agent_file(projects, "-tmp-p", "s1")
        p.with_suffix(".meta.json").write_text("{ not json")
        assert thinking.agents_live("s1") == ["agent-one"]

    def test_another_session_is_not_counted(self, home, projects):
        agent_file(projects, "-tmp-p", "s1")
        assert thinking.agents_live("s2") == []

    def test_the_project_answers_when_no_session_is_named(self, home, projects):
        agent_file(projects, thinking._slug("/tmp/p"), "s1")
        assert thinking.agents_live(cwd="/tmp/p") == ["agent-one"]

    def test_every_session_answers_when_nothing_is_named(self, home, projects):
        agent_file(projects, "-tmp-p", "s1")
        assert thinking.agents_running() == 1

    def test_a_long_dead_transcript_is_not_alive(self, home, projects):
        p = agent_file(projects, "-tmp-p", "s1")
        os.utime(p, (0, 0))
        assert thinking.agents_live("s1") == []

    def test_a_quiet_agent_holding_a_tool_open_still_counts(self, home, projects, monkeypatch):
        p = agent_file(projects, "-tmp-p", "s1")
        p.write_text(json.dumps({"message": {"content": [{"type": "tool_use"}]}}))
        # Older than AGENT_FRESH, younger than AGENT_QUIET: the mid-tool rescue.
        now = os.stat(p).st_mtime
        os.utime(p, (now - 300, now - 300))
        assert thinking.agents_live("s1") == ["agent-one"]

    def test_a_transcript_that_vanished_is_skipped(self, home, projects):
        d = projects / "-tmp-p" / "s1" / "subagents"
        d.mkdir(parents=True)
        (d / "agent-gone.jsonl").symlink_to(d / "nowhere.jsonl")
        assert thinking.agents_live("s1") == []

    def test_an_unreadable_root_is_no_agents(self, monkeypatch):
        def boom(*a):
            raise OSError

        monkeypatch.setattr(thinking, "AGENT_ROOT", types.SimpleNamespace(glob=boom))
        assert thinking.agents_live("s1") == []


class TestSounds:
    """The tick is generated, not shipped: four styles, all short and soft."""

    @pytest.mark.parametrize("style", ["soft", "double", "low", "agents"])
    def test_every_style_makes_audible_samples(self, style):
        samples = thinking.make_tick(style)
        assert samples and max(abs(s) for s in samples) <= 1.0

    def test_the_agent_tick_falls_where_the_normal_one_is_flat(self):
        assert len(thinking.make_tick("agents")) > len(thinking.make_tick("soft"))

    def test_building_writes_both_wavs(self, home, capsys):
        thinking.build()
        assert thinking.TICK.exists() and thinking.TICK_AGENTS.exists()
        assert "agents" in capsys.readouterr().out

    def test_a_named_style_is_honoured(self, home):
        thinking.build("low")
        assert thinking.TICK.read_bytes()[:4] == b"RIFF"

    def test_the_demo_auditions_each_style(self, home, clock, monkeypatch, capsys):
        played = []
        monkeypatch.setattr(
            thinking, "subprocess", types.SimpleNamespace(run=lambda cmd, **kw: played.append(cmd))
        )
        thinking.demo()
        assert len(played) == 12  # four styles, three plays each
        assert "pick one" in capsys.readouterr().out


class TestQueueBusy:
    """The tick is ambience: it never queues behind the voice."""

    def test_an_idle_queue_is_not_busy(self, home):
        assert thinking._queue_busy() is False

    def test_an_unloadable_queue_module_is_not_busy(self, home, monkeypatch):
        monkeypatch.setattr(thinking, "HERE", home)
        assert thinking._queue_busy() is False


class TestRunLoop:
    """The loop itself, on a fake clock: what it plays and how it spaces it."""

    @pytest.fixture(autouse=True)
    def quiet(self, home, monkeypatch):
        self.played = []
        monkeypatch.setattr(
            thinking,
            "subprocess",
            types.SimpleNamespace(run=lambda cmd, **kw: self.played.append(cmd), DEVNULL=-3),
        )
        monkeypatch.setattr(thinking, "_queue_busy", lambda: False)
        monkeypatch.setattr(thinking._presence, "open_now", lambda: True)

    def _stop_after(self, monkeypatch, ticks: int):
        """Close the window after N ticks, which is how the loop normally ends."""
        left = [True] * ticks + [False]
        monkeypatch.setattr(thinking._presence, "open_now", lambda: left.pop(0))

    def test_it_builds_the_ticks_it_is_missing(self, home, clock, monkeypatch):
        monkeypatch.setattr(thinking, "agents_running", lambda s: 0)
        self._stop_after(monkeypatch, 1)
        thinking.run()
        assert thinking.TICK.exists()

    def test_it_leaves_the_ticks_it_already_has(self, home, clock, monkeypatch, capsys):
        thinking.build()
        capsys.readouterr()
        monkeypatch.setattr(thinking, "agents_running", lambda s: 0)
        self._stop_after(monkeypatch, 1)
        thinking.run()
        assert capsys.readouterr().out == ""  # nothing was rebuilt

    def test_the_first_tick_waits_out_the_delay(self, home, clock, monkeypatch):
        thinking.TUNE.write_text(json.dumps({"delay": 1.5, "interval": 2.0}))
        monkeypatch.setattr(thinking, "agents_running", lambda s: 0)
        self._stop_after(monkeypatch, 2)
        thinking.run()
        assert clock.slept == [1.5, 2.0, 2.0]
        assert all(str(thinking.TICK) in cmd for cmd in self.played)

    def test_a_closed_window_stops_it_mid_loop(self, home, clock, monkeypatch):
        monkeypatch.setattr(thinking, "agents_running", lambda s: 0)
        self._stop_after(monkeypatch, 0)
        thinking.run()
        assert self.played == []

    def test_a_busy_queue_skips_the_tick(self, home, clock, monkeypatch):
        monkeypatch.setattr(thinking, "agents_running", lambda s: 0)
        monkeypatch.setattr(thinking, "_queue_busy", lambda: True)
        self._stop_after(monkeypatch, 2)
        thinking.run()
        assert self.played == []

    def test_agents_get_their_own_sound_and_no_decay(self, home, clock, monkeypatch):
        monkeypatch.setattr(thinking, "agents_running", lambda s: 2)
        monkeypatch.setattr(thinking, "AGENT_MAX_RUN", 5)
        thinking.run("s1")
        assert [c[-1] for c in self.played] == [str(thinking.TICK_AGENTS)] * 2
        assert clock.slept[1:] == [thinking.AGENT_INTERVAL] * 2

    def test_a_long_turn_stops_nagging(self, home, clock, monkeypatch):
        # Past DECAY_AFTER the gap grows towards the cap instead of hammering.
        thinking.TUNE.write_text(json.dumps({"delay": 0, "interval": 50}))
        monkeypatch.setattr(thinking, "agents_running", lambda s: 0)
        self._stop_after(monkeypatch, 2)
        thinking.run()
        assert clock.slept == [0, 50, thinking.DECAY_MAX]

    def test_the_cap_shuts_a_dead_session_up(self, home, clock, monkeypatch):
        thinking.TUNE.write_text(json.dumps({"delay": 0, "interval": 200}))
        monkeypatch.setattr(thinking, "agents_running", lambda s: 0)
        thinking.run()
        assert len(self.played) == 1  # one tick, then past MAX_RUN


class TestMain:
    """The flags, each of which is somebody's entry point."""

    @pytest.fixture(autouse=True)
    def argv(self, monkeypatch):
        monkeypatch.setattr(thinking.sys, "argv", ["thinking.py"])

    def _run(self, monkeypatch, *args):
        monkeypatch.setattr(thinking.sys, "argv", ["thinking.py", *args])
        return thinking.main()

    def test_build_regenerates_the_sounds(self, home, monkeypatch, capsys):
        assert self._run(monkeypatch, "--build", "double") == 0
        assert thinking.TICK.exists()

    def test_demo_auditions_them(self, home, clock, monkeypatch, capsys):
        monkeypatch.setattr(thinking, "subprocess", types.SimpleNamespace(run=lambda *a, **k: None))
        assert self._run(monkeypatch, "--demo") == 0

    def test_tune_writes_the_timing(self, home, monkeypatch, capsys):
        assert self._run(monkeypatch, "--tune", "0.5", "3.0") == 0
        assert thinking.timing() == (0.5, 3.0)

    def test_tune_without_both_numbers_is_an_unknown_flag(self, home, monkeypatch, capsys):
        assert self._run(monkeypatch, "--tune", "0.5") == 2

    def test_agents_lists_them(self, home, projects, monkeypatch, capsys):
        p = agent_file(projects, "-tmp-p", "s1")
        p.with_suffix(".meta.json").write_text(json.dumps({"description": "Reading files"}))
        assert self._run(monkeypatch, "--agents", "s1") == 0
        assert "Reading files" in capsys.readouterr().out

    def test_agents_without_a_session_covers_every_window(
        self, home, projects, capsys, monkeypatch
    ):
        assert self._run(monkeypatch, "--agents") == 0
        assert "all" in capsys.readouterr().out

    def test_whose_names_the_session_in_a_pane(self, home, projects, monkeypatch, capsys):
        thinking.bind("s-99", pane="%2", cwd="/tmp/p")
        assert self._run(monkeypatch, "--whose", "/tmp/p", "a title", "%2") == 0
        assert "s-99" in capsys.readouterr().out

    def test_whose_says_so_when_it_cannot_tell(self, home, projects, monkeypatch, capsys):
        assert self._run(monkeypatch, "--whose", "/tmp/p", "a title") == 0
        assert "unidentified" in capsys.readouterr().out

    def test_bind_reads_the_session_off_stdin(self, home, monkeypatch, feed_stdin, hook_payload):
        monkeypatch.setenv("TMUX_PANE", "%5")
        feed_stdin(hook_payload(session_id="s-bind", cwd="/tmp/p"))
        assert self._run(monkeypatch, "--bind") == 0
        assert thinking.bound_session("%5") == "s-bind"

    def test_a_malformed_bind_event_is_ignored(self, home, monkeypatch, feed_stdin):
        feed_stdin("{ not json")
        assert self._run(monkeypatch, "--bind") == 0
        assert thinking.pane_files() == []

    def test_panes_lists_the_bindings(self, home, monkeypatch, capsys):
        thinking.bind("session-abcdef", pane="%2", cwd="/tmp/p")
        assert self._run(monkeypatch, "--panes") == 0
        assert "/tmp/p" in capsys.readouterr().out

    def test_panes_skips_a_corrupt_binding(self, home, monkeypatch, capsys):
        (home / "pane-9.json").write_text("{ not json")
        assert self._run(monkeypatch, "--panes") == 0
        assert capsys.readouterr().out.strip() == ""

    def test_panes_says_so_when_nothing_is_bound(self, home, monkeypatch, capsys):
        assert self._run(monkeypatch, "--panes") == 0
        assert "no pane is bound" in capsys.readouterr().out

    def test_show_prints_the_timing(self, home, projects, monkeypatch, capsys):
        assert self._run(monkeypatch, "--show") == 0
        assert "first tick" in capsys.readouterr().out

    def test_an_unknown_flag_says_so_rather_than_ticking(self, home, monkeypatch, capsys):
        assert self._run(monkeypatch, "--nonsense") == 2
        assert "unknown flag" in capsys.readouterr().out

    def test_no_flag_runs_the_loop(self, home, monkeypatch):
        seen = []
        monkeypatch.setattr(thinking, "run", lambda sid="": seen.append(sid))
        assert self._run(monkeypatch) == 0
        assert seen == [""]

    def test_session_binds_the_loop_to_one_window(self, home, monkeypatch):
        seen = []
        monkeypatch.setattr(thinking, "run", lambda sid="": seen.append(sid))
        assert self._run(monkeypatch, "--session", "s-7") == 0
        assert seen == ["s-7"]

    def test_ctrl_c_ends_the_loop_quietly(self, home, monkeypatch):
        def interrupted(sid=""):
            raise KeyboardInterrupt

        monkeypatch.setattr(thinking, "run", interrupted)
        assert self._run(monkeypatch) == 0
