"""Shared test harness.

Two things have to be true before a single module of the package is imported,
and neither can be arranged from inside a fixture:

  * ``CLAUDE_VOICE_HOME`` must point somewhere disposable. ``config.BASE`` is
    read from it at import time and every path in the package hangs off that
    one value, so a test session that imports first and redirects later has
    already written to the developer's real config directory.
  * The ambient credentials and desktop have to be gone. A machine with
    ``ANTHROPIC_API_KEY`` set would run the acknowledgement against the live
    API from a unit test, and a machine with a display would open windows.

So this happens at module scope, in a file pytest imports before it collects
anything.
"""

import os
import shutil
import sys
import tempfile
from pathlib import Path

_HOME = Path(tempfile.mkdtemp(prefix="claude-voice-tests-"))
os.environ["CLAUDE_VOICE_HOME"] = str(_HOME)
for _leak in (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_VOICE_SESSION",
    "TMUX",
    "TMUX_PANE",
    "DISPLAY",
    "WAYLAND_DISPLAY",
):
    os.environ.pop(_leak, None)
os.environ["CLAUDE_VOICE_TESTING"] = "1"

import pytest  # noqa: E402

# The package inserts its own directory on sys.path and imports its siblings by
# bare name (`import config`), which normally produces a SECOND module object
# over the same file -- `claude_voice.config` and `config`, each with its own
# `_cached`. Under test that is a trap with no upside: reloading one leaves the
# other holding a configuration that points at a directory the last test
# deleted, so `write_config` would appear to do nothing.
#
# Registering the package module under the bare name first makes every
# `import config` inside the package resolve to this one object. It has to
# happen here, before any module of the package is imported.
import claude_voice.config as config  # noqa: E402

sys.modules.setdefault("config", config)

# The bundled plugins import these two by bare name as well, and they carry
# caches of their own: a test that stubs `claude_voice.repo.info` while a
# plugin reads a second copy of it would shell out to `gh` for real. Imported
# after `config` is registered, because they import it themselves and would
# otherwise be the ones to create the duplicate this file exists to prevent.
import claude_voice.repo as repo  # noqa: E402
import claude_voice.sysstat as sysstat  # noqa: E402

sys.modules.setdefault("repo", repo)
sys.modules.setdefault("sysstat", sysstat)

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "claude_voice"


@pytest.fixture(autouse=True)
def home(monkeypatch):
    """An empty ``CLAUDE_VOICE_HOME`` per test, with the config cache dropped.

    Yields the directory. Anything the package writes -- state files, logs, the
    queue, the preset marker -- lands in here and is gone by the next test.
    """
    if _HOME.exists():
        shutil.rmtree(_HOME)
    _HOME.mkdir(parents=True)
    config.load(reload=True)
    yield _HOME
    config.load(reload=True)


@pytest.fixture
def cfg(home):
    """The freshly loaded configuration, for a test that wants to read a key."""
    return config.load(reload=True)


@pytest.fixture
def write_config(home):
    """Write a ``config.toml`` into the test home and reload.

    Takes TOML source, returns the reloaded configuration::

        cfg = write_config("[tts]\\nlength_scale = 2.0\\n")
    """

    def _write(toml_source: str):
        (home / "config.toml").write_text(toml_source)
        return config.load(reload=True)

    return _write


@pytest.fixture
def no_subprocess(monkeypatch):
    """Fail the test if anything shells out.

    The default for a unit test: this package spawns players, recorders and
    window managers, and a suite that actually launches them is a suite that
    behaves differently on the machine it runs on. Patch a specific call
    yourself when the point of the test is what gets spawned.
    """
    import subprocess

    calls = []

    def _forbidden(name):
        def _fn(*a, **kw):
            calls.append((name, a, kw))
            raise AssertionError(f"the test shelled out: subprocess.{name}{a[:1]}")

        return _fn

    for name in ("run", "Popen", "call", "check_call", "check_output"):
        monkeypatch.setattr(subprocess, name, _forbidden(name))
    return calls


@pytest.fixture
def fake_proc():
    """A stand-in for ``subprocess.Popen`` / ``CompletedProcess``.

    ``fake_proc(stdout="x", returncode=0)`` returns an object answering the
    handful of attributes this package reads off a child: ``stdout``,
    ``stderr``, ``returncode``, ``pid``, ``poll``, ``wait``, ``kill``,
    ``terminate`` and ``communicate``.
    """

    def _make(stdout="", stderr="", returncode=0, pid=4242):
        class _P:
            def __init__(self):
                self.stdout = stdout
                self.stderr = stderr
                self.returncode = returncode
                self.pid = pid
                self.killed = False
                self.terminated = False
                self.args = []

            def poll(self):
                return self.returncode

            def wait(self, timeout=None):
                return self.returncode

            def kill(self):
                self.killed = True

            def terminate(self):
                self.terminated = True

            def communicate(self, input=None, timeout=None):
                return self.stdout, self.stderr

        return _P()

    return _make


@pytest.fixture
def hook_payload():
    """A Claude Code hook event, as the modules read it off stdin.

    ``hook_payload(session_id="s1", transcript_path=...)`` returns the dict;
    pair it with ``feed_stdin`` to hand it to a module's ``main``.
    """

    def _make(**over):
        payload = {
            "session_id": "test-session",
            "transcript_path": str(_HOME / "transcript.jsonl"),
            "cwd": str(_HOME),
            "hook_event_name": "Stop",
        }
        payload.update(over)
        return payload

    return _make


@pytest.fixture
def feed_stdin(monkeypatch):
    """Put text -- or a JSON-serialisable object -- on ``sys.stdin``."""
    import io
    import json

    def _feed(data):
        if not isinstance(data, str):
            data = json.dumps(data)
        monkeypatch.setattr(sys, "stdin", io.StringIO(data))
        return data

    return _feed
