#!/usr/bin/env python3
"""Run a session inside a pty we own, so dictation has somewhere to type.

  run.py [command ...]        wrap that command, `claude` if none is given
  run.py --resume             a leading flag is claude's, not ours
  run.py --sessions           the wrapped sessions that are live, as JSON

Why a wrapper
-------------
There is no way into a session that is already running. Its stdin belongs to
the terminal emulator, which holds the pty master; `/dev/pts/N` is the slave
and writing there paints the screen instead of feeding the program. TIOCSTI
was the old answer and the kernel disabled it in 6.2 -- and even alive it only
ever reached the caller's OWN controlling terminal, which a dictation process
never is. Everything else that types (ydotool, wtype, xdotool) aims at
whichever window has focus, which is not a session and cannot be checked.

So the text has to arrive from something that was there at launch. This forks
the real command onto a pty it holds the master of, and pumps bytes both ways.
Writing into that master is indistinguishable from typing, because it is the
same file the keyboard's bytes travel down. tmux does exactly this and asks
the user to live in tmux; this asks for one word on the command line.

Arguments are not parsed
------------------------
Everything after the verb is the child's, handed to execvp untouched, so
`--model`, `--resume`, `-c` and anything Claude Code grows later work without
this file knowing they exist. That is also why this command has no flags of
its own: one would collide the day the child grew the same name. What the
wrapper needs to be told comes from the config file, never from argv.

The command need not be claude. Whatever runs here gets the ear, which is the
whole reason the ear is not built on Claude Code's own plumbing.

Safety
------
Delivery TYPES INTO A TERMINAL, so a bad transcription in a shell would run as
a command. tmux delivery checks `pane_current_command` for this. Here the
guarantee is different and stronger about identity: the socket only ever
reaches the process THIS wrapper started, named in the registry file next to
it. A newline is never delivered -- it would submit half a sentence -- so the
Enter is ours to send, once, after the text has landed.
"""

import fcntl
import json
import os
import pty
import select
import signal
import socket
import struct
import subprocess
import sys
import termios
import time
import tty
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import config as _config  # noqa: E402
import presence as _presence  # noqa: E402

BASE = _config.BASE
PREFIX = "run-"

# Claude Code's own registry of live sessions: one file per session, removed
# when it exits. It carries the session uuid, the cwd and the derived
# conversation name -- everything the tmux path reconstructs by scraping pane
# titles, available identically outside tmux, and available immediately rather
# than only once the window has been titled.
CC_SESSIONS = Path.home() / ".claude" / "sessions"

# Long enough for the TUI to have taken the text before the newline arrives.
# Claude Code treats a newline that lands in the same read as a literal one,
# which puts the sentence in the box and never sends it.
ENTER_DELAY = 0.15

# The window is the application: without one open, nothing of ours speaks. A
# session started here would come up mute, so the wrapper opens one first --
# and only if none is up, because one HUD serves every session.
HUD_WAIT = 6.0


def _sock_dir() -> Path:
    """Where the sockets live: the runtime dir, not the state dir.

    Not tidiness. A unix socket path is capped at about 108 bytes by the
    kernel, and the state dir follows `$CLAUDE_VOICE_HOME`, which somebody
    will one day point somewhere deep. The runtime dir is short by
    construction and is swept at logout, which is exactly a socket's lifetime.
    """
    rt = os.environ.get("XDG_RUNTIME_DIR", "")
    d = Path(rt) / "claude-voice" if rt else BASE
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        return BASE
    return d


# --------------------------------------------------------------- the registry


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, ValueError):
        return False
    except PermissionError:
        return True  # somebody else's process, but a process


def sessions() -> list:
    """The wrapped sessions that are live, sweeping the rest.

    Liveness, never a marker -- the same rule presence.py follows and for the
    same reason: a wrapper that is killed, or whose terminal is closed, gets
    no chance to tidy up, and those are exactly the cases where a stale target
    would send a sentence into nowhere. The sweep happens here because this is
    the moment the file is known to be stale.
    """
    out = []
    try:
        files = sorted(BASE.glob(f"{PREFIX}*.json"))
    except Exception:
        return []
    for f in files:
        # The pid is converted inside the guard. Outside it, one entry with a
        # non-numeric pid raised out of the whole sweep -- and this is what the
        # HUD and `--sessions` both call.
        try:
            d = json.loads(f.read_text())
            pid = int(d.get("pid", 0))
        except Exception:
            continue
        if not _alive(pid):
            for p in (f, Path(d.get("sock", ""))):
                try:
                    p.unlink(missing_ok=True)
                except Exception:
                    pass
            continue
        out.append({**d, **describe(d)})
    return out


def claude_session(pty_path: str) -> dict:
    """Claude Code's registry entry for whatever runs on that pty, {} if none.

    Matched on the pty rather than on the child's pid, deliberately. The pid
    we forked is the launcher, which may or may not be the process that writes
    the registry entry, and that is an implementation detail of somebody
    else's program. The controlling terminal is not: whatever ends up being
    the session, it reads from this pty and nothing else does.
    """
    try:
        files = list(CC_SESSIONS.glob("*.json"))
    except Exception:
        return {}
    for f in files:
        try:
            d = json.loads(f.read_text())
            pid = int(d.get("pid", 0))
        except Exception:
            continue
        try:
            if os.readlink(f"/proc/{pid}/fd/0") == pty_path:
                return d
        except OSError:
            continue  # exited between the glob and the readlink
    return {}


def describe(d: dict) -> dict:
    """What identifies a wrapped session on screen: the directory and a name.

    The name comes from Claude Code when it has one and from the command line
    when it does not, so a session that has not spoken yet is still nameable.
    That is the case the tmux path never got right -- a fresh window reads
    `Claude Code` until the first exchange retitles it, which is exactly the
    moment dictation is most likely to be the thing opening the conversation.
    """
    path = d.get("cwd", "")
    cc = claude_session(d.get("pty", ""))
    # The command line is the fallback name, and it is somebody's argv: it can
    # carry newlines and be arbitrarily long, and this goes on one HUD row.
    cmd = " ".join(" ".join(d.get("cmd", [])).split())
    title = cc.get("name") or cmd[:60] or "(untitled)"
    return {
        "kind": "wrap",
        "id": f"wrap:{d.get('pid')}",
        # What the SESSION knows itself by, the way `%12` is inside tmux: the
        # pty it was started on. focus.py writes the same string from inside
        # the session, so a focus and a dictation target still meet.
        "pane_id": f"pts:{d.get('pty', '')}",
        "dir": Path(path).name or path,
        "path": path,
        "title": title,
        "session": cc.get("sessionId", ""),
        "status": cc.get("status", ""),
    }


def deliver(sess: dict, text: str) -> bool:
    """Hand one line to a wrapped session. True when it took it.

    Refuses a newline rather than translating it: the wrapper owns when Enter
    is pressed, and a sentence carrying its own would submit at the break and
    leave the rest of it typed into the next prompt.
    """
    text = " ".join(text.split())
    if not text:
        return False
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(5)
        s.connect(sess["sock"])
        s.sendall(text.encode())
        s.shutdown(socket.SHUT_WR)
        return s.recv(16).strip() == b"ok"
    except Exception:
        return False
    finally:
        try:
            s.close()
        except Exception:
            pass


# ------------------------------------------------------------------ the window


def _hud_is_up() -> bool:
    try:
        return bool(_presence.windows())
    except Exception:
        return False


def ensure_hud() -> bool:
    """Open the HUD if none is open. True when one is up by the time we return.

    One HUD serves every session, so this starts at most one no matter how
    many terminals run the wrapper: the second and third find the first and
    attach to it. Detached from this terminal on purpose -- the window is the
    real process, and holding a terminal open to show nothing was the reason
    people stopped opening it at all.
    """
    if not _config.load().get("hud.autostart", True):
        return _hud_is_up()
    if _hud_is_up():
        return True
    try:
        subprocess.Popen(
            [sys.executable, str(HERE / "hudweb.py")],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception:
        return False
    # Waited for, not fired and forgotten: the hooks consult presence the
    # moment the session starts, and a session that wins that race comes up
    # silent with no sign of why.
    until = time.time() + HUD_WAIT
    while time.time() < until:
        if _hud_is_up():
            return True
        time.sleep(0.1)
    return False


# ------------------------------------------------------------------ the wrapper


def _winsize(fd: int) -> bytes:
    try:
        return fcntl.ioctl(fd, termios.TIOCGWINSZ, b"\0" * 8)
    except Exception:
        return struct.pack("HHHH", 24, 80, 0, 0)


def _spawn(cmd: list) -> tuple:
    """Fork the command onto a pty we hold the master of. (pid, master, name).

    openpty and a hand-rolled fork rather than pty.fork, for one reason: the
    slave's name. It is the identity everything else here joins on -- Claude
    Code's registry, the focus file, the sweep -- and pty.fork does not hand
    it back.
    """
    master, slave = pty.openpty()
    name = os.ttyname(slave)
    if sys.stdin.isatty():
        try:
            fcntl.ioctl(slave, termios.TIOCSWINSZ, _winsize(sys.stdin.fileno()))
        except Exception:
            pass
    pid = os.fork()
    if pid == 0:
        try:
            os.close(master)
            os.setsid()  # a session of its own, so that
            fcntl.ioctl(slave, termios.TIOCSCTTY, 0)  # this pty is its tty
            for target in (0, 1, 2):
                os.dup2(slave, target)
            if slave > 2:
                os.close(slave)
            os.execvp(cmd[0], cmd)
        except Exception as e:
            os.write(2, f"claude-voice run: {cmd[0]}: {e}\n".encode())
        os._exit(127)
    os.close(slave)
    return pid, master, name


def _listener(path: Path) -> socket.socket:
    """The way in. One socket per wrapper, readable only by its owner."""
    path.unlink(missing_ok=True)
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(str(path))
    os.chmod(path, 0o600)
    s.listen(4)
    return s


def _typed(conn: socket.socket) -> str:
    """One delivery off the socket. Newlines collapse rather than submit."""
    chunks = []
    # Short, because the wrapper is not pumping the child's output while it
    # reads this: a client that connects and stalls would freeze the terminal
    # for as long as the timeout, and a dictated line is a few hundred bytes.
    conn.settimeout(2)
    try:
        while True:
            b = conn.recv(4096)
            if not b:
                break
            chunks.append(b)
    except Exception:
        pass
    return " ".join(b"".join(chunks).decode("utf-8", "replace").split())


def wrap(cmd: list) -> int:
    """Run the command on our pty until it exits, and return what it returned."""
    ensure_hud()

    pid, master, ptyname = _spawn(cmd)
    BASE.mkdir(parents=True, exist_ok=True)
    sock_path = _sock_dir() / f"{PREFIX}{os.getpid()}.sock"
    reg = BASE / f"{PREFIX}{os.getpid()}.json"
    lis = _listener(sock_path)
    reg.write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "child": pid,
                "pty": ptyname,
                "cmd": cmd,
                "cwd": os.getcwd(),
                "sock": str(sock_path),
                "started": time.time(),
            }
        )
    )

    stdin_fd = sys.stdin.fileno()
    saved = None
    if sys.stdin.isatty():
        saved = termios.tcgetattr(stdin_fd)
        tty.setraw(stdin_fd)  # every key belongs to the child,
        # including the ones that would otherwise be ours: Ctrl-C reaches the
        # child's own session through its pty and raises there, which is what
        # makes the wrapper invisible rather than a thing to escape from.

    def resize(*_):
        try:
            fcntl.ioctl(master, termios.TIOCSWINSZ, _winsize(stdin_fd))
            os.kill(pid, signal.SIGWINCH)
        except Exception:
            pass

    try:
        signal.signal(signal.SIGWINCH, resize)
    except Exception:
        pass

    enter_at = 0.0  # when the pending Enter is due
    try:
        while True:
            watch = [master, lis]
            if saved is not None:
                watch.append(stdin_fd)
            timeout = max(0.0, enter_at - time.time()) if enter_at else None
            try:
                ready, _, _ = select.select(watch, [], [], timeout)
            except InterruptedError:
                continue  # a window resize, not an error
            except OSError:
                break

            if enter_at and time.time() >= enter_at:
                try:
                    os.write(master, b"\r")
                except OSError:
                    pass
                enter_at = 0.0

            if master in ready:
                try:
                    data = os.read(master, 65536)
                except OSError:
                    data = b""
                if not data:
                    break  # the child closed its end: it exited
                os.write(sys.stdout.fileno(), data)

            if stdin_fd in ready:
                try:
                    data = os.read(stdin_fd, 65536)
                except OSError:
                    data = b""
                if data:
                    os.write(master, data)

            if lis in ready:
                try:
                    conn, _ = lis.accept()
                except OSError:
                    continue
                try:
                    text = _typed(conn)
                    if text:
                        os.write(master, text.encode())
                        enter_at = time.time() + ENTER_DELAY
                        conn.sendall(b"ok")
                    else:
                        conn.sendall(b"no")
                except Exception:
                    pass
                finally:
                    conn.close()
    finally:
        if saved is not None:
            try:
                termios.tcsetattr(stdin_fd, termios.TCSADRAIN, saved)
            except Exception:
                pass
        for p in (reg, sock_path):
            try:
                p.unlink(missing_ok=True)
            except Exception:
                pass
        try:
            lis.close()
            os.close(master)
        except Exception:
            pass

    _, status = os.waitpid(pid, 0)
    # The child's exit is the wrapper's exit, signals included, so a script
    # that ran `claude` and checked $? still works when it runs this instead.
    if os.WIFSIGNALED(status):
        return 128 + os.WTERMSIG(status)
    return os.WEXITSTATUS(status)


def main(argv: list) -> int:
    if argv and argv[0] == "--sessions":
        print(json.dumps(sessions(), indent=2))
        return 0
    # `--` ends the wrapper's own words, for the day it has any. Until then it
    # is accepted and dropped, so a command line written defensively works.
    if argv and argv[0] == "--":
        argv = argv[1:]
    # A leading flag cannot be the command, so it is claude's: `run --resume`
    # is `run claude --resume`. That is what makes the bare `claude-voice`
    # usable with arguments, since it forwards its whole argv here untouched.
    if not argv or argv[0].startswith("-"):
        argv = ["claude", *argv]
    return wrap(argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
