"""The contextual acknowledgement, with the API replaced by a stub.

Not one test here may reach the network. ``anthropic`` is swapped out in
``sys.modules`` before ``_client`` imports it, so even a test that forgot to
patch the credentials would build a fake client rather than a real one -- and
the harness has already stripped ``ANTHROPIC_API_KEY`` besides.
"""

import json
import sys
import types

import pytest

import claude_voice.ack as ack


class Cfg:
    """The handful of ``ack.*`` keys this module reads, and nothing else."""

    def __init__(self, **over):
        self._d = {k.replace("__", "."): v for k, v in over.items()}

    def get(self, dotted, default=None):
        return self._d.get(dotted, default)


class Block:
    def __init__(self, text, type="text"):
        self.text = text
        self.type = type


class Response:
    def __init__(self, *blocks):
        self.content = list(blocks)


class FakeAnthropic:
    """One call, recorded. Constructed by the stub module, never by the SDK."""

    calls = []

    def __init__(self, **kwargs):
        FakeAnthropic.calls.append(kwargs)
        self.kwargs = kwargs
        self.messages = types.SimpleNamespace(create=self._create)
        FakeAnthropic.last = self

    def _create(self, **kwargs):
        self.request = kwargs
        FakeAnthropic.request = kwargs
        if isinstance(FakeAnthropic.reply, Exception):
            raise FakeAnthropic.reply
        return FakeAnthropic.reply


@pytest.fixture(autouse=True)
def anthropic_stub(monkeypatch):
    """A module named ``anthropic`` that cannot talk to anything."""
    FakeAnthropic.calls = []
    FakeAnthropic.reply = Response(Block("Checking the disk space."))
    FakeAnthropic.request = None
    mod = types.ModuleType("anthropic")
    mod.Anthropic = FakeAnthropic
    monkeypatch.setitem(sys.modules, "anthropic", mod)
    return mod


@pytest.fixture
def cfg(monkeypatch):
    """Replace the configuration the module froze at import time."""

    def _set(**over):
        c = Cfg(**over)
        monkeypatch.setattr(ack, "CFG", c)
        return c

    return _set


@pytest.fixture
def oauth(home, monkeypatch):
    """A Claude Code login on disk, in place of the developer's own."""

    def _write(token="oauth-token"):
        fake_home = home / "fakehome"
        creds = fake_home / ".claude"
        creds.mkdir(parents=True, exist_ok=True)
        payload = {"claudeAiOauth": {"accessToken": token}} if token else {"claudeAiOauth": {}}
        (creds / ".credentials.json").write_text(json.dumps(payload))
        monkeypatch.setattr(ack.Path, "home", staticmethod(lambda: fake_home))
        return fake_home

    return _write


def stub_mods(monkeypatch, **fakes):
    """Swap the modules ack loads by path; anything unnamed stays real."""
    real = ack._mod
    monkeypatch.setattr(ack, "_mod", lambda name: fakes[name] if name in fakes else real(name))


def spokenlog(entries):
    return types.SimpleNamespace(tail=lambda n, session: list(entries))


class TestClient:
    """Whatever credential this machine has, and nothing it does not."""

    def test_the_api_key_is_used_first(self, cfg, monkeypatch, oauth):
        cfg(ack__timeout=1.25)
        oauth("would-not-be-reached")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        client = ack._client()
        assert client.kwargs["api_key"] == "sk-test"
        assert "auth_token" not in client.kwargs

    def test_the_timeout_is_the_configured_one(self, cfg, monkeypatch):
        cfg(ack__timeout=0.5)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        assert ack._client().kwargs == {"api_key": "sk-test", "timeout": 0.5, "max_retries": 0}

    def test_the_login_on_disk_is_the_fallback(self, cfg, oauth):
        cfg()
        oauth("oauth-abc")
        client = ack._client()
        assert client.kwargs["auth_token"] == "oauth-abc"
        assert client.kwargs["max_retries"] == 0  # a retry costs more than silence

    def test_a_login_without_a_token_is_no_client(self, cfg, oauth):
        cfg()
        oauth(token="")
        assert ack._client() is None

    def test_no_credential_at_all_raises_into_the_caller(self, cfg, home, monkeypatch):
        cfg()
        monkeypatch.setattr(ack.Path, "home", staticmethod(lambda: home / "nothing-here"))
        with pytest.raises(OSError):
            ack._client()
        assert ack.contextual("do a thing") == ""  # and contextual absorbs it


class TestBare:
    """A line reduced to what was said, so punctuation is not a difference."""

    def test_punctuation_and_case_fall_away(self):
        assert ack._bare("Do it, please!") == ack._bare("do it please")

    def test_an_empty_line_reduces_to_nothing(self):
        assert ack._bare("  ...  ") == ""


class TestDropPrompt:
    """A dictated prompt is already in the log; it must not be sent twice."""

    def test_the_trailing_copy_of_the_prompt_goes(self):
        entries = [
            {"side": "out", "text": "Reading the config."},
            {"side": "in", "text": "check the disk"},
        ]
        assert ack._drop_prompt(entries, "Check the disk.") == entries[:1]

    def test_a_typed_prompt_was_never_there(self):
        entries = [{"side": "in", "text": "something else"}]
        assert ack._drop_prompt(entries, "check the disk") == entries

    def test_only_the_trailing_in_lines_are_candidates(self):
        entries = [
            {"side": "in", "text": "check the disk"},
            {"side": "out", "text": "Four percent."},
        ]
        assert ack._drop_prompt(entries, "check the disk") == entries

    def test_an_empty_line_never_matches(self):
        entries = [{"side": "in", "text": "   "}]
        assert ack._drop_prompt(entries, "   ") == entries


class TestHistory:
    """The last few turns of the spoken log, shaped into a conversation."""

    def test_context_zero_reads_nothing(self, cfg, monkeypatch):
        cfg(ack__context=0)
        stub_mods(monkeypatch, spokenlog=spokenlog([{"side": "in", "text": "hi"}]))
        assert ack.history("prompt", "s1") == []

    def test_an_unnamed_session_reads_nothing(self, cfg, monkeypatch):
        cfg(ack__context=6)
        assert ack.history("prompt", "") == []

    def test_an_unreadable_count_falls_back_to_six(self, cfg, monkeypatch):
        cfg(ack__context="lots")
        entries = [{"side": "in", "text": f"line {i}"} for i in range(20)]
        stub_mods(monkeypatch, spokenlog=spokenlog(entries))
        assert len(ack.history("prompt", "s1")) == 1  # all collapsed, then capped

    def test_a_broken_spoken_log_is_no_history(self, cfg, monkeypatch):
        cfg(ack__context=6)

        def boom(n, session):
            raise OSError("gone")

        stub_mods(monkeypatch, spokenlog=types.SimpleNamespace(tail=boom))
        assert ack.history("prompt", "s1") == []

    def test_consecutive_lines_from_one_side_become_one_turn(self, cfg, monkeypatch):
        cfg(ack__context=6)
        stub_mods(
            monkeypatch,
            spokenlog=spokenlog(
                [
                    {"side": "in", "text": "run the tests"},
                    {"side": "out", "text": "Running them."},
                    {"side": "out", "text": "Done, the tests pass."},
                ]
            ),
        )
        assert ack.history("next", "s1") == [
            {"role": "user", "content": "run the tests"},
            {"role": "assistant", "content": "Running them. Done, the tests pass."},
        ]

    def test_blank_lines_contribute_nothing(self, cfg, monkeypatch):
        cfg(ack__context=6)
        stub_mods(
            monkeypatch,
            spokenlog=spokenlog(
                [{"side": "in", "text": "  "}, {"side": "in", "text": "run the tests"}]
            ),
        )
        assert ack.history("next", "s1") == [{"role": "user", "content": "run the tests"}]

    def test_a_monologue_contributes_its_opening_only(self, cfg, monkeypatch):
        cfg(ack__context=6)
        stub_mods(monkeypatch, spokenlog=spokenlog([{"side": "in", "text": "x" * 900}]))
        assert ack.history("next", "s1")[0]["content"] == "x" * ack.MAX_LINE

    def test_a_collapsed_turn_is_capped_too(self, cfg, monkeypatch):
        cfg(ack__context=6)
        line = {"side": "out", "text": "y" * 300}
        entries = [{"side": "in", "text": "go"}, *[line] * 6]
        stub_mods(monkeypatch, spokenlog=spokenlog(entries))
        msgs = ack.history("next", "s1")
        assert len(msgs[-1]["content"]) == ack.MAX_LINE * 3

    def test_the_conversation_starts_on_your_side(self, cfg, monkeypatch):
        # Trimming to the last n turns can cut into the middle of an exchange
        # and leave an answer with no question above it.
        cfg(ack__context=3)
        stub_mods(
            monkeypatch,
            spokenlog=spokenlog(
                [
                    {"side": "out", "text": "An orphan answer."},
                    {"side": "in", "text": "and then this"},
                    {"side": "out", "text": "The reply to it."},
                ]
            ),
        )
        assert [m["role"] for m in ack.history("next", "s1")] == ["user", "assistant"]

    def test_a_history_of_answers_alone_is_dropped_entirely(self, cfg, monkeypatch):
        cfg(ack__context=6)
        stub_mods(monkeypatch, spokenlog=spokenlog([{"side": "out", "text": "Only an answer."}]))
        assert ack.history("next", "s1") == []

    def test_the_prompt_being_acknowledged_is_removed(self, cfg, monkeypatch):
        cfg(ack__context=6)
        stub_mods(
            monkeypatch,
            spokenlog=spokenlog(
                [
                    {"side": "in", "text": "first thing"},
                    {"side": "out", "text": "Done."},
                    {"side": "in", "text": "check the disk"},
                ]
            ),
        )
        assert ack.history("Check the disk!", "s1") == [
            {"role": "user", "content": "first thing"},
            {"role": "assistant", "content": "Done."},
        ]


class TestContextual:
    """What the model is asked, and what is done with what it answers."""

    @pytest.fixture(autouse=True)
    def key(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")

    def test_switched_off_it_never_asks(self, cfg):
        cfg(ack__contextual=False)
        assert ack.contextual("anything") == ""
        assert FakeAnthropic.calls == []

    def test_without_a_credential_it_never_asks(self, cfg, oauth, monkeypatch):
        cfg()
        monkeypatch.delenv("ANTHROPIC_API_KEY")
        oauth(token="")
        assert ack.contextual("anything") == ""

    def test_the_line_comes_back_stripped(self, cfg):
        cfg()
        FakeAnthropic.reply = Response(Block('  "Checking the\ndisk space." '))
        assert ack.contextual("check the disk") == "Checking the disk space."

    def test_only_the_text_blocks_are_read(self, cfg):
        cfg()
        FakeAnthropic.reply = Response(Block("ignored", type="thinking"), Block("On it."))
        assert ack.contextual("do a thing") == "On it."

    def test_a_quick_prompt_is_declined_rather_than_acknowledged(self, cfg):
        cfg()
        for word in ("SILENT", "silent", "Silencio"):
            FakeAnthropic.reply = Response(Block(word))
            assert ack.contextual("hello, how are you") == ack.SILENT

    def test_an_empty_answer_is_a_failure(self, cfg):
        cfg()
        FakeAnthropic.reply = Response(Block("   "))
        assert ack.contextual("do a thing") == ""

    def test_a_long_answer_stops_being_an_acknowledgement(self, cfg):
        cfg(ack__max_words=3)
        FakeAnthropic.reply = Response(Block("one two three four five six seven eight"))
        assert ack.contextual("do a thing") == ""

    def test_an_api_failure_is_an_empty_line_not_a_decline(self, cfg):
        # The difference matters: this falls back to the cached phrase, a
        # decline plays nothing at all.
        cfg()
        FakeAnthropic.reply = TimeoutError("timed out")
        assert ack.contextual("do a thing") == ""

    def test_the_prompt_is_truncated_before_it_is_sent(self, cfg):
        cfg()
        ack.contextual("z" * 4000)
        assert FakeAnthropic.request["messages"][-1]["content"] == "z" * 1500

    def test_the_model_and_the_ceiling_come_from_the_config(self, cfg):
        cfg(ack__model="claude-test-1")
        ack.contextual("do a thing")
        assert FakeAnthropic.request["model"] == "claude-test-1"
        assert FakeAnthropic.request["max_tokens"] == 40

    def test_the_history_is_sent_ahead_of_the_prompt(self, cfg, monkeypatch):
        cfg(ack__context=6, ack__context_system="That history is what was SPOKEN.")
        stub_mods(
            monkeypatch,
            spokenlog=spokenlog(
                [
                    {"side": "in", "text": "run the tests"},
                    {"side": "out", "text": "Done, the tests pass."},
                ]
            ),
        )
        ack.contextual("now the linter", "s1")
        assert FakeAnthropic.request["messages"] == [
            {"role": "user", "content": "run the tests"},
            {"role": "assistant", "content": "Done, the tests pass."},
            {"role": "user", "content": "now the linter"},
        ]
        assert "SPOKEN" in FakeAnthropic.request["system"]

    def test_a_turn_that_never_spoke_merges_into_the_prompt(self, cfg, monkeypatch):
        # Two user messages in a row is not a conversation the API will take.
        cfg(ack__context=6)
        stub_mods(monkeypatch, spokenlog=spokenlog([{"side": "in", "text": "the turn before"}]))
        ack.contextual("and now this", "s1")
        assert FakeAnthropic.request["messages"] == [
            {"role": "user", "content": "the turn before\nand now this"}
        ]

    def test_the_quick_instruction_is_appended_when_asked_for(self, cfg):
        cfg(ack__system="Be brief.", ack__skip_quick=True, ack__quick_system="Say SILENT if quick.")
        ack.contextual("hello")
        assert FakeAnthropic.request["system"] == "Be brief.\n\nSay SILENT if quick."

    def test_an_empty_quick_instruction_leaves_the_system_alone(self, cfg):
        cfg(ack__system="Be brief.", ack__skip_quick=True, ack__quick_system="  ")
        ack.contextual("hello")
        assert FakeAnthropic.request["system"] == "Be brief."

    def test_skip_quick_off_asks_for_no_decline(self, cfg):
        cfg(ack__system="Be brief.", ack__skip_quick=False, ack__quick_system="Say SILENT.")
        ack.contextual("hello")
        assert FakeAnthropic.request["system"] == "Be brief."

    def test_an_empty_history_note_leaves_the_system_alone(self, cfg, monkeypatch):
        cfg(ack__context=6, ack__system="Be brief.", ack__context_system="")
        stub_mods(monkeypatch, spokenlog=spokenlog([{"side": "in", "text": "earlier"}]))
        ack.contextual("now this", "s1")
        assert FakeAnthropic.request["system"] == "Be brief."


class TestGeneric:
    """The cached phrase: the net under everything above."""

    @pytest.fixture
    def acks(self, home, monkeypatch):
        monkeypatch.setattr(ack, "tempfile", types.SimpleNamespace(gettempdir=lambda: str(home)))

        def _make(*names):
            d = home / "acks" / "en"
            d.mkdir(parents=True, exist_ok=True)
            for n in names:
                (d / n).write_bytes(b"RIFF")
            return d

        return _make

    def test_an_empty_cache_has_nothing_to_offer(self, home, acks):
        acks()
        assert ack.generic() == (None, "")

    def test_it_returns_a_copy_and_what_it_says(self, home, acks):
        acks("ack00.wav")
        wav, text = ack.generic()
        assert wav.read_bytes() == b"RIFF"
        assert text == "One moment."  # the phrase at that index, for the log
        assert (home / "acks" / "en" / "ack00.wav").exists()  # the original survives

    def test_it_does_not_repeat_itself(self, home, acks):
        acks("ack00.wav", "ack01.wav")
        (home / "last-ack").write_text("ack00.wav")
        _, text = ack.generic()
        assert text == "On it."
        assert (home / "last-ack").read_text() == "ack01.wav"

    def test_one_cached_phrase_repeats_rather_than_going_silent(self, home, acks):
        acks("ack00.wav")
        (home / "last-ack").write_text("ack00.wav")
        wav, _ = ack.generic()
        assert wav is not None

    def test_an_unwritable_marker_does_not_stop_it(self, home, acks, monkeypatch):
        acks("ack00.wav")
        monkeypatch.setattr(ack, "BASE", home / "gone" / "deeper")
        wav, text = ack.generic()
        assert wav is not None and text == "One moment."


class TestMain:
    """The entry point voice.py spawns, and the dry run that measures it."""

    @pytest.fixture(autouse=True)
    def wiring(self, home, monkeypatch, cfg):
        cfg()
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        monkeypatch.setattr(ack, "tempfile", types.SimpleNamespace(gettempdir=lambda: str(home)))
        self.queued = []
        self.audioq = types.SimpleNamespace(
            enqueue=lambda wav, text="", **kw: self.queued.append((wav, text, kw))
        )
        self.synthesized = []

        def synthesize(text, path, cfg=None):
            self.synthesized.append(text)
            path.write_bytes(b"RIFF")
            return self.ok

        self.ok = True
        self.speak = types.SimpleNamespace(synthesize=synthesize)

    def _run(self, monkeypatch, *args, **fakes):
        monkeypatch.setattr(ack.sys, "argv", ["ack.py", *args])
        stub_mods(monkeypatch, audioq=self.audioq, speak=self.speak, **fakes)
        return ack.main()

    def test_the_line_is_synthesized_and_queued_under_its_session(self, monkeypatch):
        FakeAnthropic.reply = Response(Block("Checking the disk space."))
        assert self._run(monkeypatch, "--session", "s-1", "check", "the", "disk") == 0
        assert self.synthesized == ["Checking the disk space."]
        [(wav, text, kw)] = self.queued
        assert text == "Checking the disk space." and kw["session"] == "s-1"
        assert wav.read_bytes() == b"RIFF"

    def test_a_declined_prompt_makes_no_sound_at_all(self, monkeypatch):
        FakeAnthropic.reply = Response(Block("SILENT"))
        assert self._run(monkeypatch, "hello there") == 0
        assert self.queued == []

    def test_a_failed_call_falls_back_to_the_cached_phrase(self, home, monkeypatch):
        FakeAnthropic.reply = RuntimeError("no")
        d = home / "acks" / "en"
        d.mkdir(parents=True)
        (d / "ack00.wav").write_bytes(b"RIFF")
        assert self._run(monkeypatch, "do a thing") == 0
        assert [t for _, t, _ in self.queued] == ["One moment."]

    def test_a_failed_synthesis_falls_back_the_same_way(self, home, monkeypatch):
        self.ok = False
        d = home / "acks" / "en"
        d.mkdir(parents=True)
        (d / "ack00.wav").write_bytes(b"RIFF")
        assert self._run(monkeypatch, "do a thing") == 0
        assert [t for _, t, _ in self.queued] == ["One moment."]

    def test_no_cache_and_no_line_is_simply_silence(self, monkeypatch):
        FakeAnthropic.reply = RuntimeError("no")
        assert self._run(monkeypatch, "do a thing") == 0
        assert self.queued == []

    def test_an_empty_prompt_is_never_put_to_the_model(self, monkeypatch):
        assert self._run(monkeypatch) == 0
        assert FakeAnthropic.calls == []

    def test_the_dry_run_prints_the_line_and_what_it_cost(self, monkeypatch, capsys):
        FakeAnthropic.reply = Response(Block("Checking the disk space."))
        assert self._run(monkeypatch, "--dry-run", "--session", "s-1", "check the disk") == 0
        out = capsys.readouterr().out
        assert "Checking the disk space." in out and "ms," in out and "session s-1" in out
        assert self.queued == []

    def test_the_dry_run_says_when_nothing_would_be_said(self, monkeypatch, capsys):
        FakeAnthropic.reply = RuntimeError("no")
        assert self._run(monkeypatch, "--dry-run", "--session", "s-1", "do a thing") == 0
        assert "the cached phrase would play" in capsys.readouterr().out

    def test_the_dry_run_marks_a_declined_prompt(self, monkeypatch, capsys):
        FakeAnthropic.reply = Response(Block("SILENT"))
        assert self._run(monkeypatch, "--dry-run", "--session", "s-1", "hello") == 0
        assert "silent" in capsys.readouterr().out

    def test_the_dry_run_asks_the_reader_policy_which_conversation(self, monkeypatch, capsys):
        log = types.SimpleNamespace(
            tail=lambda n, session: [],
            target=lambda: ("", ""),
            follow=lambda session, cwd: "",
        )
        assert self._run(monkeypatch, "--dry-run", "do a thing", spokenlog=log) == 0
        assert "no conversation to read" in capsys.readouterr().out
