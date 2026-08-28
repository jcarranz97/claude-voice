"""Whether a window is open, answered from liveness rather than a marker.

Every test here writes real pid files into the throwaway ``CLAUDE_VOICE_HOME``
and asks the module. The point of the design is that a stale file is worth
nothing, so faking the answer would test the wrong thing -- what is faked is
only ``os.kill``, in the two cases a test cannot arrange for real.
"""

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

import claude_voice.presence as presence


@pytest.fixture(autouse=True)
def fresh_config(home):
    """Drop the cache of the config module *this* module imported.

    The package imports its siblings by bare name, so ``config`` and
    ``claude_voice.config`` are two module objects with two caches. The shared
    ``home`` fixture reloads the dotted one; the bare one would otherwise keep
    whatever an earlier test wrote in a directory that no longer exists.
    """
    presence._config.load(reload=True)
    yield
    presence._config.load(reload=True)


def pidfile(home, pid) -> Path:
    f = home / f"{presence.PREFIX}{pid}{presence.SUFFIX}"
    f.write_text(str(pid))
    return f


class TestAlive:
    """`_alive` decides whether a recorded pid is still a process."""

    def test_this_process_is_alive(self):
        assert presence._alive(os.getpid()) is True

    def test_a_missing_process_is_not(self, monkeypatch):
        def gone(pid, sig):
            raise ProcessLookupError

        monkeypatch.setattr(presence.os, "kill", gone)
        assert presence._alive(12345) is False

    def test_a_nonsensical_pid_is_not(self, monkeypatch):
        def bad(pid, sig):
            raise ValueError("pid out of range")

        monkeypatch.setattr(presence.os, "kill", bad)
        assert presence._alive(-1) is False

    def test_somebody_elses_process_still_counts(self, monkeypatch):
        # A HUD running as another user is a window that is open. Refusing to
        # signal it says nothing about whether it is there.
        def denied(pid, sig):
            raise PermissionError

        monkeypatch.setattr(presence.os, "kill", denied)
        assert presence._alive(1) is True


class TestWindows:
    """`windows` lists the live HUDs and sweeps whatever is left over."""

    def test_no_files_means_no_windows(self, home):
        assert presence.windows() == []

    def test_a_live_pid_is_listed(self, home):
        pidfile(home, os.getpid())
        assert presence.windows() == [os.getpid()]

    def test_a_dead_pid_is_swept(self, home, monkeypatch):
        stale = pidfile(home, 4242)
        monkeypatch.setattr(presence, "_alive", lambda pid: False)
        assert presence.windows() == []
        assert not stale.exists()

    def test_a_file_that_is_not_a_pid_is_ignored(self, home):
        junk = home / f"{presence.PREFIX}nonsense{presence.SUFFIX}"
        junk.write_text("")
        assert presence.windows() == []
        # Not swept either: the sweep is for files that named a pid and lost
        # it, and this one never named one.
        assert junk.exists()

    def test_a_sweep_that_cannot_delete_is_survived(self, home, monkeypatch):
        pidfile(home, 4242)
        monkeypatch.setattr(presence, "_alive", lambda pid: False)

        def refuse(self, missing_ok=False):
            raise PermissionError

        monkeypatch.setattr(Path, "unlink", refuse)
        assert presence.windows() == []

    def test_an_unreadable_state_dir_reads_as_no_windows(self, monkeypatch):
        def boom(pattern):
            raise OSError("gone")

        monkeypatch.setattr(presence, "BASE", SimpleNamespace(glob=boom))
        assert presence._files() == []
        assert presence.windows() == []


class TestRequired:
    """`required` is the setting that lets the voice run with no window."""

    def test_a_window_is_required_by_default(self, cfg):
        assert presence.required() is True

    def test_the_config_can_turn_the_gate_off(self, write_config):
        write_config("[hud]\nrequired = false\n")
        presence._config.load(reload=True)
        assert presence.required() is False

    def test_an_unreadable_config_still_requires_a_window(self):
        # Failing open would let a broken config file re-enable the voice on a
        # machine with nothing on screen, which is the whole failure mode.
        def boom(reload=False):
            raise RuntimeError("no config")

        # Restored by hand rather than by monkeypatch: the fixture that resets
        # the config cache tears down first and would run into this stub.
        real = presence._config.load
        presence._config.load = boom
        try:
            assert presence.required() is True
        finally:
            presence._config.load = real


class TestOpenNow:
    """`open_now` is the single question every hook asks."""

    def test_closed_with_no_window(self, home):
        assert presence.open_now() is False

    def test_open_with_a_live_window(self, home):
        pidfile(home, os.getpid())
        assert presence.open_now() is True

    def test_always_open_when_the_gate_is_off(self, write_config):
        write_config("[hud]\nrequired = false\n")
        presence._config.load(reload=True)
        assert presence.open_now() is True


class TestEnterAndLeave:
    """A window records itself under its own pid, and tidies up after."""

    def test_entering_writes_this_pid(self, home):
        f = presence.enter()
        assert f.name == f"{presence.PREFIX}{os.getpid()}{presence.SUFFIX}"
        assert f.read_text() == str(os.getpid())
        assert presence.open_now() is True

    def test_leaving_removes_it(self, home):
        presence.enter()
        presence.leave()
        assert presence.windows() == []

    def test_leaving_twice_is_harmless(self, home):
        presence.enter()
        presence.leave()
        presence.leave()

    def test_a_state_dir_that_cannot_be_made_is_survived(self, home, monkeypatch):
        # enter() is called on the HUD's startup path. It reports no window
        # rather than taking the window down with it.
        blocked = home / "not-a-dir"
        blocked.write_text("")
        monkeypatch.setattr(presence, "BASE", blocked)
        f = presence.enter()
        assert not f.exists()

    def test_a_pid_file_that_cannot_be_removed_is_survived(self, home, monkeypatch):
        (home / f"{presence.PREFIX}{os.getpid()}{presence.SUFFIX}").mkdir()
        presence.leave()


class TestLastOneOut:
    """Two HUDs are two windows; the first to close takes nothing with it."""

    def test_alone_means_last_one_out(self, home):
        assert presence.last_one_out() is True

    def test_another_live_window_is_not(self, home):
        pidfile(home, os.getpid())
        assert presence.last_one_out() is False
