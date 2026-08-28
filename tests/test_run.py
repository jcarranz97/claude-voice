"""The wrapper: the registry, the delivery socket, and the pump loop.

Nothing here forks a shell or opens a pty for a real program. ``_spawn`` is
replaced by a socketpair -- a pair of fds that read and write exactly like a
pty master does -- and ``select.select`` by a script, so the loop advances one
deterministic step at a time and never blocks on anything. ``ENTER_DELAY`` goes
to zero for the same reason: the delay is the child's, not the test's.
"""

import json
import os
import socket
import sys

import pytest

import claude_voice.run as run

# --------------------------------------------------------------- stand-ins


class FakeConn:
    """One accepted connection: scripted receives, recorded sends."""

    def __init__(self, chunks=(), send_fails=False):
        self.chunks = list(chunks)
        self.send_fails = send_fails
        self.sent = []
        self.closed = False
        self.timeout = None

    def settimeout(self, t):
        self.timeout = t

    def recv(self, n):
        if not self.chunks:
            return b""
        chunk = self.chunks.pop(0)
        if isinstance(chunk, BaseException):
            raise chunk
        return chunk

    def sendall(self, data):
        if self.send_fails:
            raise OSError("the client went away")
        self.sent.append(data)

    def close(self):
        self.closed = True


class FakeListener:
    """A listening socket whose accept can be made to fail on demand."""

    def __init__(self, conn=None, error=None, close_fails=False):
        self.conn = conn
        self.error = error
        self.close_fails = close_fails
        self.closed = False

    def fileno(self):
        return -1

    def accept(self):
        if self.error:
            raise self.error
        return self.conn, "peer"

    def close(self):
        self.closed = True
        if self.close_fails:
            raise OSError("already gone")


class FakeSocket:
    """``socket.socket`` for the delivery side: records, never connects."""

    made = []

    def __init__(self, family=None, kind=None, reply=b"ok", fail=None):
        self.reply = reply
        self.fail = fail
        self.connected = None
        self.sent = b""
        self.shutdowns = []
        self.closed = False
        FakeSocket.made.append(self)

    def settimeout(self, t):
        self.timeout = t

    def connect(self, path):
        if self.fail:
            raise self.fail
        self.connected = path

    def sendall(self, data):
        self.sent += data

    def shutdown(self, how):
        self.shutdowns.append(how)

    def recv(self, n):
        return self.reply

    def close(self):
        self.closed = True


class _Fd:
    """Something with a file descriptor and nothing else, like sys.stdout."""

    def __init__(self, fd, tty=False):
        self._fd = fd
        self._tty = tty

    def fileno(self):
        return self._fd

    def isatty(self):
        return self._tty


class _Stdin:
    """The wrapper's stdin, asked live: a test may make it a terminal later."""

    def __init__(self, env):
        self._env = env

    def fileno(self):
        return self._env.stdin_fd

    def isatty(self):
        return self._env.tty


class Driver:
    """``select.select``, scripted.

    Each step is a callable taking the watch list. It may act first -- the
    child closing its end, a key arriving -- and returns what select should
    report ready, or an exception to raise instead.
    """

    def __init__(self, steps):
        self.steps = list(steps)
        self.watched = []
        self.timeouts = []

    def __call__(self, watch, writes, errors, timeout):
        self.watched.append(list(watch))
        self.timeouts.append(timeout)
        if not self.steps:
            raise AssertionError("the pump loop outlived its script")
        ready = self.steps.pop(0)(watch)
        if isinstance(ready, BaseException):
            raise ready
        return list(ready), [], []


class Env:
    """Everything ``wrap`` reaches for that is not its own logic."""

    CHILD = 4242

    def __init__(self, home, monkeypatch):
        self.home = home
        self.mp = monkeypatch
        self.events = []
        self.status = 0
        self.spawned = None
        self.hud = True

        # The socket path follows the runtime dir; without one it is BASE,
        # which is the disposable home this test session runs in.
        monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
        monkeypatch.setattr(run, "ENTER_DELAY", 0.0)
        monkeypatch.setattr(run, "ensure_hud", self._ensure_hud)
        monkeypatch.setattr(run, "_spawn", self._spawn)
        monkeypatch.setattr(run.os, "waitpid", lambda pid, flags: (pid, self.status))

        ours, theirs = socket.socketpair()
        self.master = ours.detach()  # the wrapper owns this fd and closes it
        self.child = theirs

        self.sock_path = home / f"{run.PREFIX}{os.getpid()}.sock"
        self.reg_path = home / f"{run.PREFIX}{os.getpid()}.json"
        self.listener = run._listener(self.sock_path)
        self.asked_for = None
        monkeypatch.setattr(run, "_listener", self._make_listener)

        self.stdout_fd = os.open(home / "stdout.bin", os.O_RDWR | os.O_CREAT)
        self.tty = False
        self.stdin_fd = os.open(os.devnull, os.O_RDONLY)
        self.keyboard = None
        self._own = [self.stdin_fd, self.stdout_fd]

        self.handlers = {}
        monkeypatch.setattr(run.signal, "signal", self._signal)

    # -- the pieces the wrapper calls -------------------------------------

    def _ensure_hud(self):
        self.events.append("hud")
        return self.hud

    def _spawn(self, cmd):
        self.events.append("spawn")
        self.spawned = list(cmd)
        return self.CHILD, self.master, "/dev/pts/7"

    def _make_listener(self, path):
        self.asked_for = path
        return self.listener

    def _signal(self, num, handler):
        self.handlers[num] = handler

    # -- what a test sets up ----------------------------------------------

    def use_tty(self):
        """Pretend stdin is a terminal, so keys are pumped and raw mode is set."""
        self.tty = True
        mine, theirs = socket.socketpair()
        self.keyboard, self._stdin_sock = theirs, mine
        self.stdin_fd = mine.fileno()
        self.mp.setattr(run.termios, "tcgetattr", lambda fd: ["the cooked settings"])
        self.mp.setattr(
            run.termios,
            "tcsetattr",
            lambda fd, when, attrs: self.events.append(("restored", attrs)),
        )
        self.mp.setattr(run.tty, "setraw", lambda fd: self.events.append(("raw", fd)))
        return self.keyboard

    def client(self, text=b""):
        """A dictation client, connected before the wrapper starts listening.

        A pending connection sits in the backlog until the loop accepts it,
        which is what makes the delivery deterministic without a thread.
        """
        c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        c.connect(str(self.sock_path))
        if text:
            c.sendall(text)
        c.shutdown(socket.SHUT_WR)
        self._own.append(c)
        return c

    def go(self, *steps, cmd=("claude",)):
        # Late, and not in the fixture: pytest re-installs its own capture on
        # sys.stdout between setup and the test body, which would undo this.
        self.mp.setattr(sys, "stdout", _Fd(self.stdout_fd))
        self.mp.setattr(sys, "stdin", _Stdin(self))
        self.driver = Driver(steps)
        self.mp.setattr(run.select, "select", self.driver)
        return run.wrap(list(cmd))

    # -- steps ------------------------------------------------------------

    def says(self, data):
        """The child writes to its terminal, then that fd is ready."""

        def _step(_watch):
            self.child.sendall(data)
            return [self.master]

        return _step

    def exits(self, _watch=None):
        """The child closes its end: what the loop reads as an exit."""

        def _step(_w):
            self.child.shutdown(socket.SHUT_WR)
            return [self.master]

        return _step

    def ready(self, *what):
        return lambda _watch: [getattr(self, name) for name in what]

    def raises(self, exc):
        return lambda _watch: exc

    # -- afterwards -------------------------------------------------------

    def drained(self) -> bytes:
        """Everything the wrapper wrote to the child. Safe: the fd is closed."""
        out = b""
        while True:
            got = self.child.recv(65536)
            if not got:
                return out
            out += got

    def printed(self) -> bytes:
        os.lseek(self.stdout_fd, 0, os.SEEK_SET)
        return os.read(self.stdout_fd, 65536)

    def close(self):
        for fd in self._own:
            try:
                os.close(fd) if isinstance(fd, int) else fd.close()
            except OSError:
                pass
        for sock in (self.child, self.listener, getattr(self, "_stdin_sock", None)):
            try:
                sock.close()
            except (OSError, AttributeError):
                pass


@pytest.fixture
def env(monkeypatch, home):
    made = Env(home, monkeypatch)
    yield made
    made.close()


# ------------------------------------------------------------------- tests


class TestSocketDirectory:
    """A unix socket path is capped at 108 bytes, so where it lives matters."""

    def test_the_runtime_directory_wins(self, monkeypatch, home):
        rt = home / "runtime"
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(rt))
        assert run._sock_dir() == rt / "claude-voice"
        assert (rt / "claude-voice").is_dir()

    def test_without_one_the_state_directory_stands_in(self, monkeypatch, home):
        monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
        assert run._sock_dir() == run.BASE

    def test_a_runtime_directory_we_cannot_write_falls_back(self, monkeypatch, home):
        monkeypatch.setenv("XDG_RUNTIME_DIR", "/proc/self/nothing-here")
        assert run._sock_dir() == run.BASE


class TestLiveness:
    """A wrapper is believed while its pid is, and swept when it is not."""

    def test_this_process_is_alive(self):
        assert run._alive(os.getpid()) is True

    def test_a_pid_that_is_gone_is_not(self, monkeypatch):
        monkeypatch.setattr(run.os, "kill", _raiser(ProcessLookupError))
        assert run._alive(999999) is False

    def test_somebody_elses_process_still_counts(self, monkeypatch):
        monkeypatch.setattr(run.os, "kill", _raiser(PermissionError))
        assert run._alive(1) is True


def _raiser(exc):
    def _fn(*a, **kw):
        raise exc

    return _fn


class TestSessions:
    """The registry: live wrappers listed, dead ones removed."""

    def _write(self, home, pid, **over):
        sock = home / f"sock-{pid}"
        sock.write_text("")
        data = {"pid": pid, "pty": "/dev/pts/3", "cmd": ["claude"], "cwd": str(home)}
        data.update(sock=str(sock), **over)
        (home / f"{run.PREFIX}{pid}.json").write_text(json.dumps(data))
        return home / f"{run.PREFIX}{pid}.json", sock

    def test_a_registry_entry_with_a_bad_pid_is_skipped(self, home, monkeypatch):
        # The conversion used to sit outside the guard, so one entry with a
        # non-numeric pid raised out of the whole sweep -- and this is what the
        # HUD and `--sessions` both call.
        monkeypatch.setattr(run, "claude_session", lambda pty: {})
        (home / f"{run.PREFIX}bad.json").write_text('{"pid": "abc", "sock": ""}')
        self._write(home, os.getpid())
        assert [s["id"] for s in run.sessions()] == [f"wrap:{os.getpid()}"]

    def test_a_registry_entry_that_is_not_an_object_is_skipped(self, home, monkeypatch):
        monkeypatch.setattr(run, "claude_session", lambda pty: {})
        (home / f"{run.PREFIX}bad.json").write_text('"half a write"')
        self._write(home, os.getpid())
        assert [s["id"] for s in run.sessions()] == [f"wrap:{os.getpid()}"]

    def test_a_live_session_is_described(self, home, monkeypatch):
        monkeypatch.setattr(run, "claude_session", lambda pty: {})
        self._write(home, os.getpid())
        (only,) = run.sessions()
        assert only["id"] == f"wrap:{os.getpid()}"
        assert only["pane_id"] == "pts:/dev/pts/3"
        assert only["title"] == "claude"

    def test_a_dead_session_takes_its_socket_with_it(self, home, monkeypatch):
        monkeypatch.setattr(run, "_alive", lambda pid: False)
        reg, sock = self._write(home, 31337)
        assert run.sessions() == []
        assert not reg.exists()
        assert not sock.exists()

    def test_a_sweep_that_cannot_remove_something_carries_on(self, home, monkeypatch):
        monkeypatch.setattr(run, "_alive", lambda pid: False)
        reg, sock = self._write(home, 31337)
        sock.unlink()
        sock.mkdir()  # a directory where a socket was: unlink will refuse
        assert run.sessions() == []
        assert not reg.exists()

    def test_a_file_that_is_not_json_is_skipped(self, home, monkeypatch):
        monkeypatch.setattr(run, "claude_session", lambda pty: {})
        (home / f"{run.PREFIX}broken.json").write_text("{not json")
        assert run.sessions() == []

    def test_a_state_directory_that_cannot_be_listed_is_no_sessions(self, monkeypatch):
        class _Boom:
            def glob(self, pattern):
                raise OSError("gone")

        monkeypatch.setattr(run, "BASE", _Boom())
        assert run.sessions() == []


class TestClaudeSession:
    """Matched on the pty, because the pid we forked is only the launcher."""

    def _registry(self, home, monkeypatch, entries):
        d = home / "cc-sessions"
        d.mkdir()
        for i, entry in enumerate(entries):
            (d / f"{i}.json").write_text(entry if isinstance(entry, str) else json.dumps(entry))
        monkeypatch.setattr(run, "CC_SESSIONS", d)
        return d

    def test_the_entry_reading_from_that_pty_wins(self, home, monkeypatch):
        self._registry(
            home,
            monkeypatch,
            [
                {"pid": 11, "name": "elsewhere", "sessionId": "a"},
                {"pid": 22, "name": "here", "sessionId": "b"},
            ],
        )
        links = {"/proc/11/fd/0": "/dev/pts/1", "/proc/22/fd/0": "/dev/pts/2"}
        monkeypatch.setattr(run.os, "readlink", lambda p: links[p])
        assert run.claude_session("/dev/pts/2")["name"] == "here"

    def test_a_pty_nobody_is_reading_has_no_session(self, home, monkeypatch):
        self._registry(home, monkeypatch, [{"pid": 11}, {"pid": 12}])
        monkeypatch.setattr(run.os, "readlink", lambda p: "/dev/pts/9")
        assert run.claude_session("/dev/pts/2") == {}

    def test_an_entry_that_is_not_json_is_skipped(self, home, monkeypatch):
        self._registry(home, monkeypatch, ["{oops"])
        assert run.claude_session("/dev/pts/2") == {}

    def test_a_process_that_exited_between_the_two_calls_is_skipped(self, home, monkeypatch):
        self._registry(home, monkeypatch, [{"pid": 11}])
        monkeypatch.setattr(run.os, "readlink", _raiser(OSError))
        assert run.claude_session("/dev/pts/1") == {}

    def test_no_registry_at_all_is_no_answer(self, monkeypatch):
        class _Boom:
            def glob(self, pattern):
                raise OSError("gone")

        monkeypatch.setattr(run, "CC_SESSIONS", _Boom())
        assert run.claude_session("/dev/pts/1") == {}


class TestDescribe:
    """What a wrapped session is called on a HUD row."""

    def test_claude_codes_own_name_wins(self, monkeypatch):
        monkeypatch.setattr(
            run,
            "claude_session",
            lambda pty: {"name": "the voice", "sessionId": "s1", "status": "idle"},
        )
        d = run.describe({"pid": 7, "pty": "/dev/pts/3", "cmd": ["claude"], "cwd": "/tmp/work"})
        assert d["title"] == "the voice"
        assert (d["session"], d["status"]) == ("s1", "idle")
        assert d["dir"] == "work"

    def test_the_command_line_names_a_session_that_has_not_spoken(self, monkeypatch):
        monkeypatch.setattr(run, "claude_session", lambda pty: {})
        d = run.describe({"pid": 7, "cmd": ["claude", "--resume"], "cwd": "/tmp/work"})
        assert d["title"] == "claude --resume"

    def test_a_command_line_is_flattened_and_cut_to_one_row(self, monkeypatch):
        monkeypatch.setattr(run, "claude_session", lambda pty: {})
        d = run.describe({"pid": 7, "cmd": ["say", "one\ntwo", "x" * 200], "cwd": ""})
        assert "\n" not in d["title"]
        assert len(d["title"]) == 60

    def test_nothing_to_go_on_is_still_nameable(self, monkeypatch):
        monkeypatch.setattr(run, "claude_session", lambda pty: {})
        assert run.describe({"pid": 7})["title"] == "(untitled)"


class TestDeliver:
    """One line, handed to a wrapped session over its socket."""

    @pytest.fixture(autouse=True)
    def _fake_sockets(self, monkeypatch):
        FakeSocket.made = []
        monkeypatch.setattr(run.socket, "socket", FakeSocket)

    def test_a_line_it_took_is_true(self):
        assert run.deliver({"sock": "/run/x.sock"}, "  open   the file \n") is True
        (sock,) = FakeSocket.made
        assert sock.connected == "/run/x.sock"
        assert sock.sent == b"open the file"  # collapsed, and no newline
        assert sock.closed

    def test_a_refusal_is_false(self, monkeypatch):
        monkeypatch.setattr(run.socket, "socket", lambda *a: FakeSocket(reply=b"no"))
        assert run.deliver({"sock": "/run/x.sock"}, "hello") is False

    def test_nothing_to_say_is_not_delivered(self):
        assert run.deliver({"sock": "/run/x.sock"}, "   \n ") is False
        assert FakeSocket.made == []

    def test_a_socket_that_will_not_connect_is_false(self, monkeypatch):
        monkeypatch.setattr(
            run.socket, "socket", lambda *a: FakeSocket(fail=ConnectionRefusedError())
        )
        assert run.deliver({"sock": "/run/x.sock"}, "hello") is False

    def test_a_socket_that_cannot_even_be_made_is_false(self, monkeypatch):
        monkeypatch.setattr(run.socket, "socket", _raiser(OSError("no fds")))
        assert run.deliver({"sock": "/run/x.sock"}, "hello") is False


class TestTheWindow:
    """One HUD serves every session, so the wrapper starts at most one."""

    def test_presence_answers_whether_one_is_up(self, monkeypatch):
        monkeypatch.setattr(run._presence, "windows", lambda: [123])
        assert run._hud_is_up() is True
        monkeypatch.setattr(run._presence, "windows", lambda: [])
        assert run._hud_is_up() is False

    def test_a_presence_check_that_breaks_is_no_window(self, monkeypatch):
        monkeypatch.setattr(run._presence, "windows", _raiser(OSError))
        assert run._hud_is_up() is False

    def test_autostart_off_reports_without_starting_anything(self, monkeypatch, no_subprocess):
        # The wrapper's own copy of the config module, which is the one it asks.
        off = run._config.Config({"hud": {"autostart": False}})
        monkeypatch.setattr(run._config, "load", lambda *a, **kw: off)
        monkeypatch.setattr(run, "_hud_is_up", lambda: False)
        assert run.ensure_hud() is False

    def test_a_window_already_open_is_left_alone(self, monkeypatch, no_subprocess):
        monkeypatch.setattr(run, "_hud_is_up", lambda: True)
        assert run.ensure_hud() is True

    def test_one_is_started_and_waited_for(self, monkeypatch):
        started = []
        monkeypatch.setattr(run.subprocess, "Popen", lambda cmd, **kw: started.append((cmd, kw)))
        # Closed when asked the first time, open by the time the loop asks:
        # the wait is the point, since a session that wins that race is mute.
        answers = iter([False, True])
        monkeypatch.setattr(run, "_hud_is_up", lambda: next(answers))
        assert run.ensure_hud() is True
        ((cmd, kw),) = started
        assert cmd[0] == sys.executable and cmd[1].endswith("hudweb.py")
        assert kw["start_new_session"] is True

    def test_the_wait_gives_it_a_moment_before_asking_again(self, monkeypatch):
        naps = []
        monkeypatch.setattr(run.time, "sleep", naps.append)  # no real waiting
        monkeypatch.setattr(run.subprocess, "Popen", lambda cmd, **kw: None)
        answers = iter([False, False, True])
        monkeypatch.setattr(run, "_hud_is_up", lambda: next(answers))
        assert run.ensure_hud() is True
        assert naps == [0.1]

    def test_a_window_that_never_comes_up_is_reported(self, monkeypatch):
        monkeypatch.setattr(run, "HUD_WAIT", 0.0)  # no waiting in a unit test
        monkeypatch.setattr(run.subprocess, "Popen", lambda cmd, **kw: None)
        monkeypatch.setattr(run, "_hud_is_up", lambda: False)
        assert run.ensure_hud() is False

    def test_a_window_that_will_not_start_is_reported(self, monkeypatch):
        monkeypatch.setattr(run.subprocess, "Popen", _raiser(FileNotFoundError))
        monkeypatch.setattr(run, "_hud_is_up", lambda: False)
        assert run.ensure_hud() is False


class TestTerminalSize:
    def test_the_size_is_asked_of_the_terminal(self, monkeypatch):
        monkeypatch.setattr(
            run.fcntl, "ioctl", lambda fd, req, buf: b"\x01\x02\x03\x04\x05\x06\x07\x08"
        )
        assert run._winsize(0) == b"\x01\x02\x03\x04\x05\x06\x07\x08"

    def test_no_terminal_gets_the_classic_eighty_by_twenty_four(self):
        with open(os.devnull) as f:
            assert run._winsize(f.fileno()) == run.struct.pack("HHHH", 24, 80, 0, 0)


class TestSpawn:
    """The fork itself, with the fork replaced: both sides are exercised."""

    @pytest.fixture
    def quiet(self, monkeypatch):
        """Every call the child branch makes, neutered. It runs in-process."""
        events = []
        for name in ("setsid", "dup2", "close", "write"):
            monkeypatch.setattr(run.os, name, lambda *a, _n=name: events.append((_n, a)))
        monkeypatch.setattr(run.fcntl, "ioctl", lambda *a: events.append(("ioctl", a)))
        monkeypatch.setattr(run.os, "_exit", _raiser(SystemExit))
        monkeypatch.setattr(run.os, "execvp", lambda f, a: events.append(("execvp", f, a)))
        monkeypatch.setattr(sys, "stdin", _Fd(0))
        return events

    def test_the_parent_gets_the_pid_the_master_and_the_pty_name(self, monkeypatch):
        monkeypatch.setattr(sys, "stdin", _Fd(0))
        monkeypatch.setattr(run.os, "fork", lambda: 99)
        pid, master, name = run._spawn(["claude"])
        assert pid == 99
        assert name.startswith("/dev/pts/")
        os.close(master)

    def test_a_terminal_hands_its_size_to_the_new_pty(self, monkeypatch):
        sizes = []
        monkeypatch.setattr(sys, "stdin", _Fd(0, tty=True))
        monkeypatch.setattr(run.fcntl, "ioctl", lambda fd, req, buf=b"": sizes.append(req) or b"")
        monkeypatch.setattr(run.os, "fork", lambda: 99)
        _, master, _ = run._spawn(["claude"])
        assert run.termios.TIOCSWINSZ in sizes
        os.close(master)

    def test_a_terminal_that_will_not_say_its_size_is_not_fatal(self, monkeypatch):
        monkeypatch.setattr(sys, "stdin", _Fd(0, tty=True))
        monkeypatch.setattr(run.fcntl, "ioctl", _raiser(OSError("not a tty")))
        monkeypatch.setattr(run.os, "fork", lambda: 99)
        pid, master, _ = run._spawn(["claude"])
        assert pid == 99
        os.close(master)

    def test_the_child_takes_the_pty_as_its_controlling_terminal(self, monkeypatch, quiet):
        quiet.clear()  # the capture machinery dup2s around a test too
        monkeypatch.setattr(run.os, "fork", lambda: 0)
        with pytest.raises(SystemExit):
            run._spawn(["claude", "--resume"])
        assert ("execvp", "claude", ["claude", "--resume"]) in quiet
        assert sum(1 for e in quiet if e[0] == "dup2") == 3

    def test_a_command_that_does_not_exist_says_so_on_the_pty(self, monkeypatch, quiet):
        quiet.clear()
        monkeypatch.setattr(run.os, "fork", lambda: 0)
        monkeypatch.setattr(run.os, "execvp", _raiser(FileNotFoundError("nope")))
        with pytest.raises(SystemExit):
            run._spawn(["nosuchthing"])
        (written,) = [e for e in quiet if e[0] == "write"]
        assert b"claude-voice run: nosuchthing" in written[1][1]


class TestListener:
    """The way in: one socket per wrapper, readable only by its owner."""

    def test_it_binds_and_is_private(self, home):
        path = home / "one.sock"
        s = run._listener(path)
        try:
            assert path.exists()
            assert oct(path.stat().st_mode)[-3:] == "600"
        finally:
            s.close()

    def test_a_socket_left_behind_is_replaced(self, home):
        path = home / "two.sock"
        path.write_text("a stale file")
        s = run._listener(path)
        s.close()


class TestTyped:
    """One delivery off the socket, whitespace and all."""

    def test_the_chunks_are_joined_and_collapsed(self):
        conn = FakeConn([b"open  the", b" \n file"])
        assert run._typed(conn) == "open the file"
        assert conn.timeout == 2

    def test_a_read_that_breaks_keeps_what_arrived(self):
        conn = FakeConn([b"half", OSError("reset")])
        assert run._typed(conn) == "half"

    def test_bytes_that_are_not_utf8_are_replaced_not_raised(self):
        assert run._typed(FakeConn([b"caf\xe9"])) == "caf�"


class TestWrap:
    """The pump loop, one scripted step at a time."""

    def test_the_window_opens_before_the_child_is_spawned(self, env):
        assert env.go(env.exits()) == 0
        assert env.events[:2] == ["hud", "spawn"]
        assert env.spawned == ["claude"]

    def test_the_registry_names_the_session_while_it_runs(self, env):
        seen = {}

        def peek(_watch):
            seen.update(json.loads(env.reg_path.read_text()))
            return []  # nothing ready: one turn of the loop, doing nothing

        env.go(peek, env.exits(), cmd=("claude", "--resume"))
        assert seen["pid"] == os.getpid()
        assert seen["child"] == env.CHILD
        assert seen["pty"] == "/dev/pts/7"
        assert seen["cmd"] == ["claude", "--resume"]
        assert seen["sock"] == str(env.sock_path)
        assert env.asked_for == env.sock_path

    def test_teardown_leaves_no_registry_and_no_socket(self, env):
        env.go(env.exits())
        assert not env.reg_path.exists()
        assert not env.sock_path.exists()

    def test_what_the_child_writes_reaches_the_terminal(self, env):
        env.go(env.says(b"welcome\r\n"), env.exits())
        assert env.printed() == b"welcome\r\n"

    def test_a_dictated_line_is_typed_and_then_entered(self, env):
        client = env.client(b"open  the\nfile")

        def deliver(_watch):
            return [env.listener]

        assert env.go(deliver, env.exits()) == 0
        assert client.recv(16) == b"ok"
        # Collapsed to one line, and the Enter arrives on its own afterwards:
        # a newline in the same write would be taken as a literal one.
        assert env.drained() == b"open the file\r"

    def test_a_blank_delivery_is_refused_and_nothing_is_typed(self, env):
        client = env.client(b"   \n\t ")
        assert env.go(lambda _w: [env.listener], env.exits()) == 0
        assert client.recv(16) == b"no"
        assert env.drained() == b""

    def test_keys_typed_reach_the_child_and_the_terminal_is_restored(self, env):
        keyboard = env.use_tty()
        keyboard.sendall(b"hello")
        env.go(env.ready("stdin_fd"), env.exits())
        assert env.drained() == b"hello"
        assert ("raw", env.stdin_fd) in env.events
        assert ("restored", ["the cooked settings"]) in env.events

    def test_a_resize_is_passed_on_to_the_child(self, env, monkeypatch):
        signalled = []
        monkeypatch.setattr(run.fcntl, "ioctl", lambda *a: b"\0" * 8)
        monkeypatch.setattr(run.os, "kill", lambda pid, sig: signalled.append((pid, sig)))

        def resize(_watch):
            env.handlers[run.signal.SIGWINCH]()
            return []

        env.go(resize, env.exits())
        assert signalled == [(env.CHILD, run.signal.SIGWINCH)]

    def test_a_resize_that_the_child_will_not_take_is_not_fatal(self, env, monkeypatch):
        monkeypatch.setattr(run.os, "kill", _raiser(ProcessLookupError))

        def resize(_watch):
            env.handlers[run.signal.SIGWINCH]()
            return []

        assert env.go(resize, env.exits()) == 0

    def test_a_terminal_that_refuses_a_handler_is_not_fatal(self, env, monkeypatch):
        monkeypatch.setattr(run.signal, "signal", _raiser(ValueError("not the main thread")))
        assert env.go(env.exits()) == 0

    def test_an_interrupted_select_is_not_an_error(self, env):
        assert env.go(env.raises(InterruptedError()), env.exits()) == 0
        assert len(env.driver.watched) == 2

    def test_a_broken_select_ends_the_loop(self, env):
        assert env.go(env.raises(OSError("bad fd"))) == 0

    def test_a_read_that_fails_is_treated_as_nothing(self, env, monkeypatch):
        # Both ends, one at a time: the keyboard first, so that the loop is
        # still running when the child's own fd goes the same way.
        env.use_tty()
        monkeypatch.setattr(run.os, "read", _raiser(OSError("gone")))
        assert env.go(env.ready("stdin_fd"), env.ready("master")) == 0
        assert env.drained() == b""

    def test_an_enter_the_child_never_receives_is_not_fatal(self, env, monkeypatch):
        real_write = os.write

        def refuse_the_enter(fd, data):
            if data == b"\r":
                raise OSError("the child went away")
            return real_write(fd, data)

        monkeypatch.setattr(run.os, "write", refuse_the_enter)
        client = env.client(b"hello")
        assert env.go(lambda _w: [env.listener], env.exits()) == 0
        assert client.recv(16) == b"ok"

    def test_a_terminal_that_will_not_go_back_to_cooked_is_not_fatal(self, env, monkeypatch):
        env.use_tty()
        monkeypatch.setattr(run.termios, "tcsetattr", _raiser(run.termios.error("gone")))
        assert env.go(env.exits()) == 0

    def test_state_that_will_not_be_swept_is_not_fatal(self, env, monkeypatch):
        monkeypatch.setattr(run.Path, "unlink", _raiser(PermissionError))
        assert env.go(env.exits()) == 0

    def test_a_listener_that_will_not_close_is_not_fatal(self, env):
        env.listener = FakeListener(close_fails=True)
        assert env.go(env.exits()) == 0

    def test_an_accept_that_fails_is_ignored(self, env, monkeypatch):
        env.listener = FakeListener(error=OSError("connection reset"))
        assert env.go(lambda _w: [env.listener], env.exits()) == 0

    def test_a_delivery_that_explodes_is_swallowed(self, env):
        conn = FakeConn([b"hello"], send_fails=True)
        env.listener = FakeListener(conn=conn)
        assert env.go(lambda _w: [env.listener], env.exits()) == 0
        assert conn.closed

    def test_the_exit_status_is_the_childs(self, env):
        env.status = 3 << 8  # exit code 3, as waitpid encodes it
        assert env.go(env.exits()) == 3

    def test_a_signalled_child_reports_a_shell_style_status(self, env):
        env.status = 9  # killed, no core
        assert env.go(env.exits()) == 137


class TestMain:
    """The words the wrapper answers to before the command line is the child's."""

    @pytest.fixture
    def wrapped(self, monkeypatch):
        seen = []
        monkeypatch.setattr(run, "wrap", lambda cmd: seen.append(cmd) or 0)
        return seen

    def test_no_arguments_means_claude(self, wrapped):
        assert run.main([]) == 0
        assert wrapped == [["claude"]]

    def test_a_leading_flag_is_claudes(self, wrapped):
        run.main(["--resume", "-c"])
        assert wrapped == [["claude", "--resume", "-c"]]

    def test_a_double_dash_is_accepted_and_dropped(self, wrapped):
        run.main(["--", "htop"])
        assert wrapped == [["htop"]]

    def test_any_command_may_be_wrapped(self, wrapped):
        run.main(["bash", "-l"])
        assert wrapped == [["bash", "-l"]]

    def test_sessions_prints_json_and_wraps_nothing(self, monkeypatch, capsys, wrapped):
        monkeypatch.setattr(run, "sessions", lambda: [{"id": "wrap:7"}])
        assert run.main(["--sessions"]) == 0
        assert json.loads(capsys.readouterr().out) == [{"id": "wrap:7"}]
        assert wrapped == []
