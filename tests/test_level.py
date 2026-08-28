"""How loud the voice is: the envelope the mouth reads, the number the ear writes."""

import os
import sys
import time
import wave
from array import array
from pathlib import Path

import pytest

import claude_voice.level as level


def _wav(path, samples, rate=1000, chans=1, width=2):
    """A WAV on disk. The default rate makes a bucket exactly 40 samples,
    so a test can count buckets instead of computing them."""
    with wave.open(str(path), "wb") as w:
        w.setnchannels(chans)
        w.setsampwidth(width)
        w.setframerate(rate)
        w.writeframes(array("h", samples).tobytes() if width == 2 else bytes(samples))
    return path


class TestEnvelope:
    """One 0-100 number per 40 ms, normalised over the utterance."""

    def test_it_returns_one_value_per_step(self, home):
        # 1000 Hz and STEP=0.04 means 40 frames a bucket; 120 frames is three.
        path = _wav(home / "a.wav", [1000] * 120)
        secs, env = level.envelope(path)
        assert len(env) == 3
        assert secs == pytest.approx(0.12)

    def test_the_loudest_bucket_is_full_scale(self, home):
        # Normalised per utterance: a line synthesised quietly still fills the
        # reactor rather than reading as a fault.
        path = _wav(home / "a.wav", [100] * 40 + [1000] * 40)
        _, env = level.envelope(path)
        assert env[-1] == 100
        assert 0 < env[0] < 100

    def test_the_quiet_parts_are_lifted_by_the_curve(self, home):
        # A consonant a tenth of a vowel still has to be visible next to one,
        # so the ratio is raised to 0.6 rather than shown raw.
        path = _wav(home / "a.wav", [1000] * 40 + [10000] * 40)
        _, env = level.envelope(path)
        assert env[0] > 10

    def test_the_negative_half_counts(self, home):
        # Speech is usually louder below the line than above it.
        quiet = _wav(home / "q.wav", [1] * 40 + [-20000] * 40)
        _, env = level.envelope(quiet)
        assert env[1] == 100

    def test_silence_has_no_envelope(self, home):
        # Nothing to normalise against, and a curve over zero is a lie.
        secs, env = level.envelope(_wav(home / "a.wav", [0] * 80))
        assert env == []
        assert secs == pytest.approx(0.08)

    def test_an_empty_file_is_no_envelope_and_no_time(self, home):
        assert level.envelope(_wav(home / "a.wav", [])) == (0.0, [])

    def test_eight_bit_audio_is_refused(self, home):
        # 16-bit is what we synthesise; anything else is somebody else's file.
        path = _wav(home / "a.wav", [128] * 80, width=1)
        secs, env = level.envelope(path)
        assert env == []
        assert secs == pytest.approx(0.08)

    def test_stereo_buckets_by_frame_not_by_sample(self, home):
        # Two channels means twice the samples for the same 40 ms.
        path = _wav(home / "a.wav", [1000] * 160, chans=2)
        _, env = level.envelope(path)
        assert len(env) == 2

    def test_a_big_endian_host_still_reads_a_little_endian_wav(self, home, monkeypatch):
        # 0x0001 and 0x0100 swap places, so the loud bucket moves: proof the
        # byteswap happened rather than the samples being read as written.
        monkeypatch.setattr(sys, "byteorder", "big")
        path = _wav(home / "a.wav", [1] * 40 + [256] * 40)
        _, env = level.envelope(path)
        assert env[0] == 100

    def test_a_trailing_partial_bucket_still_counts(self, home):
        path = _wav(home / "a.wav", [1000] * 50)
        _, env = level.envelope(path)
        assert len(env) == 2


class TestAt:
    """The envelope indexed by the wall clock, so a late window catches up."""

    def test_no_envelope_is_no_movement(self):
        assert level.at([], 100.0, now=100.0) == 0.0

    def test_no_start_time_is_no_movement(self):
        assert level.at([50, 50], 0.0, now=100.0) == 0.0

    def test_the_first_sample_lands_a_lead_after_the_start(self):
        # aplay has to open the device first: the envelope is played slightly
        # behind the clock, because a mouth that moves early reads as a fault.
        assert level.at([80, 40], 100.0, now=100.0 + level.LEAD) == pytest.approx(0.8)

    def test_it_interpolates_between_samples(self):
        # Twenty-five numbers a second must not come out as twenty-five steps.
        t = 100.0 + level.LEAD + level.STEP / 2
        assert level.at([80, 40], 100.0, now=t) == pytest.approx(0.6)

    def test_asking_a_shade_early_gives_the_first_sample(self):
        # One step of slack at the front, which is the lead itself.
        t = 100.0 + level.LEAD - level.STEP / 2
        assert level.at([80, 40], 100.0, now=t) == pytest.approx(0.8)

    def test_before_the_line_there_is_silence(self):
        assert level.at([80, 40], 100.0, now=100.0 + level.LEAD - 2 * level.STEP) == 0.0

    def test_after_the_line_there_is_silence(self):
        # Not the nearest value held: a mouth left open is worse than a late one.
        assert level.at([80, 40], 100.0, now=100.0 + level.LEAD + 2 * level.STEP) == 0.0

    def test_the_last_sample_is_held_to_the_end(self):
        t = 100.0 + level.LEAD + 1.5 * level.STEP
        assert level.at([80, 40], 100.0, now=t) == pytest.approx(0.4)

    def test_a_custom_step_stretches_the_envelope(self):
        assert level.at([80, 40], 100.0, step=1.0, now=100.0 + level.LEAD + 0.5) == pytest.approx(
            0.6
        )

    def test_a_zero_step_falls_back_to_the_default(self):
        t = 100.0 + level.LEAD + level.STEP
        assert level.at([80, 40], 100.0, step=0, now=t) == pytest.approx(0.4)

    def test_it_reads_the_clock_when_not_given_one(self, monkeypatch):
        # `now` is a seam for the tests; the HUD calls this on every frame
        # with nothing but the start time.
        monkeypatch.setattr(time, "time", lambda: 500.0 + level.LEAD)
        assert level.at([80, 40], 500.0) == pytest.approx(0.8)


class TestPublish:
    """The ear's level: one bare float, rewritten twenty-five times a second."""

    def test_it_writes_three_decimals(self, home):
        level.publish(0.5)
        assert level.LIVE.read_text() == "0.500"

    def test_it_clamps_both_ends(self, home):
        level.publish(7.0)
        assert level.LIVE.read_text() == "1.000"
        level.publish(-7.0)
        assert level.LIVE.read_text() == "0.000"

    def test_a_failed_write_is_never_allowed_to_cost_a_frame(self, monkeypatch):
        monkeypatch.setattr(level, "BASE", Path("/proc/nowhere/claude-voice"))
        monkeypatch.setattr(level, "LIVE", Path("/proc/nowhere/claude-voice/mic-level"))
        assert level.publish(0.5) is None


class TestClear:
    """The room closing, which is not the same as the room being quiet."""

    def test_it_removes_the_file(self, home):
        level.publish(0.5)
        level.clear()
        assert not level.LIVE.exists()

    def test_clearing_twice_is_not_an_error(self, home):
        level.clear()
        assert level.clear() is None

    def test_an_unremovable_level_is_absorbed(self, home, monkeypatch):
        monkeypatch.setattr(level, "LIVE", home)
        assert level.clear() is None


class TestPublishing:
    """Is anybody listening at all -- a different question from how loud."""

    def test_a_fresh_level_means_somebody_is_listening(self, home):
        level.publish(0.2)
        assert level.publishing() is True

    def test_no_file_means_nobody_is(self, home):
        assert level.publishing() is False

    def test_a_stale_level_is_a_closed_room(self, home):
        # A file is not a heartbeat: the ear stops publishing when it stops
        # listening, and the last value must not stand in for a live one.
        level.publish(0.2)
        old = time.time() - level.STALE - 1
        os.utime(level.LIVE, (old, old))
        assert level.publishing() is False


class TestLive:
    """What the microphone is hearing, or zero."""

    def test_it_reads_back_what_was_published(self, home):
        level.publish(0.25)
        assert level.live() == pytest.approx(0.25)

    def test_nothing_published_is_zero(self, home):
        assert level.live() == 0.0

    def test_a_stale_level_reads_as_silence(self, home):
        level.publish(0.9)
        old = time.time() - level.STALE - 1
        os.utime(level.LIVE, (old, old))
        assert level.live() == 0.0

    def test_a_torn_write_reads_as_silence_rather_than_raising(self, home):
        # The reader that catches a half-written file shows the previous value
        # one frame longer, which at this rate is invisible.
        level.publish(0.5)
        level.LIVE.write_text("0.4\x00\x00garbage")
        assert level.live() == 0.0

    def test_an_out_of_range_value_is_clamped(self, home):
        level.publish(0.5)
        level.LIVE.write_text("4.0")
        assert level.live() == 1.0
