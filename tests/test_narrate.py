"""Mid-turn narration: finding the prose, and stripping it down to speech.

Everything a screen can show and a speaker cannot -- code, tables, hashes, the
path of a file -- has to be gone before the text reaches the synthesizer, and
what is left has to still be a sentence.
"""

import hashlib
import json
import wave

import pytest

import claude_voice.config as config
import claude_voice.narrate as narrate


@pytest.fixture
def with_cfg(monkeypatch):
    """Swap the configuration the module captured at import time.

    `CFG` is read once when the module loads, because this runs as a hook and
    lives for a fraction of a second.
    """

    def _set(**values):
        cfg = config.Config({"narrate": values})
        monkeypatch.setattr(narrate, "CFG", cfg)
        return cfg

    return _set


@pytest.fixture
def fake_speak(monkeypatch, home):
    """A synthesizer that writes a real WAV without a voice model on disk.

    `main` measures the audio to know when the notice will finish playing, so
    the file has to be a genuine WAV; nothing here needs it to contain sound.
    """

    class _Speak:
        allowed = True
        audio = True
        synthesized = []

        @classmethod
        def enabled(cls, session_id):
            return cls.allowed

        @classmethod
        def audio_available(cls):
            return cls.audio

        @classmethod
        def synthesize(cls, text, path):
            cls.synthesized.append((text, path))
            if not cls.allowed:
                return False
            with wave.open(str(path), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(16000)
                w.writeframes(b"\x00\x00" * 8000)  # half a second
            return True

    class _Audioq:
        queued = []

        @classmethod
        def enqueue(cls, path, text, session=""):
            cls.queued.append((path, text, session))

    monkeypatch.setattr(narrate, "_mod", lambda name: {"speak": _Speak, "audioq": _Audioq}[name])
    # The scratch WAV is written to the system temp directory; keep it inside
    # the disposable home instead.
    monkeypatch.setattr(narrate.tempfile, "gettempdir", lambda: str(home))
    return _Speak, _Audioq


class TestCfg:
    """Runtime tuning wins over the config file, which wins over defaults."""

    def test_the_config_file_supplies_both_numbers(self, with_cfg):
        with_cfg(word_limit=25, max_per_turn=3)
        assert narrate.cfg() == (25, 3)

    def test_the_tuned_file_wins(self, with_cfg):
        with_cfg(word_limit=25, max_per_turn=3)
        narrate.TUNE.write_text(json.dumps({"word_limit": 8, "max_per_turn": 1}))
        assert narrate.cfg() == (8, 1)

    def test_a_half_written_tuning_falls_through_key_by_key(self, with_cfg):
        with_cfg(word_limit=25, max_per_turn=3)
        narrate.TUNE.write_text(json.dumps({"word_limit": 8}))
        assert narrate.cfg() == (8, 3)

    def test_a_corrupt_tuning_file_is_ignored(self, with_cfg):
        with_cfg(word_limit=25, max_per_turn=3)
        narrate.TUNE.write_text("{ not json")
        assert narrate.cfg() == (25, 3)


class TestTune:
    """`--tune`, which changes the numbers without touching the config file."""

    def test_it_writes_what_cfg_reads_back(self, capsys):
        narrate.tune(30, 5)
        assert narrate.cfg() == (30, 5)
        assert "30 words" in capsys.readouterr().out


class TestFindText:
    """The MessageDisplay payload is undocumented, so every name is tried."""

    @pytest.mark.parametrize(
        "key",
        [
            "message",
            "text",
            "content",
            "assistant_message",
            "display_text",
            "last_assistant_message",
        ],
    )
    def test_any_of_the_known_keys_carries_the_prose(self, key):
        assert narrate.find_text({key: "the notice"}) == "the notice"

    def test_a_wrapped_message_is_unwrapped(self):
        assert narrate.find_text({"message": {"content": "inside content"}}) == "inside content"
        assert narrate.find_text({"message": {"text": "inside text"}}) == "inside text"

    def test_an_empty_value_does_not_win_over_a_later_key(self):
        assert narrate.find_text({"message": "   ", "text": "the real one"}) == "the real one"

    def test_a_shape_nobody_expected_is_skipped(self):
        assert (
            narrate.find_text({"message": ["a", "list"], "text": "the real one"}) == "the real one"
        )

    def test_a_wrapped_message_with_nothing_in_it_is_skipped(self):
        assert narrate.find_text({"message": {"role": "assistant"}}) == ""

    def test_nothing_at_all_is_nothing(self):
        assert narrate.find_text({}) == ""

    def test_a_transcript_that_is_not_there_is_nothing(self, home):
        assert narrate.find_text({"transcript_path": str(home / "gone.jsonl")}) == ""

    def test_it_falls_back_to_the_last_assistant_block(self, home):
        tp = home / "transcript.jsonl"
        tp.write_text(
            json.dumps({"message": {"role": "assistant", "content": "an older line"}})
            + "\n"
            + json.dumps(
                {
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "the latest line"}],
                    }
                }
            )
            + "\n"
        )
        assert narrate.find_text({"transcript_path": str(tp)}) == "the latest line"

    def test_it_reads_backwards_past_tool_results_and_junk(self, home):
        tp = home / "transcript.jsonl"
        tp.write_text(
            json.dumps({"message": {"role": "assistant", "content": "the last prose"}})
            + "\n"
            + json.dumps(
                {"message": {"role": "assistant", "content": [{"type": "tool_use", "id": "1"}]}}
            )
            + "\n"
            + json.dumps({"message": {"role": "user", "content": "a reply"}})
            + "\n"
            + json.dumps({"no_message_at_all": True})
            + "\n"
            + "{ not json at all\n"
        )
        assert narrate.find_text({"transcript_path": str(tp)}) == "the last prose"

    def test_a_content_shape_that_is_neither_list_nor_text_is_skipped(self, home):
        tp = home / "transcript.jsonl"
        tp.write_text(
            json.dumps({"message": {"role": "assistant", "content": "the prose"}})
            + "\n"
            + json.dumps({"message": {"role": "assistant", "content": None}})
            + "\n"
        )
        assert narrate.find_text({"transcript_path": str(tp)}) == "the prose"

    def test_a_transcript_with_no_assistant_line_is_nothing(self, home):
        tp = home / "transcript.jsonl"
        tp.write_text(json.dumps({"message": {"role": "user", "content": "hello"}}) + "\n")
        assert narrate.find_text({"transcript_path": str(tp)}) == ""

    def test_a_transcript_that_cannot_be_opened_is_nothing(self, home):
        # A directory where a file was expected: it exists, and reading it
        # raises. The hook must not.
        (home / "transcript.jsonl").mkdir()
        assert narrate.find_text({"transcript_path": str(home / "transcript.jsonl")}) == ""

    def test_only_the_tail_of_a_huge_transcript_is_read(self, home):
        tp = home / "transcript.jsonl"
        filler = json.dumps({"message": {"role": "user", "content": "x" * 500}})
        with tp.open("w") as f:
            f.write(
                json.dumps({"message": {"role": "assistant", "content": "too far back"}}) + "\n"
            )
            for _ in range(600):
                f.write(filler + "\n")
        assert tp.stat().st_size > 200_000
        assert narrate.find_text({"transcript_path": str(tp)}) == ""


class TestSpeakableRemoves:
    """What must never reach the speaker."""

    def test_the_stop_hooks_own_block_is_left_alone(self):
        # The block carrying the marker is the end of the turn; speaking it
        # here would say the conclusion twice.
        assert narrate.speakable("Done.\n<!-- TTS: The tests pass. -->") == ""

    def test_nothing_is_nothing(self):
        assert narrate.speakable("") == ""

    def test_fenced_code_goes(self):
        text = "```python\nprint('hello')\n```\nThe script prints a greeting now."
        assert narrate.speakable(text) == "The script prints a greeting now."

    def test_inline_code_goes(self):
        assert "speakable" not in narrate.speakable("Fixed it in `speakable()` and moved on.")

    def test_html_comments_go(self):
        got = narrate.speakable("<!-- a note to nobody -->The change landed on main.")
        assert got == "The change landed on main."

    def test_whole_table_rows_go(self):
        # Reading a table aloud column by column is incomprehensible.
        text = "| name | value |\n| a | 1 |\nThe table lists both of the columns."
        assert narrate.speakable(text) == "The table lists both of the columns."

    def test_list_bullets_and_quote_markers_go(self):
        assert narrate.speakable("- the first item of the list") == "the first item of the list"
        assert narrate.speakable("> a quoted line from earlier") == "a quoted line from earlier"
        assert narrate.speakable("### A heading of some kind") == "A heading of some kind"
        assert narrate.speakable("1. the numbered item here") == "the numbered item here"

    def test_a_link_keeps_its_words_and_loses_its_target(self):
        got = narrate.speakable("Read the [release notes](https://example.com/notes) first.")
        assert got == "Read the release notes first."

    def test_emphasis_markers_go(self):
        got = narrate.speakable("**Bold** and _italic_ and ~struck~ words survive.")
        assert got == "Bold and italic and struck words survive."

    def test_a_path_collapses_to_its_filename(self):
        # Deleting paths outright left broken sentences; the basename informs
        # and reads out loud fine.
        got = narrate.speakable("Edited claude_voice/narrate.py and the tests pass.")
        assert got == "Edited narrate.py and the tests pass."

    def test_a_url_loses_its_route(self):
        got = narrate.speakable("See https://example.com/docs/setup for the rest of it.")
        assert "example.com" not in got and "docs" not in got

    def test_a_hash_goes_and_takes_no_punctuation_with_it(self):
        got = narrate.speakable("The commit 4f2a9c1b8d3e landed, and nothing broke.")
        assert got == "The commit landed, and nothing broke."

    def test_a_trailing_colon_goes(self):
        # "Let me check the config:" introduces something we are not going to
        # read, and the sentence is useful without it.
        assert narrate.speakable("Let me check the config:") == "Let me check the config"

    def test_too_few_words_is_not_worth_saying(self):
        for noise in ("Ok.", "Done:", "Sure"):
            assert narrate.speakable(noise) == ""


class TestSpeakableLength:
    """Short is spoken whole; long becomes a lead-in."""

    def test_a_short_notice_is_spoken_whole(self, with_cfg):
        with_cfg(word_limit=50)
        text = "Committed that, and now I will check the tests."
        assert narrate.speakable(text) == text

    def test_a_long_one_is_cut_to_its_first_two_sentences(self, with_cfg):
        with_cfg(word_limit=5)
        text = "First sentence here. Second sentence here. " + " ".join(["filler"] * 40)
        assert narrate.speakable(text) == "First sentence here. Second sentence here."

    def test_a_long_one_with_no_sentence_end_is_capped_by_characters(self, with_cfg):
        with_cfg(word_limit=5)
        got = narrate.speakable(" ".join(["filler"] * 200))
        assert len(got) <= 400

    def test_a_lead_in_too_short_to_be_useful_falls_back_to_words(self, with_cfg):
        # Two tiny sentences would be a lead-in of nothing at all, so the first
        # forty words are spoken instead.
        with_cfg(word_limit=5)
        got = narrate.speakable("Hi. Ok. " + " ".join(["filler"] * 60))
        assert got.startswith("Hi. Ok. filler")
        assert len(got.split()) == 40


class TestSiblingModules:
    """The speaker and the queue, loaded by file rather than imported."""

    def test_a_sibling_loads_by_file(self):
        # Late and by path, so a hook that ends up saying nothing has not paid
        # to import the synthesizer.
        assert narrate._mod("turn").safe_session("a/b") == "ab"


class TestTurnState:
    """One state file per prompt, so the counters reset with the turn."""

    def test_it_is_named_for_the_prompt(self, home):
        p = narrate.turn_state("prompt-1")
        assert p.parent == home
        assert p.name.startswith("narr-") and p.name.endswith(".json")

    def test_two_prompts_do_not_share_a_file(self):
        assert narrate.turn_state("a") != narrate.turn_state("b")

    def test_no_prompt_id_still_has_a_file(self):
        assert narrate.turn_state("").name.startswith("narr-")


class TestMain:
    """The hook itself: every gate that can end the turn without a sound."""

    def _payload(self, text="Committed that, and now the tests are running."):
        return {"session_id": "s1", "prompt_id": "p1", "message": text}

    def test_it_speaks_a_notice_and_remembers_it(self, feed_stdin, fake_speak, no_subprocess):
        speak, audioq = fake_speak
        feed_stdin(self._payload())
        assert narrate.main() == 0
        assert audioq.queued[-1][1] == "Committed that, and now the tests are running."
        assert audioq.queued[-1][2] == "s1"
        st = json.loads(narrate.turn_state("p1").read_text())
        assert st["n"] == 1
        assert len(st["said"]) == 1

    def test_disabled_narration_says_nothing(self, feed_stdin, with_cfg, fake_speak):
        with_cfg(enabled=False)
        feed_stdin(self._payload())
        assert narrate.main() == 0
        assert fake_speak[0].synthesized == []

    def test_a_notice_file_missing_its_keys_is_a_default(
        self, feed_stdin, fake_speak, no_subprocess
    ):
        # Valid JSON, interrupted mid-write. The recovery that exists for a
        # corrupt file did not exist for an incomplete one, and st["n"] was a
        # KeyError out of main().
        speak, audioq = fake_speak
        narrate.turn_state("p1").parent.mkdir(parents=True, exist_ok=True)
        narrate.turn_state("p1").write_text("{}")
        feed_stdin(self._payload())
        assert narrate.main() == 0
        assert len(audioq.queued) == 1

    def test_a_notice_file_that_is_not_an_object_is_a_default(
        self, feed_stdin, fake_speak, no_subprocess
    ):
        speak, audioq = fake_speak
        narrate.turn_state("p1").parent.mkdir(parents=True, exist_ok=True)
        narrate.turn_state("p1").write_text('"idle"')
        feed_stdin(self._payload())
        assert narrate.main() == 0
        assert len(audioq.queued) == 1

    def test_a_payload_that_is_not_json_says_nothing(self, feed_stdin, fake_speak):
        feed_stdin("not json at all")
        assert narrate.main() == 0
        assert fake_speak[0].synthesized == []

    def test_a_muted_session_says_nothing(self, feed_stdin, fake_speak, monkeypatch):
        speak, _ = fake_speak
        monkeypatch.setattr(speak, "allowed", False)
        feed_stdin(self._payload())
        assert narrate.main() == 0
        assert speak.synthesized == []

    def test_a_headless_box_says_nothing(self, feed_stdin, fake_speak, monkeypatch):
        # No PipeWire session means no speaker to narrate to.
        speak, _ = fake_speak
        monkeypatch.setattr(speak, "audio", False)
        feed_stdin(self._payload())
        assert narrate.main() == 0
        assert speak.synthesized == []

    def test_nothing_speakable_says_nothing(self, feed_stdin, fake_speak):
        feed_stdin({"session_id": "s1", "prompt_id": "p1", "message": "Ok."})
        assert narrate.main() == 0
        assert fake_speak[0].synthesized == []

    def test_the_per_turn_budget_stops_it(self, feed_stdin, fake_speak, with_cfg):
        with_cfg(max_per_turn=2, word_limit=50)
        narrate.turn_state("p1").write_text(json.dumps({"n": 2, "last": 0, "said": []}))
        feed_stdin(self._payload())
        assert narrate.main() == 0
        assert fake_speak[0].synthesized == []

    def test_it_never_repeats_itself_within_a_turn(self, feed_stdin, fake_speak):
        text = "Committed that, and now the tests are running."
        h = hashlib.sha1(text.encode()).hexdigest()[:10]
        narrate.turn_state("p1").write_text(json.dumps({"n": 1, "last": 0, "said": [h]}))
        feed_stdin(self._payload(text))
        assert narrate.main() == 0
        assert fake_speak[0].synthesized == []

    def test_a_corrupt_state_file_starts_the_turn_over(self, feed_stdin, fake_speak):
        narrate.turn_state("p1").write_text("{ not json")
        feed_stdin(self._payload())
        assert narrate.main() == 0
        assert json.loads(narrate.turn_state("p1").read_text())["n"] == 1

    def test_a_voice_model_that_is_not_installed_queues_nothing(
        self, feed_stdin, fake_speak, monkeypatch
    ):
        speak, audioq = fake_speak
        monkeypatch.setattr(speak, "synthesize", classmethod(lambda cls, text, path: False))
        feed_stdin(self._payload())
        assert narrate.main() == 0
        assert audioq.queued == []

    def test_a_state_file_that_cannot_be_written_still_speaks(
        self, feed_stdin, fake_speak, home, monkeypatch
    ):
        # The notice is the point; remembering it is the bonus.
        blocked = home / "not-a-directory"
        blocked.write_text("in the way")
        monkeypatch.setattr(narrate, "BASE", blocked)
        feed_stdin(self._payload())
        assert narrate.main() == 0
        assert fake_speak[1].queued

    def test_the_spoken_history_is_capped_at_twenty(self, feed_stdin, fake_speak):
        old = [f"{i:010d}" for i in range(20)]
        narrate.turn_state("p1").write_text(json.dumps({"n": 1, "last": 0, "said": old}))
        feed_stdin(self._payload())
        assert narrate.main() == 0
        st = json.loads(narrate.turn_state("p1").read_text())
        assert len(st["said"]) == 20
        assert old[0] not in st["said"]

    def test_it_records_when_the_notice_will_finish_playing(self, feed_stdin, fake_speak):
        # Spacing is measured from the END of the previous notice, so the audio
        # has to be measured rather than guessed.
        import time

        feed_stdin(self._payload())
        before = time.time()
        assert narrate.main() == 0
        st = json.loads(narrate.turn_state("p1").read_text())
        assert st["last"] >= before + 0.5
