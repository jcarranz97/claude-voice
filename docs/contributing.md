---
title: Contributing
---

# Contributing

Pull requests are welcome. This page is the mechanics: how to get a working checkout, what CI will ask of your branch, and how to write a test in a package that opens microphones for a living.

## A working checkout

The project is a [uv](https://docs.astral.sh/uv/) project. With uv installed:

```bash
git clone https://github.com/jcarranz97/claude-voice
cd claude-voice
uv sync --group dev
```

That is enough to run the tests. To actually *use* the branch you are working on — voice, ear and HUD — install it as a tool from the checkout:

```bash
./install.sh --editable
```

That is the same setup script the [quickstart](index.md#quickstart) runs, plus `--editable`: it installs the checkout itself rather than a copy of it, so an edit is live with nothing to reinstall. It also fetches a voice, writes a config and merges the hooks, which is what makes a branch actually speak.

If you have those already, the program alone is:

```bash
uv tool install --force --refresh --editable .
```

!!! danger "`--refresh` matters"

    Without it a reinstall can quietly keep the old build, and you will be testing code you did not write. It reports success either way.

## The three things CI checks

Three commands, with no arguments. Everything they do is configured in `pyproject.toml` rather than in the workflow file, so if they pass on your machine they pass on the runner.

```bash
uv run --group dev ruff check .          # lint
uv run --group dev ruff format .         # format, in place
uv run --group dev pytest                # tests
```

CI runs `ruff format --check` instead of the in-place version, so run the formatter before you push.

Coverage is gated: the suite has to hold at least **95 percent** of the package, and the run fails below that. A pull request that adds code adds tests for it.

The test matrix is Python 3.11, 3.12 and 3.13. Locally 3.11 is enough.

## Writing tests

`tests/conftest.py` is the harness and it is worth reading before you write a test. The important parts:

- **`CLAUDE_VOICE_HOME` is redirected** to a throwaway directory before a single module of the package is imported, and emptied again between tests. Every path in the package hangs off that one value, so nothing a test writes can reach your real config.
- **Credential and desktop environment variables are stripped**, so a test that forgets to patch the API client fails loudly instead of spending money.
- **Fixtures cover the awkward boundaries**: `no_subprocess` fails a test that shells out, `fake_proc` stands in for a child process, and `hook_payload` with `feed_stdin` hands a module the JSON event Claude Code would have given it.

Three rules follow from what this package does:

1. **Nothing real.** No microphone opened, no audio played, no window drawn, no network reached, no model downloaded, no process spawned. Patch the boundary.
2. **Nothing slow.** Anything with a sleep, a timeout or a poll loop is driven by a patched clock or a bounded fake. A test that waits is a bug in the test.
3. **Nothing left behind.** No thread, no socket, no file outside the test home.

There is one test module per source module, named to match. [Modules](reference/modules.md) is the map.

## Style

Comments here explain *why*, not what, and the modules carry long docstrings about the decisions behind them. Match that.

Line length is 100 and the formatter owns it — do not hand-wrap around it.

??? info "Why the lint config disables `E402`"

    Every module in the package is also a hook entry point, run directly as a script by Claude Code. So modules do not import each other as a package: each inserts its own directory on `sys.path` and imports its siblings by bare name, which means the import cannot be at the top of the file.

    The broad `except Exception` throughout is the design for the same reason — a hook that raises is a hook that breaks somebody's editor.

## Working on the documentation

The site is [MkDocs Material](https://squidfunk.github.io/mkdocs-material/), built from `docs/` with `mkdocs.yml` at the root.

```bash
uv run --group docs mkdocs serve     # live reload at 127.0.0.1:8000
uv run --group docs mkdocs build --strict
```

`--strict` is what CI runs, and it is what the pull request has to pass: a dead cross-reference or a page missing from the nav is a red X rather than a broken link a reader finds for us later.

The site publishes to GitHub Pages from `main` automatically. Nothing is committed to a branch by that workflow, and there is no `gh-pages` branch.

A few conventions worth keeping:

- **Every page is in the nav.** `strict` enforces it.
- **Links between pages are relative** and include the `.md` extension — `[The ear](ear.md)`, `[Settings](reference/settings.md)` — so they resolve in the built site and on GitHub both.
- **Screenshots live in `docs/assets/`** and are shared with the README, which links to the same files.
- **The prose explains why.** Same rule as the comments: a page that only restates what a flag is called has not earned its place next to `--help`.

## Opening a pull request

- Branch off `main`.
- One concern per pull request; the commit history here is one merge per feature and reads as a changelog.
- Say what was got wrong first. The design pages are written that way because the reasoning is the part that is hard to recover later.
