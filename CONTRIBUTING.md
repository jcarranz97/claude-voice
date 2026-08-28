# Contributing

Pull requests are welcome. This page is only about the mechanics — how to get a
working checkout and what CI will ask of your branch.

## A working checkout

The project is a [uv](https://docs.astral.sh/uv/) project. With uv installed:

```sh
git clone https://github.com/jcarranz97/claude-voice
cd claude-voice
uv sync --group dev
```

That is enough to run the tests. To actually *use* the branch you are working
on — voice, ear and HUD — install it as a tool from the checkout:

```sh
uv tool install --refresh --editable .
```

`--refresh` matters. Without it a reinstall can quietly keep the old build and
you will be testing code you did not write.

## The three things CI checks

They are the three commands below, with no arguments, and everything they do is
configured in `pyproject.toml` rather than in the workflow file. If they pass on
your machine they pass on the runner.

```sh
uv run --group dev ruff check .          # lint
uv run --group dev ruff format .         # format, in place
uv run --group dev pytest                # tests
```

CI runs `ruff format --check` instead of the in-place version, so run the
formatter before you push. Coverage is gated: the suite has to hold at least 95
percent of the package, and the run fails below that. A pull request that adds
code adds tests for it.

The test matrix is Python 3.11, 3.12 and 3.13. Locally 3.11 is enough.

## Writing tests

`tests/conftest.py` is the harness and it is worth reading before you write a
test. The important parts:

- `CLAUDE_VOICE_HOME` is redirected to a throwaway directory before a single
  module of the package is imported, and emptied again between tests. Every
  path in the package hangs off that one value, so nothing a test writes can
  reach your real config.
- The credential and desktop environment variables are stripped, so a test that
  forgets to patch the API client fails loudly instead of spending money.
- Fixtures are there for the awkward boundaries: `no_subprocess` fails a test
  that shells out, `fake_proc` stands in for a child process, and
  `hook_payload` with `feed_stdin` hands a module the JSON event Claude Code
  would have given it.

Three rules follow from what this package does:

1. **Nothing real.** No microphone opened, no audio played, no window drawn, no
   network reached, no model downloaded, no process spawned. Patch the
   boundary.
2. **Nothing slow.** Anything with a sleep, a timeout or a poll loop is driven
   by a patched clock or a bounded fake. A test that waits is a bug in the
   test.
3. **Nothing left behind.** No thread, no socket, no file outside the test home.

## Style

Comments here explain *why*, not what, and the modules carry long docstrings
about the decisions behind them. Match that. Line length is 100 and the
formatter owns it — do not hand-wrap around it.
