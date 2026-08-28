"""The microphone: who holds it, for how long, and what is worth saying.

Nothing here may touch a real capture device, so the two sources this module
reads from are both replaced: ``/proc`` becomes a directory we built, and
``pw-dump`` becomes captured text. The parsing of that text is the whole of the
logic, so the samples below are shaped like the real thing -- a client object
carrying the pid, a node object pointing back at it by ``client.id`` -- rather
than trimmed down to the four keys the code happens to read.
"""

import json
import os
import signal
from pathlib import Path

import pytest

import claude_voice.config as config
import claude_voice.mic as mic

# --- the fixtures the module reads from ---------------------------------

# A busy machine: Firefox recording through a running stream, Chromium holding
# a second one open and suspended, and a sink that is none of our business.
PW_DUMP_BUSY = """[
  {
    "id": 39,
    "type": "PipeWire:Interface:Client",
    "info": {
      "props": {
        "application.name": "Firefox",
        "pipewire.sec.pid": 3312,
        "pipewire.sec.uid": 1000
      }
    }
  },
  {
    "id": 52,
    "type": "PipeWire:Interface:Node",
    "info": {
      "state": "running",
      "props": {
        "media.class": "Stream/Input/Audio",
        "client.id": 39,
        "node.name": "Firefox",
        "media.name": "AudioStream",
        "application.name": "Firefox"
      }
    }
  },
  {
    "id": 55,
    "type": "PipeWire:Interface:Node",
    "info": {
      "state": "suspended",
      "props": {
        "media.class": "Stream/Input/Audio",
        "client.id": 61,
        "media.name": "Chromium input"
      }
    }
  },
  {
    "id": 61,
    "type": "PipeWire:Interface:Client",
    "info": {
      "props": {"application.name": "Chromium", "pipewire.sec.pid": 4501}
    }
  },
  {
    "id": 70,
    "type": "PipeWire:Interface:Node",
    "info": {
      "state": "idle",
      "props": {
        "media.class": "Audio/Sink",
        "node.name": "alsa_output.pci-0000_00_1f.3.analog-stereo"
      }
    }
  }
]
"""

# The quiet machine: a sink, a source, and not one stream on either.
PW_DUMP_IDLE = """[
  {
    "id": 46,
    "type": "PipeWire:Interface:Node",
    "info": {
      "state": "suspended",
      "props": {
        "media.class": "Audio/Source",
        "node.name": "alsa_input.pci-0000_00_1f.3.analog-stereo"
      }
    }
  }
]
"""

# What a truncated read looks like: pw-dump killed part-way through writing.
PW_DUMP_TRUNCATED = '[{"id": 39, "type": "PipeWire:Interface:Nod'

# A genuine /proc/<pid>/stat, comm and all. The parenthesised name is the
# reason `_started` counts from the last ')' rather than splitting on spaces.
STAT = (
    "{pid} ({comm}) S 1 {pid} {pid} 0 -1 4194560 1180 0 0 0 43 12 0 0 "
    "20 0 4 0 {start} 312664064 2604 18446744073709551615 1 1 0 0 0 0 0 "
    "4096 0 0 0 0 17 6 0 0 0 0 0 0 0 0 0 0 0 0 0"
)


def make_proc(root, pid, comm, cmdline=(), start=987654):
    """One process under our stand-in ``/proc``."""
    d = root / str(pid)
    d.mkdir(parents=True, exist_ok=True)
    (d / "comm").write_text(comm + "\n")
    (d / "cmdline").write_bytes(b"".join(a.encode() + b"\0" for a in cmdline))
    (d / "stat").write_text(STAT.format(pid=pid, comm=comm, start=start))
    return d


def redirect_proc(monkeypatch, root, *modules):
    """Point every ``Path("/proc")`` inside *modules* at *root*.

    The paths are literals in the source -- there is no seam to inject a root
    through -- so the name ``Path`` is what gets replaced. Everything that is
    not under /proc is handed to the real constructor untouched.
    """

    def _patched(*parts):
        p = Path(*parts)
        s = str(p)
        if s == "/proc":
            return root
        if s.startswith("/proc/"):
            return root / s[len("/proc/") :]
        return p

    for m in modules:
        monkeypatch.setattr(m, "Path", _patched)


class Clock:
    """A stand-in for the ``time`` module, so nothing waits on the real one."""

    def __init__(self, now=1_700_000_000.0, stop_after=None):
        self.now = now
        self.slept = []
        self.stop_after = stop_after

    def time(self):
        return self.now

    def sleep(self, secs):
        self.slept.append(secs)
        self.now += secs
        if self.stop_after is not None and len(self.slept) >= self.stop_after:
            raise LoopStopped


class LoopStopped(Exception):
    """Breaks ``--watch`` out of its otherwise endless loop."""


def cfg(**dotted):
    """A ``Config`` holding just the keys a test cares about."""
    data = {}
    for key, value in dotted.items():
        node = data
        parts = key.split("__")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value
    return config.Config(data)


@pytest.fixture(autouse=True)
def fresh_caches():
    """The one-second and five-second caches must not leak between tests."""
    mic._open_cache.update(t=0.0, v=False)
    mic._held_cache.update(t=0.0, v=[])
    yield
    mic._open_cache.update(t=0.0, v=False)
    mic._held_cache.update(t=0.0, v=[])


@pytest.fixture
def proc(home, monkeypatch):
    """A stand-in ``/proc``, already wired into the module."""
    root = home / "proc"
    root.mkdir()
    redirect_proc(monkeypatch, root, mic)
    return root


class TestOurCaptures:
    """`our_captures` names pw-record processes by signature, not by pidfile."""

    def test_matches_the_raw_capture_flags(self, proc, monkeypatch):
        monkeypatch.setattr(mic, "CFG", cfg(stt__node=""))
        make_proc(proc, 8801, "pw-record", ["pw-record", "--raw", "--latency=20ms", "-"])
        assert mic.our_captures() == [8801]

    def test_matches_the_configured_node(self, proc, monkeypatch):
        monkeypatch.setattr(mic, "CFG", cfg(stt__node="alsa_input.usb-Blue_Yeti"))
        make_proc(proc, 8802, "pw-record", ["pw-record", "--target", "alsa_input.usb-Blue_Yeti"])
        assert mic.our_captures() == [8802]

    def test_ignores_a_pw_record_that_is_not_ours(self, proc, monkeypatch):
        monkeypatch.setattr(mic, "CFG", cfg(stt__node=""))
        make_proc(proc, 8803, "pw-record", ["pw-record", "recording.wav"])
        assert mic.our_captures() == []

    def test_ignores_processes_that_are_not_pw_record(self, proc, monkeypatch):
        monkeypatch.setattr(mic, "CFG", cfg(stt__node=""))
        make_proc(proc, 8804, "firefox", ["firefox", "--raw", "--latency"])
        assert mic.our_captures() == []

    def test_skips_the_non_numeric_entries(self, proc, monkeypatch):
        # /proc is full of them -- self, cpuinfo, asound -- and none is a pid.
        monkeypatch.setattr(mic, "CFG", cfg(stt__node=""))
        (proc / "self").mkdir()
        (proc / "cpuinfo").write_text("processor : 0\n")
        assert mic.our_captures() == []

    def test_survives_a_process_that_exits_mid_read(self, proc, monkeypatch):
        # The directory is there and comm is not: the pid died between the
        # listing and the read, which happens on any busy machine.
        monkeypatch.setattr(mic, "CFG", cfg(stt__node=""))
        (proc / "8805").mkdir()
        assert mic.our_captures() == []


class TestMicOpen:
    """`mic_open` answers "is anyone hearing me", not "does a stream exist"."""

    def test_a_capture_of_ours_is_enough(self, monkeypatch, no_subprocess):
        monkeypatch.setattr(mic, "our_captures", lambda: [8801])
        assert mic.mic_open(fresh=True) is True

    def test_a_running_stream_of_anyones_counts(self, monkeypatch, fake_proc):
        monkeypatch.setattr(mic, "our_captures", list)
        monkeypatch.setattr(mic.subprocess, "run", lambda *a, **kw: fake_proc(stdout=PW_DUMP_BUSY))
        assert mic.mic_open(fresh=True) is True

    def test_a_parked_stream_does_not(self, monkeypatch, fake_proc):
        dump = json.loads(PW_DUMP_BUSY)
        for o in dump:
            if o.get("info", {}).get("state") == "running":
                o["info"]["state"] = "idle"
        monkeypatch.setattr(mic, "our_captures", list)
        monkeypatch.setattr(
            mic.subprocess, "run", lambda *a, **kw: fake_proc(stdout=json.dumps(dump))
        )
        assert mic.mic_open(fresh=True) is False

    def test_a_broken_proc_read_is_not_an_open_microphone(self, monkeypatch, fake_proc):
        def _boom():
            raise PermissionError("/proc")

        monkeypatch.setattr(mic, "our_captures", _boom)
        monkeypatch.setattr(mic.subprocess, "run", lambda *a, **kw: fake_proc(stdout=PW_DUMP_IDLE))
        assert mic.mic_open(fresh=True) is False

    def test_truncated_pw_dump_falls_through_to_alsa(self, monkeypatch, fake_proc, proc):
        monkeypatch.setattr(mic, "our_captures", list)
        monkeypatch.setattr(
            mic.subprocess, "run", lambda *a, **kw: fake_proc(stdout=PW_DUMP_TRUNCATED)
        )
        status = proc / "asound" / "card0" / "pcm0c" / "sub0" / "status"
        status.parent.mkdir(parents=True)
        status.write_text("state: RUNNING\nowner_pid   : 3312\ntrigger_time: 9210.481\n")
        assert mic.mic_open(fresh=True) is True

    def test_missing_pw_dump_falls_through_to_alsa(self, monkeypatch, proc):
        def _missing(*a, **kw):
            raise FileNotFoundError("pw-dump")

        monkeypatch.setattr(mic, "our_captures", list)
        monkeypatch.setattr(mic.subprocess, "run", _missing)
        status = proc / "asound" / "card0" / "pcm0c" / "sub0" / "status"
        status.parent.mkdir(parents=True)
        status.write_text("closed\n")
        assert mic.mic_open(fresh=True) is False

    def test_neither_source_available_reads_as_closed(self, monkeypatch, proc):
        def _missing(*a, **kw):
            raise FileNotFoundError("pw-dump")

        monkeypatch.setattr(mic, "our_captures", list)
        monkeypatch.setattr(mic.subprocess, "run", _missing)
        # A status that is a directory: the glob finds it, the read cannot.
        (proc / "asound" / "card0" / "pcm0c" / "sub0" / "status").mkdir(parents=True)
        assert mic.mic_open() is False

    def test_the_answer_is_cached_for_a_second(self, monkeypatch, no_subprocess):
        monkeypatch.setattr(mic, "our_captures", lambda: [8801])
        assert mic.mic_open(fresh=True) is True
        # The HUD asks twenty times a second; the second answer is the cache,
        # which is why forbidding subprocess here does not fail the test.
        monkeypatch.setattr(mic, "our_captures", list)
        assert mic.mic_open() is True


class TestMicSpeaking:
    def test_reads_the_marker_file(self, home, no_subprocess):
        assert mic.mic_speaking() is False
        (home / "mic-active").touch()
        assert mic.mic_speaking() is True


class TestMicHeld:
    """`mic_held` names the app that lit the tray icon without recording."""

    def test_names_the_parked_holder_by_its_command(self, monkeypatch, fake_proc, proc):
        make_proc(proc, 4501, "chromium")
        monkeypatch.setattr(mic.subprocess, "run", lambda *a, **kw: fake_proc(stdout=PW_DUMP_BUSY))
        # Firefox is running, so it belongs to mic_open, not here.
        assert mic.mic_held() == ["chromium (4501)"]

    def test_a_dead_pid_is_residue_rather_than_a_holder(self, monkeypatch, fake_proc, proc):
        monkeypatch.setattr(mic.subprocess, "run", lambda *a, **kw: fake_proc(stdout=PW_DUMP_BUSY))
        assert mic.mic_held() == []

    def test_an_unattributable_stream_falls_back_to_its_own_name(
        self, monkeypatch, fake_proc, proc
    ):
        dump = [
            {
                "id": 55,
                "type": "PipeWire:Interface:Node",
                "info": {
                    "state": "suspended",
                    "props": {
                        "media.class": "Stream/Input/Audio",
                        "application.name": "OBS Studio",
                    },
                },
            }
        ]
        monkeypatch.setattr(
            mic.subprocess, "run", lambda *a, **kw: fake_proc(stdout=json.dumps(dump))
        )
        assert mic.mic_held() == ["OBS Studio"]

    def test_a_stream_with_no_name_at_all_is_a_question_mark(self, monkeypatch, fake_proc, proc):
        dump = [
            {
                "id": 55,
                "type": "PipeWire:Interface:Node",
                "info": {"state": "idle", "props": {"media.class": "Stream/Input/Audio"}},
            }
        ]
        monkeypatch.setattr(
            mic.subprocess, "run", lambda *a, **kw: fake_proc(stdout=json.dumps(dump))
        )
        assert mic.mic_held() == ["?"]

    def test_without_pw_dump_nobody_is_named(self, monkeypatch, proc):
        def _missing(*a, **kw):
            raise FileNotFoundError("pw-dump")

        monkeypatch.setattr(mic.subprocess, "run", _missing)
        assert mic.mic_held() == []

    def test_the_list_is_cached_for_five_seconds(self, monkeypatch, fake_proc, proc, no_subprocess):
        mic._held_cache.update(t=9e18, v=["cached (1)"])
        assert mic.mic_held() == ["cached (1)"]


class TestListenStranded:
    def test_empty_when_the_daemon_is_working(self, home, no_subprocess):
        assert mic.listen_stranded() == ""

    def test_reports_what_listen_wrote(self, home, no_subprocess):
        (home / "listen-stranded").write_text("no session to deliver to\n")
        assert mic.listen_stranded() == "no session to deliver to"


class TestDaemonAlive:
    def test_false_without_a_pidfile(self, home, no_subprocess):
        assert mic.daemon_alive() is False

    def test_false_when_the_pidfile_is_not_a_number(self, home, no_subprocess):
        (home / "listen.pid").write_text("not a pid\n")
        assert mic.daemon_alive() is False

    def test_true_while_the_pid_answers(self, home, no_subprocess):
        # Our own pid: signal 0 checks for existence and delivers nothing.
        (home / "listen.pid").write_text(f"{os.getpid()}\n")
        assert mic.daemon_alive() is True


class TestSweepOrphans:
    """`--sweep` closes captures of ours, and only those."""

    def test_signals_each_capture_of_ours(self, monkeypatch, no_subprocess):
        sent = []
        monkeypatch.setattr(mic, "our_captures", lambda: [8801, 8802])
        monkeypatch.setattr(os, "kill", lambda pid, sig: sent.append((pid, sig)))
        assert mic.sweep_orphans() == 2
        assert sent == [(8801, signal.SIGTERM), (8802, signal.SIGTERM)]

    def test_never_signals_itself(self, monkeypatch, no_subprocess):
        sent = []
        monkeypatch.setattr(mic, "our_captures", lambda: [os.getpid()])
        monkeypatch.setattr(os, "kill", lambda pid, sig: sent.append(pid))
        assert mic.sweep_orphans() == 0
        assert sent == []

    def test_a_pid_that_died_first_is_not_a_failure(self, monkeypatch, no_subprocess):
        def _gone(pid, sig):
            raise ProcessLookupError(pid)

        monkeypatch.setattr(mic, "our_captures", lambda: [8801])
        monkeypatch.setattr(os, "kill", _gone)
        assert mic.sweep_orphans() == 0

    def test_a_capture_with_a_live_owner_is_not_an_orphan(self, monkeypatch, no_subprocess):
        # The whole distinction the word carries. Sweeping here stopped a
        # conversation that was working, which is what `x` in the HUD did.
        sent = []
        monkeypatch.setattr(mic, "daemon_alive", lambda: True)
        monkeypatch.setattr(mic, "our_captures", lambda: [8801, 8802])
        monkeypatch.setattr(os, "kill", lambda pid, sig: sent.append(pid))
        assert mic.sweep_orphans() == 0
        assert sent == []


class TestStartedAndComm:
    """The pid alone is not an identity; the kernel start time completes it."""

    def test_reads_field_twenty_two_past_the_parenthesised_name(self, proc):
        make_proc(proc, 8801, "pw-record", start=4412233)
        assert mic._started(8801) == "4412233"

    def test_a_name_with_spaces_and_brackets_does_not_shift_the_field(self, proc):
        make_proc(proc, 8802, "Web Content (2)", start=99)
        assert mic._started(8802) == "99"

    def test_a_process_that_is_gone_has_no_start_time(self, proc):
        assert mic._started(8899) == ""

    def test_comm_is_the_executable_name(self, proc):
        make_proc(proc, 8801, "pw-record")
        assert mic._comm(8801) == "pw-record"

    def test_comm_is_empty_for_a_process_that_is_gone(self, proc):
        assert mic._comm(8899) == ""


class TestHolders:
    """Four kinds of claim, because four different things should happen."""

    def test_our_capture_with_a_live_daemon_is_ours(self, monkeypatch, proc, home, fake_proc):
        monkeypatch.setattr(mic, "our_captures", lambda: [8801])
        make_proc(proc, 8801, "pw-record", start=555)
        (home / "listen.pid").write_text(f"{os.getpid()}\n")
        monkeypatch.setattr(mic.subprocess, "run", lambda *a, **kw: fake_proc(stdout=PW_DUMP_IDLE))
        assert mic.holders() == [
            {"kind": "ours", "pid": 8801, "name": "pw-record", "key": "8801:555"}
        ]

    def test_our_capture_with_no_daemon_is_an_orphan(self, monkeypatch, proc, fake_proc):
        monkeypatch.setattr(mic, "our_captures", lambda: [8801])
        make_proc(proc, 8801, "pw-record", start=555)
        monkeypatch.setattr(mic.subprocess, "run", lambda *a, **kw: fake_proc(stdout=PW_DUMP_IDLE))
        assert [h["kind"] for h in mic.holders()] == ["orphan"]

    def test_an_orphan_whose_comm_is_unreadable_is_still_named(self, monkeypatch, proc, fake_proc):
        monkeypatch.setattr(mic, "our_captures", lambda: [8801])
        monkeypatch.setattr(mic.subprocess, "run", lambda *a, **kw: fake_proc(stdout=PW_DUMP_IDLE))
        assert mic.holders() == [
            {"kind": "orphan", "pid": 8801, "name": "pw-record", "key": "8801:"}
        ]

    def test_other_peoples_streams_split_into_recording_and_parked(
        self, monkeypatch, proc, fake_proc
    ):
        monkeypatch.setattr(mic, "our_captures", list)
        make_proc(proc, 3312, "firefox", start=111)
        make_proc(proc, 4501, "chromium", start=222)
        monkeypatch.setattr(mic.subprocess, "run", lambda *a, **kw: fake_proc(stdout=PW_DUMP_BUSY))
        assert mic.holders() == [
            {"kind": "recording", "pid": 3312, "name": "firefox", "key": "3312:111"},
            {"kind": "parked", "pid": 4501, "name": "chromium", "key": "4501:222"},
        ]

    def test_a_stream_of_ours_is_not_listed_twice(self, monkeypatch, proc, fake_proc):
        # pw-dump sees our capture too; holders() classified it already.
        monkeypatch.setattr(mic, "our_captures", lambda: [3312])
        make_proc(proc, 3312, "pw-record", start=111)
        make_proc(proc, 4501, "chromium", start=222)
        monkeypatch.setattr(mic.subprocess, "run", lambda *a, **kw: fake_proc(stdout=PW_DUMP_BUSY))
        assert [h["pid"] for h in mic.holders()] == [3312, 4501]

    def test_a_node_whose_process_is_gone_is_skipped(self, monkeypatch, proc, fake_proc):
        monkeypatch.setattr(mic, "our_captures", list)
        monkeypatch.setattr(mic.subprocess, "run", lambda *a, **kw: fake_proc(stdout=PW_DUMP_BUSY))
        assert mic.holders() == []

    def test_a_parked_stream_nobody_owns_says_nothing(self, monkeypatch, proc, fake_proc):
        dump = [
            {
                "id": 55,
                "type": "PipeWire:Interface:Node",
                "info": {
                    "state": "suspended",
                    "props": {"media.class": "Stream/Input/Audio", "application.name": "?"},
                },
            }
        ]
        monkeypatch.setattr(mic, "our_captures", list)
        monkeypatch.setattr(
            mic.subprocess, "run", lambda *a, **kw: fake_proc(stdout=json.dumps(dump))
        )
        assert mic.holders() == []

    def test_a_running_stream_nobody_owns_is_still_reported(self, monkeypatch, proc, fake_proc):
        dump = [
            {
                "id": 55,
                "type": "PipeWire:Interface:Node",
                "info": {
                    "state": "running",
                    "props": {
                        "media.class": "Stream/Input/Audio",
                        "application.name": "OBS Studio",
                    },
                },
            }
        ]
        monkeypatch.setattr(mic, "our_captures", list)
        monkeypatch.setattr(
            mic.subprocess, "run", lambda *a, **kw: fake_proc(stdout=json.dumps(dump))
        )
        # No pid, so the name is the identity.
        assert mic.holders() == [
            {"kind": "recording", "pid": None, "name": "OBS Studio", "key": "OBS Studio"}
        ]

    def test_a_recording_does_not_hide_behind_the_same_apps_parked_stream(
        self, monkeypatch, proc, fake_proc
    ):
        dump = [
            {
                "id": 39,
                "type": "PipeWire:Interface:Client",
                "info": {"props": {"application.name": "Firefox", "pipewire.sec.pid": 3312}},
            },
            {
                "id": 52,
                "type": "PipeWire:Interface:Node",
                "info": {
                    "state": "suspended",
                    "props": {"media.class": "Stream/Input/Audio", "client.id": 39},
                },
            },
            {
                "id": 53,
                "type": "PipeWire:Interface:Node",
                "info": {
                    "state": "running",
                    "props": {"media.class": "Stream/Input/Audio", "client.id": 39},
                },
            },
        ]
        monkeypatch.setattr(mic, "our_captures", list)
        make_proc(proc, 3312, "firefox", start=111)
        monkeypatch.setattr(
            mic.subprocess, "run", lambda *a, **kw: fake_proc(stdout=json.dumps(dump))
        )
        assert [h["kind"] for h in mic.holders()] == ["recording"]

    def test_the_parked_claim_does_not_demote_a_recording(self, monkeypatch, proc, fake_proc):
        dump = [
            {
                "id": 39,
                "type": "PipeWire:Interface:Client",
                "info": {"props": {"pipewire.sec.pid": 3312}},
            },
            {
                "id": 52,
                "type": "PipeWire:Interface:Node",
                "info": {
                    "state": "running",
                    "props": {"media.class": "Stream/Input/Audio", "client.id": 39},
                },
            },
            {
                "id": 53,
                "type": "PipeWire:Interface:Node",
                "info": {
                    "state": "idle",
                    "props": {"media.class": "Stream/Input/Audio", "client.id": 39},
                },
            },
        ]
        monkeypatch.setattr(mic, "our_captures", list)
        make_proc(proc, 3312, "firefox", start=111)
        monkeypatch.setattr(
            mic.subprocess, "run", lambda *a, **kw: fake_proc(stdout=json.dumps(dump))
        )
        assert [h["kind"] for h in mic.holders()] == ["recording"]

    def test_a_missing_pw_dump_leaves_our_own_captures_standing(self, monkeypatch, proc):
        def _missing(*a, **kw):
            raise FileNotFoundError("pw-dump")

        monkeypatch.setattr(mic, "our_captures", lambda: [8801])
        make_proc(proc, 8801, "pw-record", start=555)
        monkeypatch.setattr(mic.subprocess, "run", _missing)
        assert [h["kind"] for h in mic.holders()] == ["orphan"]


class TestHuman:
    """The age, in the unit a person would have used."""

    def test_seconds_below_ninety(self):
        assert mic._human(0) == "0s"
        assert mic._human(89.7) == "89s"

    def test_minutes_up_to_ninety(self):
        assert mic._human(90) == "1m"
        assert mic._human(5399) == "89m"

    def test_hours_and_minutes_past_that(self):
        assert mic._human(5400) == "1h30m"
        # The failure that prompted the watchdog, in the unit it was reported.
        assert mic._human(7860) == "2h11m"

    def test_the_minutes_are_padded(self):
        assert mic._human(7500) == "2h05m"


class TestState:
    """What survives between timer ticks, since each tick is a new process."""

    def test_absent_state_is_empty(self, home, no_subprocess):
        assert mic._state() == {}

    def test_a_corrupt_state_file_is_empty(self, home, no_subprocess):
        mic.WATCH_STATE.write_text("{ half a write")
        assert mic._state() == {}

    def test_a_save_can_be_read_back(self, home, no_subprocess):
        mic._save({"8801:555": {"first": 1.0, "notified": 0.0}})
        assert mic._state()["8801:555"]["first"] == 1.0

    def test_a_save_that_cannot_be_written_is_swallowed(self, home, monkeypatch, no_subprocess):
        # A hook must never raise, so an unwritable state file loses the
        # ageing rather than the tick.
        blocked = home / "blocked"
        blocked.write_text("not a directory")
        monkeypatch.setattr(mic, "WATCH_STATE", blocked / "mic-watch.json")
        mic._save({"a": {}})


class TestNotify:
    """One notification, replacing the last rather than stacking on it."""

    def _sent(self, monkeypatch):
        sent = []
        monkeypatch.setattr(mic.subprocess, "run", lambda cmd, **kw: sent.append(cmd))
        return sent

    def test_an_orphan_is_critical_and_says_how_to_clear_it(self, monkeypatch):
        sent = self._sent(monkeypatch)
        mic._notify({"kind": "orphan", "name": "pw-record", "pid": 8801}, 7860)
        cmd = sent[0]
        assert cmd[0] == "notify-send"
        assert "--urgency=critical" in cmd
        assert cmd[-2] == "Microphone left open"
        assert "pw-record (8801)" in cmd[-1]
        assert "2h11m" in cmd[-1]
        assert "claude-voice mic --sweep" in cmd[-1]
        # Synchronous, so the next tick replaces this one instead of piling up.
        assert "string:x-canonical-private-synchronous:claude-voice-mic" in cmd

    def test_somebody_elses_recording_is_normal(self, monkeypatch):
        sent = self._sent(monkeypatch)
        mic._notify({"kind": "recording", "name": "firefox", "pid": 3312}, 600)
        assert "--urgency=normal" in sent[0]
        assert sent[0][-2] == "Microphone in use"

    def test_a_parked_stream_is_low_and_names_the_remedy(self, monkeypatch):
        sent = self._sent(monkeypatch)
        mic._notify({"kind": "parked", "name": "chromium", "pid": None}, 600)
        assert "--urgency=low" in sent[0]
        assert sent[0][-2] == "Microphone held open"
        assert sent[0][-1].startswith("chromium ")
        assert "Quitting it" in sent[0][-1]

    def test_a_desktop_without_notify_send_is_not_an_error(self, monkeypatch):
        def _missing(*a, **kw):
            raise FileNotFoundError("notify-send")

        monkeypatch.setattr(mic.subprocess, "run", _missing)
        mic._notify({"kind": "orphan", "name": "pw-record", "pid": 8801}, 600)


class TestCheck:
    """One tick: age every holder, speak about the ones that are overdue."""

    @pytest.fixture(autouse=True)
    def wired(self, home, monkeypatch):
        self.clock = Clock()
        self.notified = []
        monkeypatch.setattr(mic, "time", self.clock)
        monkeypatch.setattr(mic, "_notify", lambda h, age: self.notified.append((h["name"], age)))
        monkeypatch.setattr(
            mic, "CFG", cfg(mic__watch__after=300, mic__watch__repeat=1800, mic__watch__ignore=[])
        )

    def hold(self, kind="parked", name="chromium", pid=4501):
        return [{"kind": kind, "pid": pid, "name": name, "key": f"{pid}:222"}]

    def test_conversation_mode_is_never_reported(self, monkeypatch):
        monkeypatch.setattr(mic, "holders", lambda: self.hold(kind="ours", name="pw-record"))
        assert mic.check() == []
        assert mic._state() == {}

    def test_a_holder_first_seen_now_is_not_yet_overdue(self, monkeypatch):
        monkeypatch.setattr(mic, "holders", self.hold)
        assert mic.check() == []
        assert mic._state()["4501:222"]["first"] == self.clock.now

    def test_a_holder_past_the_threshold_is_announced(self, monkeypatch):
        monkeypatch.setattr(mic, "holders", self.hold)
        mic.check()
        self.clock.now += 301
        overdue = mic.check()
        assert [h["kind"] for h in overdue] == ["parked"]
        assert overdue[0]["age"] == pytest.approx(301)
        assert self.notified == [("chromium", pytest.approx(301))]

    def test_it_does_not_repeat_itself_on_the_next_tick(self, monkeypatch):
        monkeypatch.setattr(mic, "holders", self.hold)
        mic.check()
        self.clock.now += 301
        mic.check()
        self.clock.now += 60
        overdue = mic.check()
        # Still overdue, still returned, but said once.
        assert len(overdue) == 1
        assert len(self.notified) == 1

    def test_it_speaks_again_once_the_repeat_window_has_passed(self, monkeypatch):
        monkeypatch.setattr(mic, "holders", self.hold)
        mic.check()
        self.clock.now += 301
        mic.check()
        self.clock.now += 1801
        mic.check()
        assert len(self.notified) == 2

    def test_an_ignored_name_ages_but_is_never_announced(self, monkeypatch):
        monkeypatch.setattr(
            mic,
            "CFG",
            cfg(mic__watch__after=300, mic__watch__repeat=1800, mic__watch__ignore=["CHROMIUM"]),
        )
        monkeypatch.setattr(mic, "holders", self.hold)
        mic.check()
        self.clock.now += 3600
        assert mic.check() == []
        assert "4501:222" in mic._state()

    def test_notify_false_reports_without_sending(self, monkeypatch):
        monkeypatch.setattr(mic, "holders", self.hold)
        mic.check()
        self.clock.now += 301
        assert len(mic.check(notify=False)) == 1
        assert self.notified == []

    def test_a_released_microphone_is_dropped_from_the_state(self, monkeypatch):
        monkeypatch.setattr(mic, "holders", self.hold)
        mic.check()
        monkeypatch.setattr(mic, "holders", list)
        mic.check()
        assert mic._state() == {}


class TestReport:
    """`claude-voice mic`, for a person at a terminal."""

    def test_says_free_when_nothing_holds_it(self, home, monkeypatch, capsys, no_subprocess):
        monkeypatch.setattr(mic, "holders", list)
        assert mic.report() == 0
        assert "free" in capsys.readouterr().out

    def test_names_each_holder_with_the_label_for_its_kind(
        self, home, monkeypatch, capsys, no_subprocess
    ):
        monkeypatch.setattr(
            mic,
            "holders",
            lambda: [
                {"kind": "orphan", "pid": 8801, "name": "pw-record", "key": "8801:555"},
                {"kind": "recording", "pid": 3312, "name": "firefox", "key": "3312:111"},
                {"kind": "parked", "pid": None, "name": "OBS Studio", "key": "OBS Studio"},
            ],
        )
        mic._save({"8801:555": {"first": 1_699_999_000.0}})
        monkeypatch.setattr(mic, "time", Clock(now=1_699_999_600.0))
        mic.report()
        out = capsys.readouterr().out
        assert "pw-record (8801)" in out
        assert "ORPHAN" in out
        assert "for 10m" in out
        # Nothing was aged for the other two, so no age is claimed for them.
        assert "firefox (3312)" in out
        assert "OBS Studio" in out


class TestUnits:
    """The systemd units, generated for the machine they are installed on."""

    def test_prefers_the_console_script(self, monkeypatch):
        monkeypatch.setattr(mic.shutil, "which", lambda name: "/usr/bin/claude-voice")
        assert mic._exec_start() == "/usr/bin/claude-voice mic --once"

    def test_falls_back_to_this_file_in_a_checkout(self, monkeypatch):
        monkeypatch.setattr(mic.shutil, "which", lambda name: None)
        start = mic._exec_start()
        assert start.endswith("mic.py --once")

    def test_the_timer_carries_the_configured_interval(self, monkeypatch):
        monkeypatch.setattr(mic, "CFG", cfg(mic__watch__interval=90))
        monkeypatch.setattr(mic.shutil, "which", lambda name: "/usr/bin/claude-voice")
        service, timer = mic._units()
        assert "ExecStart=/usr/bin/claude-voice mic --once" in service
        # Spelled out rather than inherited: a notification with no bus goes
        # nowhere, and a silent watchdog looks like good news.
        assert "DBUS_SESSION_BUS_ADDRESS" in service
        assert "OnUnitActiveSec=90" in timer
        assert f"Unit={mic.UNIT}.service" in timer


class TestInstall:
    """Writing the units and handing them to systemd."""

    @pytest.fixture
    def fake_home(self, home, monkeypatch):
        h = home / "fake-home"
        h.mkdir()
        monkeypatch.setattr(mic.Path, "home", classmethod(lambda cls: h))
        monkeypatch.setattr(mic.shutil, "which", lambda name: "/usr/bin/claude-voice")
        return h

    def test_writes_both_units_and_enables_the_timer(
        self, fake_home, monkeypatch, capsys, fake_proc
    ):
        calls = []

        def _run(cmd, **kw):
            calls.append(cmd)
            return fake_proc(returncode=0)

        monkeypatch.setattr(mic.subprocess, "run", _run)
        assert mic.install() == 0
        d = fake_home / ".config" / "systemd" / "user"
        assert (d / f"{mic.UNIT}.service").exists()
        assert (d / f"{mic.UNIT}.timer").exists()
        assert calls == [
            ["systemctl", "--user", "daemon-reload"],
            ["systemctl", "--user", "enable", "--now", f"{mic.UNIT}.timer"],
        ]
        assert "installed" in capsys.readouterr().out

    def test_a_systemctl_failure_is_reported_and_stops_there(
        self, fake_home, monkeypatch, capsys, fake_proc
    ):
        monkeypatch.setattr(
            mic.subprocess,
            "run",
            lambda cmd, **kw: fake_proc(returncode=1, stderr="Failed to reload: no such unit\n"),
        )
        assert mic.install() == 1
        assert "Failed to reload" in capsys.readouterr().err

    def test_uninstall_removes_the_units_and_says_so(
        self, fake_home, monkeypatch, capsys, fake_proc
    ):
        calls = []
        monkeypatch.setattr(
            mic.subprocess, "run", lambda cmd, **kw: (calls.append(cmd), fake_proc())[1]
        )
        mic.install()
        calls.clear()
        assert mic.uninstall() == 0
        d = fake_home / ".config" / "systemd" / "user"
        assert not (d / f"{mic.UNIT}.timer").exists()
        assert "disable" in calls[0]
        assert "removed" in capsys.readouterr().out

    def test_uninstalling_twice_is_not_an_error(self, fake_home, monkeypatch, fake_proc):
        monkeypatch.setattr(mic.subprocess, "run", lambda cmd, **kw: fake_proc())
        assert mic.uninstall() == 0


class TestMain:
    """The argument handling, including the gate that turns the watch off."""

    def test_install_and_uninstall_are_delegated(self, monkeypatch, no_subprocess):
        monkeypatch.setattr(mic, "install", lambda: 7)
        monkeypatch.setattr(mic, "uninstall", lambda: 8)
        assert mic.main(["--install"]) == 7
        assert mic.main(["--uninstall"]) == 8

    def test_sweep_reports_what_it_closed(self, monkeypatch, capsys, no_subprocess):
        monkeypatch.setattr(mic, "sweep_orphans", lambda: 1)
        assert mic.main(["--sweep"]) == 0
        assert capsys.readouterr().out.strip() == "closed 1 capture of ours"

    def test_sweep_pluralises_when_there_was_not_exactly_one(
        self, monkeypatch, capsys, no_subprocess
    ):
        monkeypatch.setattr(mic, "sweep_orphans", lambda: 0)
        mic.main(["--sweep"])
        assert capsys.readouterr().out.strip() == "closed 0 captures of ours"

    def test_once_prints_every_overdue_holder(self, monkeypatch, capsys, no_subprocess):
        monkeypatch.setattr(
            mic,
            "CFG",
            cfg(mic__watch__enabled=True),
        )
        monkeypatch.setattr(
            mic,
            "check",
            lambda: [{"kind": "orphan", "pid": 8801, "name": "pw-record", "age": 7860}],
        )
        assert mic.main(["--once"]) == 0
        assert capsys.readouterr().out.strip() == "orphan: pw-record (8801) for 2h11m"

    def test_the_config_gate_silences_once_and_watch(self, monkeypatch, write_config, capsys):
        cfg_off = write_config("[mic.watch]\nenabled = false\n")
        monkeypatch.setattr(mic, "CFG", cfg_off)

        def _never():
            raise AssertionError("check() ran with mic.watch.enabled = false")

        monkeypatch.setattr(mic, "check", _never)
        assert mic.main(["--once"]) == 0
        assert mic.main(["--watch"]) == 0
        assert capsys.readouterr().out == ""

    def test_watch_ticks_on_the_configured_interval(self, monkeypatch, no_subprocess):
        clock = Clock(stop_after=3)
        ticks = []
        monkeypatch.setattr(mic, "time", clock)
        monkeypatch.setattr(mic, "CFG", cfg(mic__watch__interval=45))
        monkeypatch.setattr(mic, "check", lambda: ticks.append(1) or [])
        with pytest.raises(LoopStopped):
            mic.main(["--watch"])
        assert len(ticks) == 3
        assert clock.slept == [45.0, 45.0, 45.0]

    def test_a_failing_tick_does_not_stop_the_watch(self, monkeypatch, capsys, no_subprocess):
        clock = Clock(stop_after=1)
        monkeypatch.setattr(mic, "time", clock)
        monkeypatch.setattr(mic, "CFG", cfg())

        def _boom():
            raise RuntimeError("pw-dump went away")

        monkeypatch.setattr(mic, "check", _boom)
        with pytest.raises(LoopStopped):
            mic.main(["--watch"])
        assert "pw-dump went away" in capsys.readouterr().err

    def test_no_arguments_is_the_report(self, monkeypatch, no_subprocess):
        monkeypatch.setattr(mic, "report", lambda: 42)
        assert mic.main([]) == 42
