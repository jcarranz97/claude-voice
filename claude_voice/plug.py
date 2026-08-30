"""Finding plugins, and asking them for things.

Two rules shape everything here.

The first is that a manifest is read and code is not. A hook is a process
that lives for a few milliseconds and dies, several times a turn, and there
is nowhere to keep a loaded plugin between them. Scanning for installed
distributions costs tens of milliseconds before a single import, and an
import costs whatever the plugin imports -- so nothing is imported until
something actually asks for the thing it contributes.

The second is that a plugin is less trusted than we are. Every call into
one is guarded and counted, and a plugin that raises or overruns three
times is set aside for the rest of the process. A window that goes blank
because somebody's panel threw is our bug, not theirs.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
import time
import tomllib
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:  # siblings are imported by bare name here
    sys.path.insert(0, str(HERE))

import config as _config  # noqa: E402
from __init__ import __version__ as _VERSION  # noqa: E402

BUNDLED = HERE / "plugins"
INSTALLED = _config.BASE / "plugins"

# How long a plugin may hold the drawing path before it is abandoned. It is
# not a cancellation -- the call keeps running in its own time -- it is the
# point at which we stop waiting and stop trusting.
DEFAULT_TIMEOUT = 0.25
STRIKES = 3


def cfg():
    """The configuration, now.

    Read through rather than held: this module is imported by hooks that
    live for milliseconds and by a window that outlives a config change,
    and `load()` is cached anyway, so the only cost of asking again is the
    one the HUD wants to pay when it reloads.
    """
    return _config.load()


_index: dict[str, dict] | None = None
_loaded: dict[str, object] = {}
_strikes: dict[str, int] = {}
_blocked: set[str] = set()
_errors: dict[str, str] = {}


def _read(path: Path) -> dict | None:
    """A manifest, or None. A bad manifest is a plugin that does not exist."""
    try:
        with open(path, "rb") as fh:
            got = tomllib.load(fh)
    except Exception as e:  # noqa: BLE001 -- a typo must not stop the window
        _errors[path.parent.name] = f"unreadable manifest: {e}"
        return None
    name = got.get("plugin", {}).get("name")
    if not name or name != path.parent.name:
        # The directory is the namespace. A manifest that disagrees with it
        # would make `plugin disable <name>` ambiguous.
        _errors[path.parent.name] = "name does not match the directory"
        return None
    got["_dir"] = path.parent
    got["_bundled"] = BUNDLED in path.parents
    return got


def index(reload: bool = False) -> dict[str, dict]:
    """Every manifest on the machine, by name. Yours shadows what ships."""
    global _index
    if _index is not None and not reload:
        return _index
    found: dict[str, dict] = {}
    for base in (BUNDLED, INSTALLED):  # installed second: it wins
        if not base.is_dir():
            continue
        for entry in sorted(base.iterdir()):
            got = _read(entry / "plugin.toml") if entry.is_dir() else None
            if got:
                found[got["plugin"]["name"]] = got
    _index = found
    return found


def enabled(name: str) -> bool:
    """The one answer, from the one table. A plugin absent from it takes
    whatever its manifest asked for."""
    if name in _blocked:
        return False
    man = index().get(name)
    if not man:
        return False
    default = bool(man.get("options", {}).get("default_enabled", True))
    # A panel that became a plugin keeps the key that used to switch it off.
    # Somebody with `hud.github = false` set must not have the panel come
    # back because it moved house.
    # A panel usually had more than one way to switch it off -- the block and
    # the question behind it -- and every one of them still has to work.
    legacy = man.get("options", {}).get("legacy_key") or []
    for key in [legacy] if isinstance(legacy, str) else legacy:
        default = default and bool(cfg().get(key, True))
    return bool(cfg().get(f"plugins.enabled.{name}", default))


def provides(kind: str) -> list[str]:
    """Enabled plugins that say they contribute this, in a stable order.

    Read out of the manifests, so a machine with a dozen plugins and none
    of them listening costs one dictionary lookup rather than a dozen
    imports.
    """
    out = []
    for name, man in index().items():
        if kind in man.get("provides", {}) and enabled(name):
            out.append(name)
    return sorted(out, key=lambda n: (order(n), n))


def order(name: str) -> int:
    return int(cfg().get(f"plugins.{name}.order", 50))


def slot(name: str) -> str:
    return str(cfg().get(f"plugins.{name}.slot", "right"))


def _entry(name: str, kind: str):
    """Import the plugin, once, and only now."""
    key = f"{name}:{kind}"
    if key in _loaded:
        return _loaded[key]
    man = index()[name]
    target = man.get("entrypoints", {}).get(kind)
    if not target:
        raise LookupError(f"{name} declares {kind} but names no entry point")
    mod_name, _, attr = target.partition(":")

    # Loaded from the plugin's own directory under a name of our making,
    # rather than by putting that directory on sys.path and importing by
    # bare name. Two plugins that both call their module `panel` are the
    # normal case, and the second one would otherwise be handed the first
    # out of the module cache -- which is a bug that looks like the wrong
    # panel being drawn rather than like an import problem.
    file = man["_dir"] / f"{mod_name.replace('.', '/')}.py"
    unique = f"claude_voice._plugin_{name}_{mod_name}"
    mod = sys.modules.get(unique)
    if mod is None:
        spec = importlib.util.spec_from_file_location(unique, file)
        if spec is None or spec.loader is None:
            raise ImportError(f"{name}: cannot load {file}")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[unique] = mod  # before exec, so the module can import itself
        spec.loader.exec_module(mod)
    fn = getattr(mod, attr) if attr else mod
    _loaded[key] = fn
    return fn


def call(name: str, kind: str, *args, **kw):
    """Ask a plugin for something. Returns None rather than raising.

    Three failures of any kind and the plugin is set aside until this
    process ends. Said once: a plugin that fails repeatedly must not
    narrate its own decline.
    """
    if name in _blocked:
        return None
    start = time.monotonic()
    try:
        got = _entry(name, kind)(*args, **kw)
    except Exception as e:  # noqa: BLE001 -- this is the whole point
        _fail(name, f"{kind}: {e.__class__.__name__}: {e}")
        return None
    spent = time.monotonic() - start
    limit = float(cfg().get("plugins.timeout", DEFAULT_TIMEOUT))
    if spent > limit:
        # It answered, but too late to be trusted with the next frame.
        _fail(name, f"{kind}: took {spent:.2f}s, over {limit:.2f}s")
    return got


def _fail(name: str, why: str) -> None:
    _strikes[name] = _strikes.get(name, 0) + 1
    _errors[name] = why
    if _strikes[name] >= STRIKES:
        _blocked.add(name)
        print(f"claude-voice: disabling plugin {name}: {why}", file=sys.stderr)


def errors() -> dict[str, str]:
    """What went wrong, for `doctor` to report and for nobody to guess at."""
    return dict(_errors)


def blocked() -> set[str]:
    return set(_blocked)


def panels(path: str = "") -> list[dict]:
    """Every enabled panel, drawn or not by the surface that asked.

    A panel that returns nothing takes no space, which is how a rail with
    several plugins in it stays readable.
    """
    out = []
    for name in provides("panel"):
        got = call(name, "panel", _Ctx(name, path))
        if not got or not got.get("rows"):
            continue
        out.append(
            {
                "plugin": name,
                "title": got.get("title", name),
                "rows": got.get("rows", []),
                "slot": slot(name),
                "order": order(name),
                "action": got.get("action"),
            }
        )
    return out


class _Ctx:
    """What a plugin is handed. Small on purpose: everything here is either
    already known or already ours, so nothing in it can block."""

    def __init__(self, name: str, path: str = ""):
        self.name = name
        self.cfg = cfg()
        self.path = path  # the directory the watched session is in
        self.data = INSTALLED / "data" / name

    def get(self, key: str, default=None):
        """This plugin's own setting, from its own section."""
        return cfg().get(f"plugins.{self.name}.{key}", default)

    def store(self) -> Path:
        self.data.mkdir(parents=True, exist_ok=True)
        return self.data


def reset() -> None:
    """Forget everything. For the tests, and for a config reload."""
    global _index
    _index = None
    # The imported modules go too. A plugin of yours that shadows one that
    # ships has the same name, and leaving the old module in the cache would
    # mean the wrong one is still being drawn after a reload.
    for key in [k for k in sys.modules if k.startswith("claude_voice._plugin_")]:
        del sys.modules[key]
    _loaded.clear()
    _strikes.clear()
    _blocked.clear()
    _errors.clear()


def contributes(name: str) -> str:
    """What a plugin actually adds. `surfaces` is a claim about where its
    output may go, not a thing it contributes, and listing it among them
    reads as though a plugin could provide a surface."""
    got = index()[name].get("provides", {})
    return ",".join(sorted(k for k in got if k != "surfaces"))


def version(name: str) -> str:
    """A bundled plugin has no version of its own: it is the application's."""
    man = index()[name]
    if man["_bundled"]:
        return _VERSION
    return man["plugin"].get("version", "?")


def _rows() -> list[tuple]:
    out = []
    for name in sorted(index()):
        man = index()[name]
        out.append(
            (
                name,
                version(name),
                "bundled" if man["_bundled"] else "installed",
                "on" if enabled(name) else "off",
                contributes(name),
                man["plugin"].get("description", ""),
            )
        )
    return out


def main(argv=None) -> int:
    """claude-voice plugin ..."""
    args = list(sys.argv[1:] if argv is None else argv)
    cmd = args[0] if args else "list"

    if cmd in ("-h", "--help", "help"):
        print(
            "usage: claude-voice plugin [list|info <name>|enable <name>|disable <name>]\n"
            "\n"
            "  list             every plugin on the machine, and whether it is on\n"
            "  info <name>      its manifest, where it came from, and its last error\n"
            "  enable <name>    the line to add to your config to switch it on\n"
            "  disable <name>   and the line to switch it off\n"
        )
        return 0

    if cmd == "list":
        rows = _rows()
        if not rows:
            print("no plugins")
            return 0
        w = max(len(r[0]) for r in rows)
        for name, ver, where, state, kinds, desc in rows:
            print(f"{name:<{w}}  {state:<3}  {where:<9}  {ver:<7}  {kinds:<12}  {desc}")
        return 0

    if cmd in ("info", "enable", "disable"):
        if len(args) < 2:
            print(f"usage: claude-voice plugin {cmd} <name>", file=sys.stderr)
            return 2
        name = args[1]
        man = index().get(name)
        if not man:
            print(f"no such plugin: {name}", file=sys.stderr)
            return 2
        if cmd == "info":
            print(f"{name} {version(name)}")
            print(f"  {man['plugin'].get('description', '')}")
            print(f"  from       {'the wheel' if man['_bundled'] else man['_dir']}")
            print(f"  provides   {contributes(name) or 'nothing'}")
            print(f"  state      {'enabled' if enabled(name) else 'disabled'}")
            if name in errors():
                print(f"  last error {errors()[name]}")
            return 0
        # Writing somebody's config file is not something to do behind their
        # back: it is hand-written, heavily commented, and ours to read.
        want = "true" if cmd == "enable" else "false"
        print(f"add this to {_config.CONFIG}:\n\n[plugins.enabled]\n{name} = {want}")
        return 0

    print(f"unknown: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001 -- never break the session
        sys.exit(0)
