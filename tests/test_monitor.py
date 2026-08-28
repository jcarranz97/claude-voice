"""What has the microphone and the speakers, ours or anybody's.

The module answers from the machine's side -- ``/proc`` and ``pw-dump`` -- so
both are replaced here: a directory we built stands in for ``/proc``, and
captured text stands in for the tool. Note that ``monitor`` imports its
siblings by bare name, so ``monitor._mic`` is a second copy of ``mic``: it is
that copy which has to be patched, not ``claude_voice.mic``.
"""

import json
import os
from pathlib import Path

import pytest

import claude_voice.monitor as monitor

# A pw-dump with both directions on it: Firefox playing a video, our own aplay
# on the queue, and one input stream so the two classes can be told apart.
PW_DUMP = """[
  {
    "id": 39,
    "type": "PipeWire:Interface:Client",
    "info": {
      "props": {"application.name": "Firefox", "pipewire.sec.pid": 3312}
    }
  },
  {
    "id": 44,
    "type": "PipeWire:Interface:Client",
    "info": {
      "props": {"application.name": "aplay", "pipewire.sec.pid": 5150}
    }
  },
  {
    "id": 52,
    "type": "PipeWire:Interface:Node",
    "info": {
      "state": "running",
      "props": {
        "media.class": "Stream/Output/Audio",
        "client.id": 39,
        "media.name": "Bandcamp — a very long album title",
        "application.name": "Firefox"
      }
    }
  },
  {
    "id": 58,
    "type": "PipeWire:Interface:Node",
    "info": {
      "state": "running",
      "props": {
        "media.class": "Stream/Output/Audio",
        "client.id": 44,
        "media.name": "ALSA Playback",
        "application.name": "aplay"
      }
    }
  },
  {
    "id": 63,
    "type": "PipeWire:Interface:Node",
    "info": {
      "state": "idle",
      "props": {
        "media.class": "Stream/Output/Audio",
        "client.id": 44,
        "media.name": "ALSA Playback"
      }
    }
  },
  {
    "id": 66,
    "type": "PipeWire:Interface:Node",
    "info": {
      "state": "running",
      "props": {
        "media.class": "Stream/Input/Audio",
        "client.id": 39,
        "media.name": "AudioStream"
      }
    }
  }
]
"""

STAT = (
    "{pid} ({comm}) S 1 {pid} {pid} 0 -1 4194560 1180 0 0 0 43 12 0 0 "
    "20 0 4 0 {start} 312664064 2604 18446744073709551615 1 1 0 0 0 0 0 "
    "4096 0 0 0 0 17 6 0 0 0 0 0 0 0 0 0 0 0 0 0"
)

TICK = os.sysconf("SC_CLK_TCK")


def make_proc(root, pid, comm, cmdline=(), start=None):
    d = root / str(pid)
    d.mkdir(parents=True, exist_ok=True)
    (d / "comm").write_text(comm + "\n")
    (d / "cmdline").write_bytes(b"".join(a.encode() + b"\0" for a in cmdline))
    if start is not None:
        (d / "stat").write_text(STAT.format(pid=pid, comm=comm, start=start))
    return d


def redirect_proc(monkeypatch, root, *modules):
    """Point every ``Path("/proc")`` inside *modules* at *root*."""

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
    """A stand-in for ``time``, so ``--watch`` never waits on the real one."""

    def __init__(self, interrupt_after=1):
        self.slept = []
        self.interrupt_after = interrupt_after

    def sleep(self, secs):
        self.slept.append(secs)
        if len(self.slept) >= self.interrupt_after:
            raise KeyboardInterrupt


@pytest.fixture
def proc(home, monkeypatch):
    """A stand-in ``/proc``, wired into the module and into its copy of mic."""
    root = home / "proc"
    root.mkdir()
    (root / "uptime").write_text("30000.11 118422.35\n")
    redirect_proc(monkeypatch, root, monitor, monitor._mic)
    return root


def pw_dump(monkeypatch, fake_proc, text):
    monkeypatch.setattr(monitor.subprocess, "run", lambda *a, **kw: fake_proc(stdout=text))


class TestAge:
    """How long a process has been up, from the kernel rather than from us."""

    def test_counts_from_the_kernel_start_time(self, proc):
        make_proc(proc, 3312, "firefox", start=int(600 * TICK))
        assert monitor._age(3312) == pytest.approx(30000.11 - 600)

    def test_a_process_we_cannot_read_has_no_age(self, proc):
        assert monitor._age(9999) == 0.0

    def test_a_start_time_after_the_uptime_never_goes_negative(self, proc):
        # Clock skew rather than a time traveller, but the subtraction is the
        # same, and a negative age would format as nonsense.
        make_proc(proc, 3312, "firefox", start=int(90000 * TICK))
        assert monitor._age(3312) == 0.0


class TestPwStreams:
    """One class of stream at a time, with the process behind it."""

    def test_names_the_output_streams_and_their_processes(self, proc, monkeypatch, fake_proc):
        make_proc(proc, 3312, "firefox")
        make_proc(proc, 5150, "aplay")
        pw_dump(monkeypatch, fake_proc, PW_DUMP)
        streams = monitor._pw_streams("Stream/Output/Audio")
        assert [(s["pid"], s["name"], s["running"]) for s in streams] == [
            (3312, "firefox", True),
            (5150, "aplay", True),
            (5150, "aplay", False),
        ]
        assert streams[0]["detail"] == "Bandcamp — a very long album title"

    def test_the_input_streams_are_a_different_question(self, proc, monkeypatch, fake_proc):
        make_proc(proc, 3312, "firefox")
        pw_dump(monkeypatch, fake_proc, PW_DUMP)
        assert [s["pid"] for s in monitor._pw_streams("Stream/Input/Audio")] == [3312]

    def test_a_stream_whose_process_is_gone_is_residue(self, proc, monkeypatch, fake_proc):
        pw_dump(monkeypatch, fake_proc, PW_DUMP)
        assert monitor._pw_streams("Stream/Output/Audio") == []

    def test_a_stream_with_no_process_keeps_its_own_name(self, proc, monkeypatch, fake_proc):
        dump = [
            {
                "id": 58,
                "type": "PipeWire:Interface:Node",
                "info": {
                    "state": "running",
                    "props": {
                        "media.class": "Stream/Output/Audio",
                        "application.name": "Spotify",
                    },
                },
            }
        ]
        pw_dump(monkeypatch, fake_proc, json.dumps(dump))
        assert monitor._pw_streams("Stream/Output/Audio") == [
            {"pid": None, "running": True, "name": "Spotify", "detail": "Spotify"}
        ]

    def test_a_nameless_stream_is_a_question_mark(self, proc, monkeypatch, fake_proc):
        dump = [
            {
                "id": 58,
                "type": "PipeWire:Interface:Node",
                "info": {"state": "idle", "props": {"media.class": "Stream/Output/Audio"}},
            }
        ]
        pw_dump(monkeypatch, fake_proc, json.dumps(dump))
        assert monitor._pw_streams("Stream/Output/Audio")[0]["name"] == "?"

    def test_without_pw_dump_there_are_no_streams(self, proc, monkeypatch):
        def _missing(*a, **kw):
            raise FileNotFoundError("pw-dump")

        monkeypatch.setattr(monitor.subprocess, "run", _missing)
        assert monitor._pw_streams("Stream/Output/Audio") == []

    def test_a_truncated_dump_is_no_streams_rather_than_a_crash(self, proc, monkeypatch, fake_proc):
        pw_dump(monkeypatch, fake_proc, '[{"id": 58, "type": "PipeWire:Inter')
        assert monitor._pw_streams("Stream/Output/Audio") == []


class TestIsOurs:
    """Asked of /proc at this instant, because the players die that fast."""

    def test_a_module_of_ours_is_ours(self, proc):
        make_proc(proc, 6001, "python3", ["python3", f"{monitor.HERE}/speak.py"])
        assert monitor._is_ours(6001) is True

    def test_a_player_pointed_at_our_queue_is_ours(self, proc, home):
        make_proc(proc, 6002, "aplay", ["aplay", "-q", f"{home}/queue/00000007.wav"])
        assert monitor._is_ours(6002) is True

    def test_somebody_elses_player_is_not(self, proc):
        make_proc(proc, 6003, "aplay", ["aplay", "-q", "/home/someone/ring.wav"])
        assert monitor._is_ours(6003) is False

    def test_a_kernel_thread_has_no_command_line(self, proc):
        make_proc(proc, 6004, "kworker/0:1")
        assert monitor._is_ours(6004) is False

    def test_a_process_that_is_gone_is_not_ours(self, proc):
        assert monitor._is_ours(9999) is False


class TestOurPids:
    """What is actually running, by command line rather than by pidfile."""

    def test_names_each_module_by_what_it_is_for(self, proc):
        make_proc(proc, 6001, "python3", ["python3", f"{monitor.HERE}/hud.py"])
        make_proc(proc, 6002, "python3", ["python3", f"{monitor.HERE}/listen.py"])
        assert monitor.our_pids() == {
            6001: ("the window", "hud.py"),
            6002: ("conversation mode · microphone open", "listen.py"),
        }

    def test_a_player_on_our_queue_counts_as_ours(self, proc, home):
        make_proc(proc, 6003, "aplay", ["aplay", "-q", f"{home}/queue/00000007.wav"])
        assert monitor.our_pids() == {6003: ("playing a line", "aplay")}

    def test_something_of_ours_with_no_entry_is_named_by_its_command(self, proc, home):
        make_proc(proc, 6004, "mpv", ["mpv", f"{home}/queue/00000007.wav"])
        assert monitor.our_pids() == {6004: ("mpv", "mpv")}

    def test_a_process_whose_comm_is_gone_is_a_question_mark(self, proc, home):
        d = proc / "6005"
        d.mkdir()
        (d / "cmdline").write_bytes(f"mpv\0{home}/queue/1.wav\0".encode())
        assert monitor.our_pids() == {6005: ("?", "?")}

    def test_it_never_lists_itself(self, proc):
        make_proc(proc, os.getpid(), "python3", ["python3", f"{monitor.HERE}/monitor.py"])
        assert monitor.our_pids() == {}

    def test_it_ignores_everything_that_is_not_ours(self, proc):
        (proc / "self").mkdir()
        (proc / "meminfo").write_text("MemTotal: 16316688 kB\n")
        make_proc(proc, 6006, "firefox", ["firefox"])
        make_proc(proc, 6007, "kworker/1:2")
        (proc / "6008").mkdir()  # exited between the listing and the read
        assert monitor.our_pids() == {}


class TestLine:
    def test_an_age_of_zero_leaves_no_trailing_column(self):
        assert monitor._line("●", "aplay", "playing a line", 0) == (
            "  ● aplay            playing a line"
        )

    def test_the_age_is_the_last_column(self):
        assert monitor._line("⚠", "pw-record", "ORPHAN", 7860).endswith("2h11m")


class TestReport:
    """The three sections, and the one combination worth calling out."""

    @pytest.fixture(autouse=True)
    def wired(self, home, monkeypatch):
        monkeypatch.setattr(monitor, "_age", lambda pid: 0.0)
        monkeypatch.setattr(monitor, "our_pids", dict)
        monkeypatch.setattr(monitor, "_pw_streams", lambda cls: [])
        monkeypatch.setattr(monitor._mic, "holders", list)
        monkeypatch.setattr(monitor._mic, "our_captures", list)
        monkeypatch.setattr(monitor._presence, "windows", list)
        monkeypatch.setattr(monitor._presence, "required", lambda: True)

    def test_a_quiet_machine_says_so_three_times(self, capsys):
        monitor.report()
        out = capsys.readouterr().out
        assert "free — nothing has the microphone open" in out
        assert "idle — nothing is playing" in out
        assert "nothing running" in out

    def test_it_mentions_when_a_window_is_not_required(self, monkeypatch, capsys):
        monkeypatch.setattr(monitor._presence, "required", lambda: False)
        monitor.report()
        assert "(a window is not required)" in capsys.readouterr().out

    def test_a_capture_of_ours_is_enough_to_not_be_nothing(self, monkeypatch, capsys):
        monkeypatch.setattr(monitor._mic, "our_captures", lambda: [8801])
        monitor.report()
        assert "nothing running" not in capsys.readouterr().out

    def test_each_kind_of_holder_gets_its_own_mark(self, monkeypatch, capsys):
        monkeypatch.setattr(
            monitor._mic,
            "holders",
            lambda: [
                {"kind": "orphan", "pid": 8801, "name": "pw-record"},
                {"kind": "ours", "pid": 8802, "name": "pw-record"},
                {"kind": "recording", "pid": 3312, "name": "firefox"},
                {"kind": "parked", "pid": None, "name": None},
            ],
        )
        monitor.report()
        lines = capsys.readouterr().out.splitlines()
        assert "⚠ pw-record" in lines[1] and "ORPHAN, nothing owns it" in lines[1]
        assert "● pw-record" in lines[2] and "conversation mode" in lines[2]
        assert "● firefox" in lines[3] and "recording" in lines[3]
        # No name and no pid: still a claim, still drawn.
        assert "◦ ?" in lines[4] and "open, not recording" in lines[4]

    def test_only_the_running_output_streams_are_speakers(self, monkeypatch, capsys):
        monkeypatch.setattr(
            monitor,
            "_pw_streams",
            lambda cls: [
                {"pid": 5150, "running": True, "name": "aplay", "detail": "ALSA Playback"},
                {"pid": 3312, "running": True, "name": "firefox", "detail": "Bandcamp"},
                {"pid": None, "running": True, "name": "Spotify", "detail": ""},
                {"pid": 7000, "running": False, "name": "vlc", "detail": "paused"},
            ],
        )
        monkeypatch.setattr(monitor, "_is_ours", lambda pid: pid == 5150)
        monitor.report()
        out = capsys.readouterr().out
        assert "claude-voice · speaking" in out
        assert "Bandcamp" in out
        # No detail to show, so the fact that it is playing is the detail.
        assert "Spotify" in out and "playing" in out
        assert "vlc" not in out

    def test_our_processes_are_listed_oldest_first(self, monkeypatch, capsys):
        monkeypatch.setattr(
            monitor, "our_pids", lambda: {6001: ("the window", "hud.py"), 6002: ("mpv", "mpv")}
        )
        monkeypatch.setattr(monitor, "_age", lambda pid: 10.0 if pid == 6002 else 300.0)
        monkeypatch.setattr(monitor._presence, "windows", lambda: [6001])
        monitor.report()
        lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("  ●")]
        assert "hud.py" in lines[0]
        assert "the window (1 open)" in lines[0]
        assert "mpv" in lines[1]

    def test_things_of_ours_with_no_window_behind_them_are_called_out(self, monkeypatch, capsys):
        monkeypatch.setattr(monitor, "our_pids", lambda: {6002: ("the heartbeat", "thinking.py")})
        monitor.report()
        assert "no window is open" in capsys.readouterr().out

    def test_no_warning_when_a_window_is_not_required(self, monkeypatch, capsys):
        monkeypatch.setattr(monitor._presence, "required", lambda: False)
        monkeypatch.setattr(monitor, "our_pids", lambda: {6002: ("the heartbeat", "thinking.py")})
        monitor.report()
        assert "no window is open" not in capsys.readouterr().out


class TestMain:
    @pytest.fixture(autouse=True)
    def quiet(self, home, monkeypatch):
        self.reports = []
        monkeypatch.setattr(monitor, "report", lambda: self.reports.append(1))

    def test_no_arguments_reports_once(self, monkeypatch):
        monkeypatch.setattr(monitor.sys, "argv", ["monitor.py"])
        assert monitor.main() == 0
        assert len(self.reports) == 1

    def test_watch_repeats_until_interrupted(self, monkeypatch, capsys):
        clock = Clock(interrupt_after=3)
        monkeypatch.setattr(monitor, "time", clock)
        monkeypatch.setattr(monitor.sys, "argv", ["monitor.py", "--watch", "0.5"])
        assert monitor.main() == 0
        assert len(self.reports) == 3
        assert clock.slept == [0.5, 0.5, 0.5]
        assert "every 0.5s" in capsys.readouterr().out

    def test_watch_defaults_to_two_seconds(self, monkeypatch):
        clock = Clock()
        monkeypatch.setattr(monitor, "time", clock)
        monkeypatch.setattr(monitor.sys, "argv", ["monitor.py", "--watch"])
        monitor.main()
        assert clock.slept == [2.0]

    def test_an_interval_that_is_not_a_number_falls_back(self, monkeypatch):
        clock = Clock()
        monkeypatch.setattr(monitor, "time", clock)
        monkeypatch.setattr(monitor.sys, "argv", ["monitor.py", "--watch", "soon"])
        monitor.main()
        assert clock.slept == [2.0]
