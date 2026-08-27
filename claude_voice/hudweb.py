#!/usr/bin/env python3
"""The HUD, in a window that can draw curves. This is the one you get.

    claude-voice hud

Same HUD as the terminal one (`hud --terminal`), which is still there for a
machine with no desktop. The reactor, the history, the keys and every refusal
come from hudcore, which is also what the curses window reads -- there is one
answer to "is the microphone open", not two.

What is different is only the surface: a local page in a frameless browser
window, so the thing on screen can be a smooth glowing shape and a real
proportional layout instead of ring glyphs on a character grid.

  The window is the connection. While the page holds the event stream open
  this process counts as an open window, and presence.py lets the hooks
  speak. When the last stream drops -- you closed the window -- this process
  shuts itself down and takes the microphone, the heartbeat and any queued
  audio with it. That is exactly the promise the curses HUD made by being a
  process you could close, kept by different means.

Nothing is bundled and nothing is built: three files off disk, no CDN, no
node. The server is the standard library.

Why it is locked down at all, for a page only you can reach: the buttons open
a microphone and redirect the voice, and any page in any browser can aim a
navigation at a loopback port. So actions are POST only, behind a Host
allowlist (DNS rebinding), a per-run token in a CUSTOM header (a form or a
navigation cannot set one, and no CORS header is ever sent, so the preflight
that a cross-origin script would need fails), and Sec-Fetch-Site, which page
script cannot forge.
"""

import json
import secrets
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import presence as _presence                          # noqa: E402
import hudcore as core                                # noqa: E402

WEB = HERE / "web"
TOKEN = secrets.token_urlsafe(24)

# How often the producer recomputes the world. The curses HUD redraws at 20
# FPS because it also animates; here the animation is the browser's job and
# this only has to carry state, which nothing changes faster than a person can
# see. Every question underneath is cached harder than this anyway.
TICK = 0.25
HEARTBEAT = 15.0

# The microphone level, which is the one thing here that changes faster than
# the state does. It rides the same stream as a named event, and only while
# something is actually publishing it -- an idle window still gets one message
# every fifteen seconds and no more. The line being SPOKEN needs none of this:
# its whole shape is in the state, and the page draws it off the clock.
LEVEL_TICK = 0.05

# A window that has gone quiet this long is a window that was closed. Long
# enough to survive a reload, short enough that closing the HUD closes the
# microphone while you are still in the room.
GRACE = 6.0
# Nothing ever connected: the browser failed to start, or you closed it before
# it painted. Exiting is the honest answer -- a server nobody can see must not
# be what keeps the voice alive.
FIRST_CONNECT = 45.0

TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
         ".js": "text/javascript; charset=utf-8", ".svg": "image/svg+xml"}


class World:
    """One thread computes the state; every stream reads what it computed.

    Not an optimisation. snapshot() shells out to tmux, and a stream thread
    that blocks on a subprocess is a window that freezes for as long as tmux
    takes. The streams never call it: they wait on the condition and send
    whatever is there.
    """

    def __init__(self):
        self.cond = threading.Condition()
        self.data = ""
        self.seq = 0
        self.clients = 0
        self.last_client = time.time()
        self.ever = False
        self.stop = threading.Event()

    def run(self):
        while not self.stop.is_set():
            try:
                data = json.dumps(core.snapshot(), separators=(",", ":"))
            except Exception as e:                     # a stale frame, not a dead window
                data = json.dumps({"error": str(e)})
            with self.cond:
                if data != self.data:
                    self.data, self.seq = data, self.seq + 1
                    self.cond.notify_all()
            self.stop.wait(TICK)

    def join(self):
        with self.cond:
            self.clients += 1
            self.ever = True
        return self.seq - 1          # so the first wait returns immediately

    def leave(self):
        with self.cond:
            self.clients -= 1
            self.last_client = time.time()

    def wait(self, seen, timeout: float = HEARTBEAT):
        """Block until the state changes, or until the timeout. A shorter one
        is how a stream that is also carrying levels wakes up often enough to
        send them without ever polling the state faster than the state moves."""
        with self.cond:
            if self.seq != seen:
                return self.seq, self.data
            self.cond.wait(timeout)
            return self.seq, (self.data if self.seq != seen else "")

    def abandoned(self) -> bool:
        with self.cond:
            if self.clients > 0:
                return False
            since = time.time() - self.last_client
        return since > (GRACE if self.ever else FIRST_CONNECT)


WORLD = World()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "claude-voice"
    sys_version = ""

    def log_message(self, *a):
        pass                                    # the HUD is the log

    # --- the gate ---------------------------------------------------------

    def _host_ok(self) -> bool:
        """The Host header must name the socket we actually bound.

        This is the DNS-rebinding check: a name that resolves to 127.0.0.1
        reaches this port, and only the Host header says which name was typed.
        """
        return self.headers.get("Host", "") == f"127.0.0.1:{self.server.server_port}"

    def _may_act(self) -> bool:
        """Everything a mutating request has to prove.

        The custom header is the load-bearing one: a form submission and a
        top-level navigation -- the two things a hostile page can aim at
        loopback -- cannot set one, and a script that tried would need a CORS
        preflight this server answers with nothing at all.
        """
        if not secrets.compare_digest(self.headers.get("X-CV-Token", ""), TOKEN):
            return False
        site = self.headers.get("Sec-Fetch-Site", "same-origin")
        return site == "same-origin"

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # Nothing here is worth caching and a stale HUD is a lying HUD.
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, code: int, obj) -> None:
        self._send(code, json.dumps(obj).encode(), "application/json")

    # --- routes -----------------------------------------------------------

    def do_GET(self):
        if not self._host_ok():
            return self._send(421, b"wrong host", "text/plain")
        path, _, query = self.path.partition("?")

        if path == "/":
            # The token reaches the page through the URL it was opened with,
            # and lives only in this run: the page hands it back in a header
            # from then on. Nothing external is ever loaded, so it cannot
            # leave in a Referer.
            html = (WEB / "index.html").read_text().replace("__TOKEN__", TOKEN)
            return self._send(200, html.encode(), TYPES[".html"])

        if path.startswith("/static/"):
            f = (WEB / path[len("/static/"):]).resolve()
            if WEB.resolve() not in f.parents or not f.is_file():
                return self._send(404, b"no", "text/plain")
            return self._send(200, f.read_bytes(), TYPES.get(f.suffix, "text/plain"))

        if path == "/events":
            if f"token={TOKEN}" not in query:
                return self._send(403, b"no", "text/plain")
            return self.stream()

        return self._send(404, b"no", "text/plain")

    def do_POST(self):
        if not self._host_ok():
            return self._send(421, b"wrong host", "text/plain")
        if not self._may_act():
            return self._json(403, {"ok": False, "msg": "refused"})
        path = self.path.partition("?")[0]

        if path == "/quit":
            self._json(200, {"ok": True, "msg": ""})
            threading.Thread(target=quit_now, daemon=True).start()
            return

        if path == "/act":
            try:
                n = int(self.headers.get("Content-Length", 0))
                name = json.loads(self.rfile.read(n) or b"{}").get("action", "")
            except Exception:
                name = ""
            ok, msg = core.act(name)
            return self._json(200, {"ok": ok, "msg": msg})

        return self._json(404, {"ok": False, "msg": "no such action"})

    def stream(self):
        """One event stream, for as long as the window is open."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        # No Content-Length: the body ends when the socket does, which is what
        # EventSource expects and what closing the window produces.
        self.end_headers()
        seen = WORLD.join()
        sent, spoke = -1.0, time.time()
        try:
            self.wfile.write(b"retry: 1000\n\n")
            self.wfile.flush()
            while not WORLD.stop.is_set():
                open_ear, level = core.ear_level()
                seen, data = WORLD.wait(seen, LEVEL_TICK if open_ear else HEARTBEAT)
                out = f"data: {data}\n\n" if data else ""
                if not open_ear:
                    # The ear closed: say so once, so a page holding the last
                    # level does not hold it forever.
                    if sent >= 0:
                        out += "event: level\ndata: 0\n\n"
                    sent = -1.0
                elif level != sent:
                    # Named, so the page can take it without re-rendering
                    # everything else, and only when the number has moved: a
                    # quiet room is one message, not twenty a second of them.
                    sent = level
                    out += f"event: level\ndata: {level}\n\n"
                # A comment when nothing has been said for a while: it keeps
                # anything in the middle from reaping the stream, and it is
                # how this end notices a window closed without a FIN. A
                # silent room sends nothing at all, so it still needs one.
                if not out and time.time() - spoke >= HEARTBEAT:
                    out = ":\n\n"
                if not out:
                    continue
                spoke = time.time()
                self.wfile.write(out.encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass                                   # you closed the window
        finally:
            WORLD.leave()
            self.close_connection = True


def quit_now() -> None:
    WORLD.stop.set()
    with WORLD.cond:
        WORLD.cond.notify_all()


def shutdown() -> None:
    """Leave nothing of ours running -- the same sweep the curses HUD does.

    The window IS the application: what it started, it takes with it. Skipped
    while another HUD of either kind is still up, because two windows are two
    windows and closing one is not closing the application.
    """
    _presence.leave()
    if not _presence.last_one_out():
        return
    for step in (lambda: core.conversation_alive() and core.conversation_stop(),
                 lambda: core.run("voice.py", "silence"),
                 core.sweep_orphans):
        try:
            step()
        except Exception:
            pass


def serve() -> tuple:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    threading.Thread(target=WORLD.run, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_port}/?token={TOKEN}"


def main(argv: list) -> int:
    srv, url = serve()
    shell = None
    if "--url" in argv:
        # For a window you are opening yourself, or a second screen.
        print(url, flush=True)
    else:
        import hudshell
        shell = hudshell.open_window(url, argv)

    _presence.enter()
    try:
        while not WORLD.stop.is_set():
            if WORLD.abandoned():
                break
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        WORLD.stop.set()
        srv.shutdown()
        srv.server_close()
        if shell:
            shell.close()
        shutdown()
    return 0


def _bye(signum, frame):
    """A closed terminal sends SIGHUP and a killed one SIGTERM, and neither
    runs a `finally` on its own -- which is how the microphone was left open
    by the exact exit that most needed it closed."""
    raise SystemExit(0)


if __name__ == "__main__":
    for _sig in (signal.SIGTERM, signal.SIGHUP):
        try:
            signal.signal(_sig, _bye)
        except Exception:
            pass
    raise SystemExit(main(sys.argv[1:]))
