"""The window the web HUD lives in: which one gets opened, and what it does.

Two halves, and they never run in the same process on a real machine. The
launcher half picks a shell and spawns it, so here every `Popen`, `which` and
probe is a double -- nothing is started, and no browser is looked for on the
machine running the suite.

The webview half is a GTK application, and GTK is a system package this
package deliberately does not depend on. So `gi` is faked: enough of Gtk, Gdk,
WebKit2 and GLib to record what the window was told, plus a `Gtk.main` the test
drives itself. The signal handlers the window connects -- the page's bridge,
the escape key, the geometry it remembers -- are then called directly, which is
the only way to reach them without a display.
"""

import json
import subprocess
import sys
import types

import pytest

import claude_voice.hudshell as hudshell


@pytest.fixture
def shell_config(home):
    """Write a `config.toml` the way `hudshell` reads it, and put it back after.

    This module imports its sibling by bare name (`import config`), which is a
    different module object -- and a different cache -- from the
    `claude_voice.config` the harness reloads between tests. Reloading the one
    `hudshell` actually holds is what makes a written key visible here; doing
    it again on the way out is what keeps it out of the next test.
    """

    def _write(toml_source: str):
        (home / "config.toml").write_text(toml_source)
        return hudshell._config.load(reload=True)

    yield _write
    (home / "config.toml").unlink(missing_ok=True)
    hudshell._config.load(reload=True)


# --- the launcher --------------------------------------------------------


class FakePopen:
    """A child that was never started."""

    started = []

    def __init__(self, cmd, **kw):
        self.cmd, self.kw = cmd, kw
        self.returncode = None
        self.terminated = self.killed = False
        self.waited = None
        FakePopen.started.append(self)

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        self.waited = timeout
        return 0

    def kill(self):
        self.killed = True


@pytest.fixture
def popen(monkeypatch):
    """`subprocess.Popen`, recorded rather than run."""
    FakePopen.started = []
    monkeypatch.setattr(hudshell.subprocess, "Popen", FakePopen)
    return FakePopen.started


class TestShellClose:
    """A window we opened is a window we close."""

    def test_a_shell_with_no_window_closes_quietly(self):
        hudshell.Shell(None, "none").close()

    def test_a_window_already_gone_is_left_alone(self):
        proc = FakePopen(["x"])
        proc.returncode = 0
        hudshell.Shell(proc, "browser").close()
        assert not proc.terminated

    def test_a_live_window_is_asked_to_go_first(self):
        proc = FakePopen(["x"])
        hudshell.Shell(proc, "browser").close()
        assert proc.terminated and proc.waited == 3
        assert not proc.killed

    def test_a_window_that_will_not_go_is_killed(self):
        proc = FakePopen(["x"])
        proc.wait = lambda timeout=None: (_ for _ in ()).throw(subprocess.TimeoutExpired("x", 3))
        hudshell.Shell(proc, "browser").close()
        assert proc.killed

    def test_a_kill_that_also_fails_is_not_worth_a_traceback(self):
        proc = FakePopen(["x"])

        def boom(*_a, **_kw):
            raise OSError("gone")

        proc.wait, proc.kill = boom, boom
        hudshell.Shell(proc, "browser").close()


class TestPythonWithGi:
    """Finding an interpreter that can import GTK, without importing it here."""

    def test_the_first_interpreter_that_answers_wins(self, monkeypatch):
        tried = []

        def probe(cmd, **kw):
            tried.append(cmd[0])
            return types.SimpleNamespace(returncode=0)

        monkeypatch.setattr(hudshell.subprocess, "run", probe)
        assert hudshell._python_with_gi() == sys.executable
        assert tried == [sys.executable]

    def test_no_interpreter_with_gtk_is_an_empty_answer(self, monkeypatch):
        monkeypatch.setattr(
            hudshell.subprocess, "run", lambda *a, **kw: types.SimpleNamespace(returncode=1)
        )
        monkeypatch.setattr(hudshell.shutil, "which", lambda _n: "/usr/local/bin/python3")
        assert hudshell._python_with_gi() == ""

    def test_an_interpreter_that_cannot_be_run_is_skipped(self, monkeypatch):
        def boom(*_a, **_kw):
            raise OSError("no such file")

        monkeypatch.setattr(hudshell.subprocess, "run", boom)
        monkeypatch.setattr(hudshell.shutil, "which", lambda _n: None)
        assert hudshell._python_with_gi() == ""

    def test_the_same_interpreter_is_only_probed_once(self, monkeypatch):
        # sys.executable, /usr/bin/python3 and `which python3` are usually the
        # same file, and probing it three times costs three imports of GTK.
        tried = []
        monkeypatch.setattr(hudshell.shutil, "which", lambda _n: sys.executable)
        monkeypatch.setattr(hudshell.sys, "executable", sys.executable)

        def probe(cmd, **kw):
            tried.append(cmd[0])
            return types.SimpleNamespace(returncode=1)

        monkeypatch.setattr(hudshell.subprocess, "run", probe)
        hudshell._python_with_gi()
        assert len(tried) == len(set(tried))


class TestWebviewShell:
    """The frameless window, spawned under whichever interpreter has GTK."""

    def test_no_gtk_anywhere_is_a_refusal_with_the_package_names(self, monkeypatch):
        monkeypatch.setattr(hudshell, "_python_with_gi", lambda: "")
        with pytest.raises(RuntimeError, match="python3-gi"):
            hudshell._webview("http://127.0.0.1:9/")

    def test_the_window_runs_this_module_under_that_interpreter(self, monkeypatch, popen):
        monkeypatch.setattr(hudshell, "_python_with_gi", lambda: "/usr/bin/python3")
        shell = hudshell._webview("http://127.0.0.1:9/")
        assert shell.kind == "webview"
        cmd = popen[0].cmd
        assert cmd[0] == "/usr/bin/python3"
        assert cmd[1].endswith("hudshell.py")
        assert cmd[2] == "http://127.0.0.1:9/"
        # Both of these are the difference between a window and a bug report.
        assert popen[0].kw["env"]["GDK_BACKEND"] == "x11"
        assert popen[0].kw["env"]["WEBKIT_DISABLE_DMABUF_RENDERER"] == "1"


class TestBrowserShell:
    """Chrome in app mode, with a profile of its own."""

    def test_no_chromium_anywhere_is_a_refusal(self, monkeypatch):
        monkeypatch.setattr(hudshell.shutil, "which", lambda _n: None)
        with pytest.raises(RuntimeError, match="chromium-based"):
            hudshell._browser("http://127.0.0.1:9/")

    def test_the_first_browser_on_the_machine_is_used(self, monkeypatch, popen):
        monkeypatch.setattr(
            hudshell.shutil, "which", lambda n: "/usr/bin/chromium" if n == "chromium" else None
        )
        shell = hudshell._browser("http://127.0.0.1:9/")
        assert shell.kind == "browser"
        cmd = popen[0].cmd
        assert cmd[0] == "chromium"
        assert "--app=http://127.0.0.1:9/" in cmd
        assert f"--user-data-dir={hudshell.PROFILE}" in cmd
        # A shared profile would make this a window of your browser.
        assert hudshell.PROFILE.is_dir()


class TestNoShell:
    """The fallback that is not a failure: say where the HUD is."""

    def test_the_url_is_printed(self, capsys):
        shell = hudshell._none("http://127.0.0.1:9/")
        assert shell.kind == "none" and shell.proc is None
        assert capsys.readouterr().out.strip() == "http://127.0.0.1:9/"


class TestOpenWindow:
    """Which shell gets asked, in which order."""

    @pytest.fixture(autouse=True)
    def shells(self, monkeypatch):
        """Every shell, recording rather than opening."""
        asked = []

        def maker(name, fail=False):
            def _open(url):
                asked.append(name)
                if fail:
                    raise RuntimeError(f"{name} is not here")
                return hudshell.Shell(None, name)

            return _open

        for name in ("webview", "browser", "none"):
            monkeypatch.setitem(hudshell.SHELLS, name, maker(name))
        self.asked = asked
        self.maker = maker
        self.monkeypatch = monkeypatch
        return asked

    def test_auto_tries_the_browser_first(self):
        """Browser before webview, deliberately.

        The frameless window is the nicer object, but we disable WebKit's GPU
        renderer to stop it painting white on NVIDIA, and it then rasterizes
        the reactor on the CPU -- 97% of a core against Chromium's 14%.
        """
        assert hudshell.open_window("u").kind == "browser"
        assert self.asked == ["browser"]

    def test_a_flag_pins_one_shell(self):
        assert hudshell.open_window("u", ["--shell", "browser"]).kind == "browser"
        assert self.asked == ["browser"]

    def test_a_flag_with_nothing_after_it_is_ignored(self):
        assert hudshell.open_window("u", ["--shell"]).kind == "browser"

    def test_an_unknown_name_falls_back_to_the_usual_order(self):
        assert hudshell.open_window("u", ["--shell", "wayland"]).kind == "browser"

    def test_the_config_picks_the_shell_when_no_flag_does(self, shell_config):
        shell_config("[hud]\nshell = 'webview'\n")
        assert hudshell.open_window("u").kind == "webview"

    def test_a_shell_that_will_not_open_falls_through_to_the_next(self):
        """A machine with no Chromium still gets the frameless window."""
        self.monkeypatch.setitem(hudshell.SHELLS, "browser", self.maker("browser", fail=True))
        assert hudshell.open_window("u").kind == "webview"
        assert self.asked == ["browser", "webview"]

    def test_nothing_that_opens_still_says_where_the_hud_is(self, capsys):
        for name in ("webview", "browser", "none"):
            self.monkeypatch.setitem(hudshell.SHELLS, name, self.maker(name, fail=True))
        # The last resort is the real one: the URL is the whole point.
        assert hudshell.open_window("u").kind == "none"
        out = capsys.readouterr()
        assert "no window could be opened" in out.err
        assert "browser: browser is not here" in out.err
        assert out.out.strip() == "u"


# --- where the window was last time --------------------------------------


class TestGeometry:
    """Reopen where you left it, or in the middle if that is all we know."""

    def test_no_file_yet_is_an_empty_answer(self):
        assert hudshell._geometry() == {}

    def test_a_saved_position_comes_back(self):
        hudshell._save_geometry({"x": 10, "y": 20, "w": 800, "h": 600})
        assert hudshell._geometry() == {"x": 10, "y": 20, "w": 800, "h": 600}

    def test_a_file_that_is_not_an_object_is_ignored(self):
        hudshell.GEOMETRY.write_text(json.dumps([1, 2]))
        assert hudshell._geometry() == {}

    def test_unreadable_json_is_ignored(self):
        hudshell.GEOMETRY.write_text("{not json")
        assert hudshell._geometry() == {}

    def test_a_position_that_cannot_be_written_is_not_a_crash(self, monkeypatch, home):
        # Somewhere the directory cannot even be made, on the way out of a
        # window that worked fine. A small annoyance next time, not a crash.
        blocker = home / "blocker"
        blocker.write_text("")
        monkeypatch.setattr(hudshell, "GEOMETRY", blocker / "sub" / "hud.json")
        hudshell._save_geometry({"x": 1})
        assert blocker.is_file()


# --- the webview itself --------------------------------------------------


class FakeWindow:
    def __init__(self, title=""):
        self.title = title
        self.handlers = {}
        self.size = (1280, 820)
        self.pos = (0, 0)
        self.default_size = None
        self.moved = None
        self.position = None
        self.decorated = None
        self.above = False
        self.child = None
        self.min_size = None
        self.maximized = False
        self.shown = False
        self.move_drags = []
        self.resize_drags = []

    def set_default_size(self, w, h):
        self.default_size = (w, h)

    def move(self, x, y):
        self.moved = (x, y)

    def set_position(self, p):
        self.position = p

    def set_decorated(self, on):
        self.decorated = on

    def set_keep_above(self, on):
        self.above = on

    def add(self, child):
        self.child = child

    def set_size_request(self, w, h):
        self.min_size = (w, h)

    def connect(self, name, cb):
        self.handlers[name] = cb

    def show_all(self):
        self.shown = True

    def is_maximized(self):
        return self.maximized

    def maximize(self):
        self.maximized = True

    def unmaximize(self):
        self.maximized = False

    def get_position(self):
        return self.pos

    def get_size(self):
        return self.size

    def begin_move_drag(self, *a):
        self.move_drags.append(a)

    def begin_resize_drag(self, *a):
        self.resize_drags.append(a)


class FakeContentManager:
    def __init__(self):
        self.handler = None
        self.registered = []

    def connect(self, name, cb):
        self.handler = cb

    def register_script_message_handler(self, name):
        self.registered.append(name)


class FakeWebView:
    def __init__(self):
        self.ucm = FakeContentManager()
        self.settings = types.SimpleNamespace(
            devtools=None,
            set_enable_developer_extras=lambda on: setattr(self.settings, "devtools", on),
        )
        self.background = None
        self.uri = None

    def get_settings(self):
        return self.settings

    def set_background_color(self, c):
        self.background = c

    def get_user_content_manager(self):
        return self.ucm

    def load_uri(self, url):
        self.uri = url


class FakeGtk:
    WindowPosition = types.SimpleNamespace(CENTER="center")

    def __init__(self):
        self.windows = []
        self.quits = 0
        self.on_main = lambda: None

    def Window(self, title=""):  # noqa: N802 -- this is GTK's own spelling
        win = FakeWindow(title)
        self.windows.append(win)
        return win

    def main(self):
        self.on_main()

    def main_quit(self):
        self.quits += 1


class FakePointer:
    def __init__(self, pos=(0, 41, 42)):
        self.pos = pos
        self.raises = False

    def get_position(self):
        if self.raises:
            raise RuntimeError("no seat")
        return self.pos


@pytest.fixture
def gtk(monkeypatch):
    """`gi` and the four GTK modules the webview needs, faked into `sys.modules`.

    Returns a namespace with the fakes on it, so a test can reach the window
    that `run_webview` created and call the handlers it connected.
    """
    pointer = FakePointer()
    gtk = FakeGtk()
    seat = types.SimpleNamespace(get_pointer=lambda: pointer)
    display = types.SimpleNamespace(
        get_default=lambda: types.SimpleNamespace(get_default_seat=lambda: seat)
    )
    gdk = types.SimpleNamespace(
        WindowEdge=types.SimpleNamespace(
            NORTH_WEST="nw",
            NORTH="n",
            NORTH_EAST="ne",
            WEST="w",
            EAST="e",
            SOUTH_WEST="sw",
            SOUTH="s",
            SOUTH_EAST="se",
        ),
        RGBA=lambda *a: ("rgba", a),
        Display=display,
        CURRENT_TIME=0,
        keyval_name=lambda k: k,
    )
    webview = FakeWebView()
    webkit = types.SimpleNamespace(WebView=lambda: webview)
    added = []
    glib = types.SimpleNamespace(
        PRIORITY_DEFAULT=0,
        unix_signal_add=lambda prio, sig, cb: added.append(("glib", sig)),
    )
    repository = types.ModuleType("gi.repository")
    repository.Gtk = gtk
    repository.Gdk = gdk
    repository.WebKit2 = webkit
    repository.GLib = glib
    gi = types.ModuleType("gi")
    gi.require_version = lambda *a: None
    gi.repository = repository
    monkeypatch.setitem(sys.modules, "gi", gi)
    monkeypatch.setitem(sys.modules, "gi.repository", repository)
    return types.SimpleNamespace(
        gi=gi,
        repository=repository,
        gtk=gtk,
        gdk=gdk,
        webview=webview,
        glib=glib,
        pointer=pointer,
        added=added,
    )


class TestRunWebview:
    """The window itself: how it opens, and the bridge that gives it a title bar."""

    def test_it_opens_undecorated_above_everything_on_the_url(self, gtk):
        assert hudshell.run_webview("http://127.0.0.1:9/") == 0
        win = gtk.gtk.windows[0]
        assert win.decorated is False
        assert win.above is True
        assert win.shown is True
        assert gtk.webview.uri == "http://127.0.0.1:9/"
        assert gtk.webview.ucm.registered == ["hud"]

    def test_with_nothing_remembered_it_opens_in_the_middle(self, gtk):
        hudshell.run_webview("u")
        win = gtk.gtk.windows[0]
        assert win.position == "center"
        assert win.moved is None
        assert win.default_size == (1280, 820)

    def test_it_reopens_where_it_was_left(self, gtk):
        hudshell._save_geometry({"x": 30, "y": 40, "w": 900, "h": 700})
        hudshell.run_webview("u")
        win = gtk.gtk.windows[0]
        assert win.moved == (30, 40)
        assert win.default_size == (900, 700)
        assert win.position is None

    def test_the_config_can_ask_for_a_title_bar_and_no_keep_above(self, gtk, shell_config):
        shell_config(
            "[hud]\ndecorated = true\non_top = false\ndevtools = true\n"
            "min_width = 600\nmin_height = 400\ntitle = 'Reactor'\n"
        )
        hudshell.run_webview("u")
        win = gtk.gtk.windows[0]
        assert win.title == "Reactor"
        assert win.decorated is True
        assert win.above is False
        assert win.min_size == (600, 400)
        assert gtk.webview.settings.devtools is True

    def test_signals_are_added_through_glib(self, gtk):
        # A plain Python handler never runs while the interpreter is inside
        # Gtk.main(), so SIGTERM has to go through the loop that is running.
        hudshell.run_webview("u")
        assert [sig for (_who, sig) in gtk.added] == [15, 2, 1]

    def test_the_newer_glib_spelling_is_used_when_it_exists(self, gtk):
        seen = []
        gtk.repository.GLibUnix = types.SimpleNamespace(
            signal_add=lambda prio, sig, cb: seen.append(sig)
        )
        hudshell.run_webview("u")
        assert seen == [15, 2, 1] and gtk.added == []

    def test_a_signal_that_cannot_be_added_is_not_fatal(self, gtk):
        def boom(*_a):
            raise ValueError("not on this platform")

        gtk.glib.unix_signal_add = boom
        assert hudshell.run_webview("u") == 0

    def test_escape_closes_the_window(self, gtk):
        hudshell.run_webview("u")
        key = gtk.gtk.windows[0].handlers["key-press-event"]
        assert key(None, types.SimpleNamespace(keyval="Escape")) is False
        assert gtk.gtk.quits == 1
        assert key(None, types.SimpleNamespace(keyval="a")) is False
        assert gtk.gtk.quits == 1

    def test_moving_the_window_is_written_down_on_the_way_out(self, gtk):
        def drag_it():
            win = gtk.gtk.windows[0]
            win.pos, win.size = (11, 22), (640, 480)
            win.handlers["configure-event"]()

        gtk.gtk.on_main = drag_it
        hudshell.run_webview("u")
        assert hudshell._geometry() == {"x": 11, "y": 22, "w": 640, "h": 480}

    def test_a_maximised_window_is_not_a_position_worth_keeping(self, gtk):
        def maximise():
            win = gtk.gtk.windows[0]
            win.maximized = True
            assert win.handlers["configure-event"]() is False

        gtk.gtk.on_main = maximise
        hudshell.run_webview("u")
        assert hudshell._geometry() == {}

    def test_destroy_quits_the_loop(self, gtk):
        hudshell.run_webview("u")
        gtk.gtk.windows[0].handlers["destroy"]()
        assert gtk.gtk.quits == 1


class TestPageBridge:
    """What the page asks for, and what the window does about it.

    A frameless window has no title bar, so drag, maximise and close come back
    as messages from the page instead.
    """

    @pytest.fixture
    def bridge(self, gtk):
        """(the message handler, the fakes) for a window that is already open."""
        hudshell.run_webview("u")

        def send(msg):
            gtk.webview.ucm.handler(None, types.SimpleNamespace(get_js_value=lambda: msg))

        return send, gtk

    def test_close_quits(self, bridge):
        send, gtk = bridge
        send(types.SimpleNamespace(to_string=lambda: "close"))
        assert gtk.gtk.quits == 1

    def test_drag_moves_the_window_from_the_pointer(self, bridge):
        send, gtk = bridge
        send(types.SimpleNamespace(to_string=lambda: "drag"))
        # The pointer's position, not the page's: the page's are the view's.
        assert gtk.gtk.windows[0].move_drags == [(1, 41, 42, 0)]

    def test_a_drag_with_no_pointer_to_ask_is_dropped(self, bridge):
        send, gtk = bridge
        gtk.pointer.raises = True
        send(types.SimpleNamespace(to_string=lambda: "drag"))
        assert gtk.gtk.windows[0].move_drags == []

    def test_maximize_toggles(self, bridge):
        send, gtk = bridge
        win = gtk.gtk.windows[0]
        send(types.SimpleNamespace(to_string=lambda: "maximize"))
        assert win.maximized is True
        send(types.SimpleNamespace(to_string=lambda: "maximize"))
        assert win.maximized is False

    def test_an_edge_strip_starts_a_resize(self, bridge):
        send, gtk = bridge
        send(types.SimpleNamespace(to_string=lambda: "resize:se"))
        assert gtk.gtk.windows[0].resize_drags == [("se", 1, 41, 42, 0)]

    def test_an_edge_that_is_not_one_is_ignored(self, bridge):
        send, gtk = bridge
        send(types.SimpleNamespace(to_string=lambda: "resize:middle"))
        assert gtk.gtk.windows[0].resize_drags == []

    def test_a_resize_with_no_pointer_to_ask_is_dropped(self, bridge):
        send, gtk = bridge
        gtk.pointer.raises = True
        send(types.SimpleNamespace(to_string=lambda: "resize:n"))
        assert gtk.gtk.windows[0].resize_drags == []

    def test_a_message_that_will_not_read_is_dropped(self, bridge):
        send, gtk = bridge

        def boom():
            raise RuntimeError("not a string")

        gtk.webview.ucm.handler(None, types.SimpleNamespace(get_js_value=boom))
        assert gtk.gtk.quits == 0

    def test_an_unknown_message_does_nothing(self, bridge):
        send, gtk = bridge
        send(types.SimpleNamespace(to_string=lambda: "explode"))
        assert gtk.gtk.quits == 0


class TestMain:
    """Run directly, this module is the window."""

    def test_no_url_is_a_usage_error(self, capsys):
        assert hudshell.main([]) == 2
        assert "usage: hudshell.py <url>" in capsys.readouterr().err

    def test_the_first_argument_is_the_url(self, monkeypatch):
        seen = []
        monkeypatch.setattr(hudshell, "run_webview", lambda url: seen.append(url) or 0)
        assert hudshell.main(["http://127.0.0.1:9/", "ignored"]) == 0
        assert seen == ["http://127.0.0.1:9/"]
