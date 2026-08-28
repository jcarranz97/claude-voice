"""The local server behind the browser HUD: its gate, its routes, its stream.

No port is ever bound here. ``BaseHTTPRequestHandler`` needs a socket, not a
network, so every request in this file is a bytes literal handed to a pair of
buffers -- which also means the assertions can be about the exact status line
and headers that go back, rather than about what some client made of them.

The one thing that has to be arranged rather than asserted is time. The stream
loop is a ``while`` around a blocking wait, so ``World`` is replaced by a
scripted one that hands out a fixed list of frames and then sets the stop
flag. Nothing here sleeps and nothing here starts a thread.
"""

import io
import json
from types import SimpleNamespace

import pytest

import claude_voice.hudweb as hudweb

PORT = 45999
HOST = f"127.0.0.1:{PORT}"


# --- the socket that is really two buffers ---------------------------------


class FakeSocket:
    """Enough of a socket for ``StreamRequestHandler.setup``.

    ``wbufsize`` is 0, so the handler wraps this in a ``_SocketWriter`` and
    everything it writes arrives at ``sendall``. ``fail_after`` turns the Nth
    write into the broken pipe that a closed window produces.
    """

    def __init__(self, request: bytes, fail_after: int | None = None):
        self.incoming = io.BytesIO(request)
        self.outgoing = io.BytesIO()
        self.fail_after = fail_after
        self.writes = 0

    def makefile(self, mode="rb", bufsize=-1, *a, **kw):
        return self.incoming if "r" in mode else self.outgoing

    def sendall(self, data):
        self.writes += 1
        if self.fail_after is not None and self.writes > self.fail_after:
            raise BrokenPipeError(32, "broken pipe")
        self.outgoing.write(data)

    def settimeout(self, t):
        pass

    def setsockopt(self, *a):
        pass

    def shutdown(self, how):
        pass

    def close(self):
        pass


class Response:
    """The bytes that came back, split into a status line, headers and body.

    ``tail`` is whatever the handler wrote after the first response. On a
    keep-alive connection that is a second response, and there is a test below
    about when one appears.
    """

    def __init__(self, raw: bytes):
        self.raw = raw
        head, _, rest = raw.partition(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        self.status = int(lines[0].split()[1]) if lines and lines[0] else 0
        self.headers = {}
        for line in lines[1:]:
            k, _, v = line.partition(":")
            self.headers[k.strip().lower()] = v.strip()
        n = self.headers.get("content-length")
        self.body, self.tail = (rest[: int(n)], rest[int(n) :]) if n else (rest, b"")

    def json(self):
        return json.loads(self.body)


def build(method="GET", path="/", host=HOST, headers=None, body=b"") -> bytes:
    lines = [f"{method} {path} HTTP/1.1"]
    if host is not None:
        lines.append(f"Host: {host}")
    for k, v in (headers or {}).items():
        lines.append(f"{k}: {v}")
    if body:
        lines.append(f"Content-Length: {len(body)}")
    return ("\r\n".join(lines) + "\r\n\r\n").encode() + body


def serve_one(raw: bytes, fail_after: int | None = None) -> Response:
    """Run one request through the handler and return what it wrote.

    The handler loops until ``close_connection``; the second read finds an
    empty buffer, which is how a real connection ends too.
    """
    sock = FakeSocket(raw, fail_after=fail_after)
    hudweb.Handler(sock, ("127.0.0.1", 51000), SimpleNamespace(server_port=PORT))
    return Response(sock.outgoing.getvalue())


def acting(extra=None) -> dict:
    """The headers the page itself sends: the per-run token, same origin."""
    h = {"X-CV-Token": hudweb.TOKEN, "Content-Type": "application/json"}
    h.update(extra or {})
    return h


# --- a world that does not have to be waited on ----------------------------


class ScriptedWorld(hudweb.World):
    """A ``World`` that hands out a fixed list of frames and then stops.

    The real one is fed by a producer thread on a quarter-second tick. This
    one turns the stream loop into a finite, ordered thing a test can assert
    on: each ``wait`` pops one frame, and running out ends the loop.
    """

    def __init__(self, frames):
        super().__init__()
        self.frames = list(frames)
        self.timeouts = []

    def wait(self, seen, timeout=hudweb.HEARTBEAT):
        self.timeouts.append(timeout)
        if not self.frames:
            self.stop.set()
            return seen, ""
        self.seq += 1
        return self.seq, self.frames.pop(0)


@pytest.fixture
def world(monkeypatch):
    """Install a scripted world, and take it away again."""

    def _install(frames=()):
        w = ScriptedWorld(frames)
        monkeypatch.setattr(hudweb, "WORLD", w)
        return w

    return _install


@pytest.fixture
def ear(monkeypatch):
    """Script ``core.ear_level``: the one thing the stream polls per pass."""

    def _install(readings):
        seq = list(readings)

        def _read():
            return seq.pop(0) if len(seq) > 1 else seq[0]

        monkeypatch.setattr(hudweb.core, "ear_level", _read)

    return _install


# --- the gate ---------------------------------------------------------------


class TestTheHostGate:
    """A name that resolves to loopback still reaches this port; the Host
    header is the only thing that says which name was typed."""

    def test_a_get_from_another_host_is_misdirected(self):
        r = serve_one(build(host="hud.evil.example"))
        assert r.status == 421
        assert r.body == b"wrong host"

    def test_a_post_from_another_host_is_misdirected(self):
        r = serve_one(build("POST", "/act", host="localhost:1234", headers=acting()))
        assert r.status == 421

    def test_the_bound_socket_is_the_only_accepted_host(self):
        assert serve_one(build(path="/nowhere")).status == 404


class TestTheActionGate:
    """What a mutating request has to prove before anything happens."""

    def test_no_token_is_refused(self):
        r = serve_one(build("POST", "/quit"))
        assert r.status == 403
        assert r.json() == {"ok": False, "msg": "refused"}

    def test_a_wrong_token_is_refused(self):
        r = serve_one(build("POST", "/quit", headers={"X-CV-Token": "not-the-token"}))
        assert r.status == 403

    def test_a_cross_site_request_is_refused_even_with_the_token(self):
        # Sec-Fetch-Site is set by the browser and page script cannot forge it.
        r = serve_one(build("POST", "/quit", headers=acting({"Sec-Fetch-Site": "cross-site"})))
        assert r.status == 403

    def test_a_same_origin_request_with_the_token_is_allowed(self, monkeypatch):
        monkeypatch.setattr(hudweb.core, "act", lambda name: (True, "voice on"))
        r = serve_one(
            build(
                "POST",
                "/act",
                headers=acting({"Sec-Fetch-Site": "same-origin"}),
                body=b'{"action": "voice"}',
            )
        )
        assert r.status == 200

    def test_no_cors_header_is_ever_sent(self, monkeypatch):
        # The absence is the point: a cross-origin script would need the
        # preflight this refusal denies it.
        monkeypatch.setattr(hudweb.core, "act", lambda name: (True, ""))
        r = serve_one(build("POST", "/act", headers=acting(), body=b"{}"))
        assert not [k for k in r.headers if k.startswith("access-control")]


# --- what a GET can reach ---------------------------------------------------


class TestThePage:
    """`/` is the whole window: one HTML file with the run's token in it."""

    def test_the_page_is_served_as_html(self):
        r = serve_one(build(path="/"))
        assert r.status == 200
        assert r.headers["content-type"] == "text/html; charset=utf-8"
        assert r.headers["content-length"] == str(len(r.body))

    def test_the_token_is_substituted_into_the_page(self):
        body = serve_one(build(path="/")).body.decode()
        assert hudweb.TOKEN in body
        assert "__TOKEN__" not in body

    def test_nothing_is_cached(self):
        assert serve_one(build(path="/")).headers["cache-control"] == "no-store"

    def test_a_query_string_does_not_change_the_route(self):
        # The page is opened with ?token=..., which is how the token gets in.
        r = serve_one(build(path=f"/?token={hudweb.TOKEN}"))
        assert r.status == 200
        assert b"<title>claude-voice</title>" in r.body


class TestTheStaticFiles:
    """Three files off disk. No bundler, no CDN, and no way out of the dir."""

    def test_the_stylesheet_is_served_with_its_type(self):
        r = serve_one(build(path="/static/hud.css"))
        assert r.status == 200
        assert r.headers["content-type"] == "text/css; charset=utf-8"
        assert r.body == (hudweb.WEB / "hud.css").read_bytes()

    def test_the_script_is_served_with_its_type(self):
        r = serve_one(build(path="/static/hud.js"))
        assert r.status == 200
        assert r.headers["content-type"] == "text/javascript; charset=utf-8"

    def test_a_file_that_is_not_there_is_a_404(self):
        assert serve_one(build(path="/static/nothing.js")).status == 404

    def test_a_directory_is_not_a_file(self):
        assert serve_one(build(path="/static/")).status == 404

    def test_a_path_climbing_out_of_the_web_dir_is_refused(self):
        # The resolve-then-check is what stops this; the module's own source
        # sits one directory up and is very much readable.
        r = serve_one(build(path="/static/../hudweb.py"))
        assert r.status == 404
        assert b"import json" not in r.body

    def test_an_absolute_path_cannot_be_smuggled_in(self):
        assert serve_one(build(path="/static//etc/hostname")).status == 404


class TestUnknownRoutes:
    def test_an_unknown_get_is_a_404(self):
        r = serve_one(build(path="/favicon.ico"))
        assert r.status == 404
        assert r.headers["content-type"] == "text/plain"

    def test_an_unknown_post_is_a_json_404(self):
        r = serve_one(build("POST", "/nope", headers=acting(), body=b"{}"))
        assert r.status == 404
        assert r.json() == {"ok": False, "msg": "no such action"}

    def test_a_body_nobody_read_is_left_in_the_connection(self):
        # Recorded rather than approved. Only `/act` reads the request body,
        # so every other POST leaves it in the socket, and the keep-alive
        # connection then reads those bytes as the next request line.
        r = serve_one(build("POST", "/nope", headers=acting(), body=b"{}"))
        assert b"400" in r.tail


class TestABrokenPipe:
    """You closed the window between the headers and the body."""

    def test_a_dropped_connection_is_not_an_error(self):
        r = serve_one(build(path="/"), fail_after=1)
        assert r.status == 200
        assert r.body == b""


# --- what the browser posts back --------------------------------------------


class TestActions:
    """`/act` is every key the HUD offers, by name, through one route."""

    def test_the_named_action_reaches_hudcore(self, monkeypatch):
        seen = []
        monkeypatch.setattr(hudweb.core, "act", lambda name: (seen.append(name), (True, "ok"))[1])
        r = serve_one(build("POST", "/act", headers=acting(), body=b'{"action": "dictate"}'))
        assert seen == ["dictate"]
        assert r.json() == {"ok": True, "msg": "ok"}

    def test_a_refusal_comes_back_as_a_message_not_an_error(self, monkeypatch):
        # A HUD that swallows a refusal is a HUD you press twice.
        monkeypatch.setattr(hudweb.core, "act", lambda name: (False, "no microphone"))
        r = serve_one(build("POST", "/act", headers=acting(), body=b'{"action": "conversation"}'))
        assert r.status == 200
        assert r.json() == {"ok": False, "msg": "no microphone"}

    def test_a_body_that_is_not_json_asks_for_nothing(self, monkeypatch):
        seen = []
        monkeypatch.setattr(hudweb.core, "act", lambda name: (seen.append(name), (False, ""))[1])
        serve_one(build("POST", "/act", headers=acting(), body=b"not json at all"))
        assert seen == [""]

    def test_an_empty_body_asks_for_nothing(self, monkeypatch):
        seen = []
        monkeypatch.setattr(hudweb.core, "act", lambda name: (seen.append(name), (False, ""))[1])
        serve_one(build("POST", "/act", headers=acting()))
        assert seen == [""]

    def test_the_answer_is_json(self, monkeypatch):
        monkeypatch.setattr(hudweb.core, "act", lambda name: (True, ""))
        r = serve_one(build("POST", "/act", headers=acting(), body=b"{}"))
        assert r.headers["content-type"] == "application/json"


class TestQuit:
    """The close button. It answers first, then takes the window down."""

    def test_quitting_answers_then_stops_the_world(self, monkeypatch, world):
        w = world()
        ran = []

        class Immediate:
            def __init__(self, target, daemon=False):
                self.target = target

            def start(self):
                ran.append(self.target)
                self.target()

        monkeypatch.setattr(hudweb, "threading", SimpleNamespace(Thread=Immediate))
        r = serve_one(build("POST", "/quit", headers=acting()))
        assert r.json() == {"ok": True, "msg": ""}
        assert ran == [hudweb.quit_now]
        assert w.stop.is_set()

    def test_quit_now_wakes_everything_waiting(self, world):
        w = world()
        hudweb.quit_now()
        assert w.stop.is_set()


# --- the event stream -------------------------------------------------------


class TestTheEventStream:
    """`/events` is the connection, and the connection is the window."""

    def test_a_stream_without_the_token_is_refused(self, world):
        world()
        r = serve_one(build(path="/events"))
        assert r.status == 403

    def test_a_stream_with_the_wrong_token_is_refused(self, world):
        world()
        r = serve_one(build(path="/events?token=guessed"))
        assert r.status == 403

    def test_the_stream_announces_itself_as_an_event_stream(self, world, ear):
        world()
        ear([(False, 0.0)])
        r = serve_one(build(path=f"/events?token={hudweb.TOKEN}"))
        assert r.status == 200
        assert r.headers["content-type"] == "text/event-stream"
        assert r.headers["cache-control"] == "no-store"
        assert r.headers["x-accel-buffering"] == "no"
        # No Content-Length: the body ends when the socket does.
        assert "content-length" not in r.headers
        assert r.body.startswith(b"retry: 1000\n\n")

    def test_state_frames_go_out_as_default_events(self, world, ear):
        world(['{"state":"idle"}', '{"state":"speaking"}'])
        ear([(False, 0.0)])
        body = serve_one(build(path=f"/events?token={hudweb.TOKEN}")).body
        assert b'data: {"state":"idle"}\n\n' in body
        assert b'data: {"state":"speaking"}\n\n' in body

    def test_the_microphone_level_is_its_own_named_event(self, world, ear):
        world(['{"state":"idle"}'])
        ear([(True, 0.5)])
        body = serve_one(build(path=f"/events?token={hudweb.TOKEN}")).body
        assert b"event: level\ndata: 0.5\n\n" in body

    def test_an_unchanged_level_is_not_repeated(self, world, ear):
        # A quiet room is one message, not twenty a second of them.
        world(["", "", ""])
        ear([(True, 0.4)])
        body = serve_one(build(path=f"/events?token={hudweb.TOKEN}")).body
        assert body.count(b"event: level") == 1

    def test_a_closing_ear_sends_one_zero_and_stops(self, world, ear):
        # So a page holding the last level does not hold it forever.
        world(['{"a":1}', "", ""])
        ear([(True, 0.7), (False, 0.0), (False, 0.0), (False, 0.0)])
        body = serve_one(build(path=f"/events?token={hudweb.TOKEN}")).body
        assert body.count(b"event: level\ndata: 0\n\n") == 1

    def test_an_open_ear_is_waited_on_far_more_often(self, world, ear):
        w = world(["", ""])
        ear([(True, 0.1), (False, 0.0), (False, 0.0)])
        serve_one(build(path=f"/events?token={hudweb.TOKEN}"))
        assert w.timeouts[0] == hudweb.LEVEL_TICK
        assert w.timeouts[1] == hudweb.HEARTBEAT

    def test_a_silent_room_still_gets_a_comment(self, world, ear, monkeypatch):
        # It is how this end notices a window that closed without a FIN.
        monkeypatch.setattr(hudweb, "HEARTBEAT", 0.0)
        world([""])
        ear([(False, 0.0)])
        body = serve_one(build(path=f"/events?token={hudweb.TOKEN}")).body
        assert b":\n\n" in body

    def test_a_client_is_counted_while_it_is_connected(self, world, ear):
        w = world([""])
        ear([(False, 0.0)])
        serve_one(build(path=f"/events?token={hudweb.TOKEN}"))
        assert w.ever is True
        assert w.clients == 0  # left again on the way out

    def test_a_window_closed_mid_stream_is_not_an_error(self, world, ear):
        w = world(['{"a":1}', '{"a":2}'])
        ear([(False, 0.0)])
        # Headers and the retry line get through; the first frame does not.
        r = serve_one(build(path=f"/events?token={hudweb.TOKEN}"), fail_after=2)
        assert r.status == 200
        assert w.clients == 0  # the finally ran even so


class TestTheStreamContract:
    """The keys web/hud.js reads off a frame. Renaming one silently blanks a
    panel in the window, which is why they are written down twice."""

    KEYS = (
        "state",
        "label",
        "said",
        "agents",
        "voice_on",
        "focus",
        "dictation",
        "mic",
        "language",
        "session",
        "repo",
        "level",
        "panels",
        "system",
        "history",
        "labels",
    )

    def test_every_key_the_page_reads_survives_the_stream(self, world, ear, monkeypatch):
        frame = {k: {} for k in self.KEYS}
        world([json.dumps(frame, separators=(",", ":"))])
        ear([(False, 0.0)])
        body = serve_one(build(path=f"/events?token={hudweb.TOKEN}")).body
        payload = None
        for line in body.split(b"\n"):
            if line.startswith(b"data: {"):
                payload = json.loads(line[len(b"data: ") :])
        assert payload is not None
        assert set(payload) == set(self.KEYS)

    def test_hudcore_still_answers_with_those_keys(self, monkeypatch):
        # The other half of the contract: the producer is asked for exactly
        # what the page reads. Nothing is spawned -- snapshot() is only
        # allowed to look at the empty state dir.
        monkeypatch.setattr(hudweb.core, "system_stats", dict)
        monkeypatch.setattr(hudweb.core, "history_entries", list)
        monkeypatch.setattr(hudweb.core, "display_state", lambda: ("idle", "", [], False))
        monkeypatch.setattr(hudweb.core, "dictate_target_info", dict)
        monkeypatch.setattr(hudweb.core, "focus_state", lambda **kw: ("none", ""))
        snap = hudweb.core.snapshot()
        assert set(self.KEYS) <= set(snap)
        json.dumps(snap)  # and it is all serialisable


# --- the producer -----------------------------------------------------------


class TestWorld:
    """One thread computes the state; the streams only read what it computed.

    Not an optimisation: snapshot() can block, and a stream thread that blocks
    is a window that freezes.
    """

    def test_a_new_frame_bumps_the_sequence(self, monkeypatch):
        w = hudweb.World()
        monkeypatch.setattr(hudweb, "TICK", 0)
        frames = [{"n": 1}, {"n": 2}]

        def snap():
            if not frames:
                w.stop.set()
                return {"n": 0}
            return frames.pop(0)

        monkeypatch.setattr(hudweb.core, "snapshot", snap)
        w.run()
        assert w.seq == 3
        assert w.data == '{"n":0}'

    def test_an_unchanged_frame_does_not(self, monkeypatch):
        w = hudweb.World()
        monkeypatch.setattr(hudweb, "TICK", 0)
        seen = []

        def snap():
            seen.append(1)
            if len(seen) == 3:
                w.stop.set()
            return {"n": 1}

        monkeypatch.setattr(hudweb.core, "snapshot", snap)
        w.run()
        assert w.seq == 1

    def test_a_failing_snapshot_is_a_stale_frame_not_a_dead_window(self, monkeypatch):
        w = hudweb.World()
        monkeypatch.setattr(hudweb, "TICK", 0)

        def boom():
            w.stop.set()
            raise RuntimeError("tmux went away")

        monkeypatch.setattr(hudweb.core, "snapshot", boom)
        w.run()
        assert json.loads(w.data) == {"error": "tmux went away"}

    def test_joining_returns_a_sequence_that_has_already_moved(self):
        w = hudweb.World()
        w.seq = 4
        assert w.join() == 3  # so the first wait returns immediately
        assert w.clients == 1
        assert w.ever is True

    def test_waiting_on_a_frame_already_there_returns_it(self):
        w = hudweb.World()
        w.seq, w.data = 2, '{"a":1}'
        assert w.wait(1) == (2, '{"a":1}')

    def test_waiting_out_the_timeout_returns_nothing(self):
        w = hudweb.World()
        w.seq, w.data = 2, '{"a":1}'
        assert w.wait(2, timeout=0) == (2, "")

    def test_a_connected_window_is_never_abandoned(self):
        w = hudweb.World()
        w.join()
        assert w.abandoned() is False

    def test_a_window_that_never_connected_is_given_longer(self):
        w = hudweb.World()
        assert w.abandoned() is False
        w.last_client -= hudweb.FIRST_CONNECT + 1
        assert w.abandoned() is True

    def test_a_window_that_closed_is_abandoned_after_the_grace(self):
        w = hudweb.World()
        w.join()
        w.leave()
        assert w.abandoned() is False
        w.last_client -= hudweb.GRACE + 1
        assert w.abandoned() is True


# --- starting and stopping --------------------------------------------------


class FakeServer:
    def __init__(self, addr, handler):
        self.addr = addr
        self.handler = handler
        self.server_port = PORT
        self.closed = False
        self.stopped = False

    def serve_forever(self):
        pass

    def shutdown(self):
        self.stopped = True

    def server_close(self):
        self.closed = True


class FakeThread:
    started = []

    def __init__(self, target=None, daemon=False):
        self.target = target

    def start(self):
        FakeThread.started.append(self.target)


@pytest.fixture
def no_threads(monkeypatch):
    """Nothing in this file is allowed to start a thread."""
    FakeThread.started = []
    monkeypatch.setattr(hudweb, "threading", SimpleNamespace(Thread=FakeThread))
    return FakeThread


class TestServe:
    def test_the_url_carries_the_port_and_the_token(self, monkeypatch, no_threads):
        made = []
        monkeypatch.setattr(
            hudweb, "ThreadingHTTPServer", lambda a, h: made.append(FakeServer(a, h)) or made[0]
        )
        srv, url = hudweb.serve()
        assert srv.addr == ("127.0.0.1", 0)  # the kernel picks the port
        assert srv.handler is hudweb.Handler
        assert url == f"http://127.0.0.1:{PORT}/?token={hudweb.TOKEN}"

    def test_the_listener_and_the_producer_both_run(self, monkeypatch, no_threads):
        monkeypatch.setattr(hudweb, "ThreadingHTTPServer", FakeServer)
        srv, _ = hudweb.serve()
        assert srv.serve_forever in no_threads.started
        assert hudweb.WORLD.run in no_threads.started


class TestShutdown:
    """What the window takes with it when it is the last one out."""

    def test_a_second_window_keeps_everything_running(self, monkeypatch):
        left = []
        monkeypatch.setattr(hudweb._presence, "leave", lambda: left.append(1))
        monkeypatch.setattr(hudweb._presence, "last_one_out", lambda: False)
        monkeypatch.setattr(hudweb.core, "conversation_alive", _must_not_be_called)
        hudweb.shutdown()
        assert left == [1]

    def test_the_last_window_out_silences_everything(self, monkeypatch):
        done = []
        monkeypatch.setattr(hudweb._presence, "leave", lambda: done.append("leave"))
        monkeypatch.setattr(hudweb._presence, "last_one_out", lambda: True)
        monkeypatch.setattr(hudweb.core, "conversation_alive", lambda: True)
        monkeypatch.setattr(hudweb.core, "conversation_stop", lambda: done.append("conversation"))
        monkeypatch.setattr(hudweb.core, "run", lambda *a: done.append(a))
        monkeypatch.setattr(hudweb.core, "sweep_orphans", lambda: done.append("orphans"))
        hudweb.shutdown()
        assert done == ["leave", "conversation", ("voice.py", "silence"), "orphans"]

    def test_a_step_that_throws_does_not_stop_the_sweep(self, monkeypatch):
        done = []
        monkeypatch.setattr(hudweb._presence, "leave", lambda: None)
        monkeypatch.setattr(hudweb._presence, "last_one_out", lambda: True)
        monkeypatch.setattr(hudweb.core, "conversation_alive", _boom)
        monkeypatch.setattr(hudweb.core, "run", _boom)
        monkeypatch.setattr(hudweb.core, "sweep_orphans", lambda: done.append("orphans"))
        hudweb.shutdown()
        assert done == ["orphans"]


def _must_not_be_called(*a, **kw):
    raise AssertionError("the sweep ran while another window was open")


def _boom(*a, **kw):
    raise RuntimeError("no")


class StoppedWorld(hudweb.World):
    """Abandoned the first time it is asked, so main's loop runs once."""

    def abandoned(self):
        return True


@pytest.fixture
def quiet_main(monkeypatch):
    """Everything ``main`` reaches for, replaced. No socket, no window."""
    srv = FakeServer(("127.0.0.1", 0), hudweb.Handler)
    monkeypatch.setattr(hudweb, "serve", lambda: (srv, "http://127.0.0.1:1/?token=x"))
    monkeypatch.setattr(hudweb, "WORLD", StoppedWorld())
    monkeypatch.setattr(hudweb._presence, "enter", lambda: None)
    monkeypatch.setattr(hudweb, "shutdown", lambda: None)
    return srv


class TestMain:
    def test_url_mode_prints_the_address_and_opens_nothing(self, quiet_main, capsys):
        assert hudweb.main(["--url"]) == 0
        assert capsys.readouterr().out.strip() == "http://127.0.0.1:1/?token=x"
        assert quiet_main.stopped and quiet_main.closed

    def test_the_window_is_opened_and_closed_again(self, quiet_main, monkeypatch):
        opened, closed = [], []

        class Shell:
            def close(self):
                closed.append(1)

        shell = Shell()
        fake = SimpleNamespace(
            open_window=lambda url, argv: (opened.append((url, argv)), shell)[1],
        )
        monkeypatch.setitem(__import__("sys").modules, "hudshell", fake)
        assert hudweb.main([]) == 0
        assert opened == [("http://127.0.0.1:1/?token=x", [])]
        assert closed == [1]

    def test_the_world_is_stopped_on_the_way_out(self, quiet_main):
        hudweb.main(["--url"])
        assert hudweb.WORLD.stop.is_set()

    def test_an_interrupt_still_runs_the_teardown(self, quiet_main, monkeypatch):
        swept = []
        monkeypatch.setattr(hudweb, "shutdown", lambda: swept.append(1))
        monkeypatch.setattr(hudweb.WORLD, "abandoned", _raise_keyboard_interrupt, raising=False)
        assert hudweb.main(["--url"]) == 0
        assert swept == [1]
        assert quiet_main.closed

    def test_a_live_window_keeps_the_process_waiting(self, quiet_main, monkeypatch):
        # The idle path: nothing is abandoned, so main sits on its half-second
        # tick. Stopped from inside the tick rather than by waiting one out.
        world = hudweb.World()
        monkeypatch.setattr(hudweb, "WORLD", world)
        monkeypatch.setattr(hudweb.time, "sleep", lambda s: world.stop.set())
        assert hudweb.main(["--url"]) == 0
        assert world.stop.is_set()

    def test_a_world_already_stopped_never_enters_the_loop(self, quiet_main, monkeypatch):
        monkeypatch.setattr(hudweb.WORLD, "abandoned", _must_not_be_called, raising=False)
        hudweb.WORLD.stop.set()
        assert hudweb.main(["--url"]) == 0


def _raise_keyboard_interrupt():
    raise KeyboardInterrupt


class TestSignals:
    def test_a_closed_terminal_exits_rather_than_leaving_the_ear_open(self):
        # SIGHUP and SIGTERM run no `finally` of their own, which is how the
        # microphone was left open by the exact exit that most needed it shut.
        with pytest.raises(SystemExit) as e:
            hudweb._bye(15, None)
        assert e.value.code == 0


class TestHousekeeping:
    def test_the_access_log_is_the_hud_itself(self):
        assert hudweb.Handler.log_message(None, "%s", "anything") is None

    def test_the_server_does_not_announce_its_python(self):
        assert hudweb.Handler.sys_version == ""
        assert hudweb.Handler.server_version == "claude-voice"

    def test_the_token_is_long_enough_to_be_worth_having(self):
        assert len(hudweb.TOKEN) >= 24
