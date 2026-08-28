#!/usr/bin/env python3
"""The window the web HUD lives in, so that "web" does not mean "a tab".

A browser tab was never the point. The point was a drawing surface: curves,
glow, a real proportional layout -- none of which a character grid can do. A
tab would have brought the address bar, the tab strip, the bookmark bar, and a
window that closes when you close the wrong thing.

So the page opens in a window with none of that, chosen in this order:

  webview    WebKitGTK, through the system PyGObject. Frameless, and it can
             be told to stay above other windows. It paints in a quarter of a
             second and costs about a third of what a browser does. This is
             the one that looks like the design.

  browser    Chrome or Chromium in --app mode, with a profile of its own so
             it is a separate window and not a tab in the browser you are
             using. It keeps a title bar -- Chrome refuses to drop it on
             Linux, deliberately -- but it renders identically and needs
             nothing installed.

  none       print the URL and let you open it. For a second screen, a
             tablet on the desk, or a machine with no desktop at all.

Set `hud.shell` in config.toml to pin one. `auto` tries them in that order.

Two environment variables the launcher sets, and the reason for each, because
both are the difference between a window and a bug report:

  GDK_BACKEND=x11   GNOME's Wayland compositor refuses "keep above" to every
                    toolkit -- GTK, Qt, Electron and Tauri alike. Under
                    XWayland it works. A status window that cannot stay in
                    front of your terminal is not a status window.

  WEBKIT_DISABLE_DMABUF_RENDERER=1
                    WebKitGTK's newer renderer paints a blank white window on
                    NVIDIA and on several compositors. Disabling it costs
                    nothing visible and is the difference between a HUD and a
                    white rectangle.

Run directly to open a window on a URL, which is how the webview shell is
started: it needs an interpreter with PyGObject, and that is usually the
system one rather than the tool's own environment.

    python3 hudshell.py <url>
"""

import json
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import config as _config  # noqa: E402

# Chrome's own state, kept apart from yours: a shared profile would make this
# a window of your browser, closing with it and carrying its extensions.
PROFILE = _config.BASE / "hud-profile"

# Where you last put the window, and how big you last made it. A frameless
# window opens wherever the compositor feels like putting it, which is the
# middle, every time -- and having dragged it somewhere sensible once, being
# given the middle again on the next run is the whole complaint.
GEOMETRY = _config.BASE / "hud-window.json"

ENV = {"GDK_BACKEND": "x11", "WEBKIT_DISABLE_DMABUF_RENDERER": "1"}

BROWSERS = (
    "google-chrome",
    "chromium",
    "chromium-browser",
    "brave-browser",
    "microsoft-edge",
    "vivaldi",
)

PROBE = (
    "import gi; gi.require_version('Gtk', '3.0');"
    "gi.require_version('Gdk', '3.0');"
    "gi.require_version('WebKit2', '4.1');"
    "from gi.repository import Gdk, Gtk, WebKit2"
)


class Shell:
    """A window we opened, and the promise to close it again."""

    def __init__(self, proc=None, kind: str = "none"):
        self.proc, self.kind = proc, kind

    def close(self) -> None:
        """The window goes when the HUD does. Without this, quitting with `q`
        leaves an empty frame pointed at a port that has stopped answering."""
        if not self.proc or self.proc.poll() is not None:
            return
        try:
            self.proc.terminate()
            self.proc.wait(timeout=3)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass


def _python_with_gi() -> str:
    """An interpreter that can import gi and WebKit2, or "".

    The tool installs into an environment of its own, and PyGObject is a
    distro package that will not be in it -- building it from source needs
    meson and a -dev package, which is exactly the kind of install this
    project does not ask for. So the window may run under a different
    interpreter than the server. They share no state, only a URL.
    """
    seen = set()
    for exe in (sys.executable, "/usr/bin/python3", shutil.which("python3")):
        if not exe or exe in seen:
            continue
        seen.add(exe)
        try:
            if subprocess.run([exe, "-c", PROBE], capture_output=True, timeout=25).returncode == 0:
                return exe
        except Exception:
            continue
    return ""


def _webview(url: str) -> Shell:
    exe = _python_with_gi()
    if not exe:
        raise RuntimeError(
            "no interpreter with PyGObject and WebKit2 (apt install python3-gi gir1.2-webkit2-4.1)"
        )
    return Shell(
        subprocess.Popen([exe, str(HERE / "hudshell.py"), url], env={**os.environ, **ENV}),
        "webview",
    )


def _browser(url: str) -> Shell:
    exe = next((b for b in BROWSERS if shutil.which(b)), "")
    if not exe:
        raise RuntimeError("no chromium-based browser to open an app window in")
    PROFILE.mkdir(parents=True, exist_ok=True)
    cmd = [
        exe,
        f"--app={url}",
        f"--user-data-dir={PROFILE}",
        "--class=claude-voice-hud",
        "--window-size=1280,820",
        "--ozone-platform=x11",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-features=Translate",
    ]
    return Shell(
        subprocess.Popen(
            cmd, env={**os.environ, **ENV}, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        ),
        "browser",
    )


def _none(url: str) -> Shell:
    print(url, flush=True)
    return Shell(None, "none")


SHELLS = {"webview": _webview, "browser": _browser, "none": _none}


def open_window(url: str, argv: list = ()) -> Shell:
    """Open the HUD, falling back rather than failing.

    A window that could not be opened must still say where the HUD is: the
    server is already running and the page is already there, so printing the
    URL turns a dead end into one more step.
    """
    want = ""
    for i, a in enumerate(argv):
        if a == "--shell" and i + 1 < len(argv):
            want = argv[i + 1]
    want = want or str(_config.load().get("hud.shell", "auto") or "auto")

    order = [want] if want in SHELLS else ["webview", "browser", "none"]
    why = []
    for name in order:
        try:
            return SHELLS[name](url)
        except Exception as e:
            why.append(f"{name}: {e}")
    print("no window could be opened:\n  " + "\n  ".join(why), file=sys.stderr)
    return _none(url)


# --- the webview itself, run as its own process --------------------------


def _geometry() -> dict:
    try:
        d = json.loads(GEOMETRY.read_text())
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save_geometry(g: dict) -> None:
    """Best effort, always. A window that could not write down where it was
    is a small annoyance next time; a window that crashed on the way out
    because of it is a bug report."""
    try:
        GEOMETRY.parent.mkdir(parents=True, exist_ok=True)
        GEOMETRY.write_text(json.dumps(g))
    except Exception:
        pass


def run_webview(url: str) -> int:
    import gi

    # Gdk is pinned as well as Gtk. Without it, gi resolves Gdk to the newest
    # typelib on the machine -- Gdk 4.0 wherever anything GTK4 is installed --
    # and then Gtk 3.0 asks for Gdk 3.0 and finds 4.0 already loaded.
    gi.require_version("Gtk", "3.0")
    gi.require_version("Gdk", "3.0")
    gi.require_version("WebKit2", "4.1")
    from gi.repository import Gdk, Gtk, WebKit2

    global EDGES
    EDGES = {
        "nw": Gdk.WindowEdge.NORTH_WEST,
        "n": Gdk.WindowEdge.NORTH,
        "ne": Gdk.WindowEdge.NORTH_EAST,
        "w": Gdk.WindowEdge.WEST,
        "e": Gdk.WindowEdge.EAST,
        "sw": Gdk.WindowEdge.SOUTH_WEST,
        "s": Gdk.WindowEdge.SOUTH,
        "se": Gdk.WindowEdge.SOUTH_EAST,
    }

    cfg = _config.load()
    win = Gtk.Window(title=(cfg.get("hud.title", "") or cfg.name).strip())

    # Reopen where you left it, at the size you left it. Position is applied
    # before the window is shown, because moving it afterwards makes it jump.
    geo = _geometry()
    win.set_default_size(int(geo.get("w") or 1280), int(geo.get("h") or 820))
    if geo.get("x") is not None and geo.get("y") is not None:
        win.move(int(geo["x"]), int(geo["y"]))
    else:
        win.set_position(Gtk.WindowPosition.CENTER)

    # Undecorated on purpose: the design has its own bar, and a title bar
    # above it would be a second one saying less. What the title bar did --
    # drag, maximise, close -- the page's own bar does instead, through the
    # bridge below.
    win.set_decorated(bool(cfg.get("hud.decorated", False)))
    if cfg.get("hud.on_top", True):
        win.set_keep_above(True)

    view = WebKit2.WebView()
    s = view.get_settings()
    s.set_enable_developer_extras(bool(cfg.get("hud.devtools", False)))
    view.set_background_color(Gdk.RGBA(0.016, 0.070, 0.047, 1.0))
    win.add(view)

    # The page cannot move its own window, and the window cannot see through
    # the WebView to know where you clicked. So the page says what it wants
    # and this end does it -- the standard WebKitGTK bridge, and the only way
    # a frameless window gets its title bar's behaviour back.
    def message(_ucm, result):
        try:
            msg = result.get_js_value().to_string()
        except Exception:
            return
        if msg == "close":
            Gtk.main_quit()
        elif msg == "drag":
            # The pointer is still down, which is what begin_move_drag needs;
            # its position comes from the seat rather than from the page,
            # because the page's coordinates are the view's, not the screen's.
            try:
                ptr = Gdk.Display.get_default().get_default_seat().get_pointer()
                _scr, x, y = ptr.get_position()
                win.begin_move_drag(1, x, y, Gdk.CURRENT_TIME)
            except Exception:
                pass
        elif msg == "maximize":
            win.unmaximize() if win.is_maximized() else win.maximize()
        elif msg.startswith("resize:"):
            # An undecorated window has no grips either, so the page puts a
            # strip along each edge and says which one you grabbed.
            edge = EDGES.get(msg.split(":", 1)[1])
            if edge is None:
                return
            try:
                ptr = Gdk.Display.get_default().get_default_seat().get_pointer()
                _scr, x, y = ptr.get_position()
                win.begin_resize_drag(edge, 1, x, y, Gdk.CURRENT_TIME)
            except Exception:
                pass

    # Small enough to tuck in a corner, not so small that the panels collapse
    # into unreadable slivers. Below this the layout stacks instead.
    win.set_size_request(
        int(cfg.get("hud.min_width", 720) or 720), int(cfg.get("hud.min_height", 520) or 520)
    )

    ucm = view.get_user_content_manager()
    ucm.connect("script-message-received::hud", message)
    ucm.register_script_message_handler("hud")
    view.load_uri(url)

    # Escape closes it too, and always will: a bridge that failed to register
    # must not leave a window with no way out.
    def key(_w, ev):
        if Gdk.keyval_name(ev.keyval) == "Escape":
            Gtk.main_quit()
        return False

    # Where it is, kept up to date as you move it rather than read at the end.
    # Quitting with `q` kills this process from outside -- the HUD shuts down
    # and closes the window it opened -- so there is no tidy exit to ask the
    # window where it was. It has to be already known.
    last = {}

    def remember(*_a):
        if not win.is_maximized():  # maximised is the screen, not a choice
            x, y = win.get_position()
            w, h = win.get_size()
            last.update(x=x, y=y, w=w, h=h)
        return False

    win.connect("key-press-event", key)
    win.connect("configure-event", remember)
    win.connect("destroy", Gtk.main_quit)
    # SIGTERM has to go through GLib: a plain Python handler does not run
    # while the interpreter is blocked inside Gtk.main(). The call moved to
    # GLibUnix at some point, and both spellings are in the wild.
    from gi.repository import GLib

    try:
        from gi.repository import GLibUnix

        add_signal = GLibUnix.signal_add
    except Exception:
        add_signal = GLib.unix_signal_add
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        try:
            add_signal(GLib.PRIORITY_DEFAULT, sig, Gtk.main_quit)
        except Exception:
            pass

    win.show_all()
    try:
        Gtk.main()
    finally:
        if last:
            _save_geometry(last)
    return 0


def main(argv: list) -> int:
    if not argv:
        print("usage: hudshell.py <url>", file=sys.stderr)
        return 2
    return run_webview(argv[0])


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
