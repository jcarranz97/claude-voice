"""The branch, its pull request, and whether the checks went green.

No git and no gh run here. The branch is read out of a directory laid out like
a repository -- which is all the module does anyway -- and every answer from
`gh` is a captured one, fed to a stand-in for ``subprocess.run``. The module
caches on a clock and asks in a background thread, so both are reset between
tests and the thread is never actually started.
"""

import json
import subprocess

import pytest

import claude_voice.repo as repo

Config = repo._config.Config


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    """Both caches are module state, and one of them outlives a test."""
    monkeypatch.setattr(
        repo, "_state", {"key": (), "t": 0.0, "pr": None, "gh": True, "busy": False}
    )
    monkeypatch.setattr(repo, "_branch_cache", {"where": None, "t": 0.0, "val": {}})


@pytest.fixture
def repository(home):
    """A directory shaped like a clone: only ``.git/HEAD`` is ever read."""

    def _make(name="work", head="ref: refs/heads/main\n"):
        rt = home / name
        (rt / ".git").mkdir(parents=True)
        (rt / ".git" / "HEAD").write_text(head)
        return rt

    return _make


@pytest.fixture
def gh(monkeypatch):
    """What `gh pr view` said, and the command line it was asked with."""
    calls = []

    def _answer(stdout="", stderr="", returncode=0, raises=None):
        def _run(cmd, **kw):
            calls.append((cmd, kw))
            if raises:
                raise raises
            return subprocess.CompletedProcess(cmd, returncode, stdout, stderr)

        monkeypatch.setattr(repo.subprocess, "run", _run)
        return calls

    return _answer


class TestEnabled:
    """The one thing here that leaves the machine, and its off switch."""

    def test_on_unless_it_is_turned_off(self, monkeypatch):
        assert repo.enabled() is True
        monkeypatch.setattr(repo, "CFG", Config({"plugins": {"github": {"network": False}}}))
        assert repo.enabled() is False


class TestRoot:
    """Which repository a directory is in. Walks up, the way git does."""

    def test_the_directory_itself(self, repository):
        rt = repository()
        assert repo.root(str(rt)) == rt

    def test_a_directory_below_it(self, repository):
        rt = repository()
        deep = rt / "a" / "b"
        deep.mkdir(parents=True)
        assert repo.root(str(deep)) == rt

    def test_a_file_is_asked_about_its_directory(self, repository):
        rt = repository()
        f = rt / "README.md"
        f.write_text("")
        assert repo.root(str(f)) == rt

    def test_somewhere_that_is_not_a_repository(self, home):
        assert repo.root(str(home)) is None

    def test_something_that_is_not_a_path_at_all(self):
        assert repo.root(None) is None


class TestBranch:
    """What you are on, and the honest answer when it is not a branch."""

    def test_the_name_after_refs_heads(self, repository):
        assert repo.branch(repository(head="ref: refs/heads/ci-ruff-pytest\n")) == (
            "ci-ruff-pytest",
            False,
        )

    def test_a_detached_head_is_its_short_sha(self, repository):
        rt = repository(head="4f2b9c1d8e7a6b5c4d3e2f1a0b9c8d7e6f5a4b3c\n")
        assert repo.branch(rt) == ("4f2b9c1d", True)

    def test_a_head_that_says_neither(self, repository):
        assert repo.branch(repository(head="ref: refs/tags/v1\n")) == ("", False)

    def test_no_head_to_read(self, home):
        assert repo.branch(home) == ("", False)

    def test_a_worktree_points_at_the_head_that_matters(self, home, repository):
        real = repository()
        tree = home / "tree"
        tree.mkdir()
        (tree / ".git").write_text(f"gitdir: {real / '.git'}\n")
        (real / ".git" / "HEAD").write_text("ref: refs/heads/side\n")
        assert repo.branch(tree) == ("side", False)

    def test_a_pointer_file_that_points_nowhere(self, home):
        (home / ".git").write_text("this is not a gitdir line\n")
        assert repo._head_file(home / ".git") is None

    def test_a_pointer_file_that_cannot_be_read(self, home, monkeypatch):
        (home / ".git").write_text("gitdir: /elsewhere\n")

        def refuse(self, *a, **kw):
            raise PermissionError(str(self))

        monkeypatch.setattr(repo.Path, "read_text", refuse)
        assert repo._head_file(home / ".git") is None


class TestSummarise:
    """One word for the whole suite, and the names of what is failing."""

    def test_nothing_to_report(self):
        assert repo.summarise(None)["state"] == "none"
        assert repo.summarise([])["total"] == 0

    def test_a_suite_that_passed(self):
        got = repo.summarise(
            [
                {"status": "COMPLETED", "conclusion": "SUCCESS", "name": "tests"},
                {"status": "COMPLETED", "conclusion": "SKIPPED", "name": "deploy"},
                {"status": "COMPLETED", "conclusion": "NEUTRAL", "name": "notes"},
            ]
        )
        assert (got["state"], got["pass"], got["total"]) == ("passing", 3, 3)

    def test_a_check_still_running(self):
        got = repo.summarise([{"status": "IN_PROGRESS", "conclusion": None, "name": "tests"}])
        assert (got["state"], got["running"]) == ("running", 1)

    def test_a_failure_outranks_anything_still_running(self):
        got = repo.summarise(
            [
                {"status": "IN_PROGRESS", "conclusion": None, "name": "lint"},
                {"status": "COMPLETED", "conclusion": "FAILURE", "name": "tests"},
            ]
        )
        assert got["state"] == "failing"
        assert got["failing"] == ["tests"]

    def test_only_two_names_because_a_third_red_line_is_scrolled_past(self):
        got = repo.summarise(
            [
                {"status": "COMPLETED", "conclusion": "FAILURE", "name": n}
                for n in ("one", "two", "three")
            ]
        )
        assert (got["fail"], got["failing"]) == (3, ["one", "two"])

    def test_the_older_commit_statuses_are_read_too(self):
        got = repo.summarise(
            [
                {"state": "SUCCESS", "context": "ci/build"},
                {"state": "PENDING", "context": "ci/test"},
                {"state": "ERROR", "context": "ci/deploy"},
            ]
        )
        assert (got["pass"], got["running"], got["fail"]) == (1, 1, 1)
        assert got["failing"] == ["ci/deploy"]

    def test_a_check_that_says_nothing_counts_for_nothing(self):
        assert repo.summarise([{"conclusion": ""}, {"state": ""}])["total"] == 0


class TestGh:
    """Everything `gh pr view` can say, including that it is not installed."""

    ROOT = "/tmp/does-not-matter"

    def test_a_pull_request_comes_back_summarised(self, gh):
        gh(
            stdout=json.dumps(
                {
                    "number": 30,
                    "title": "Wrap the session instead of requiring tmux",
                    "state": "OPEN",
                    "isDraft": True,
                    "statusCheckRollup": [
                        {"status": "COMPLETED", "conclusion": "SUCCESS", "name": "tests"}
                    ],
                }
            )
        )
        pr = repo._gh(repo.Path(self.ROOT), "wrap")["pr"]
        assert (pr["number"], pr["state"], pr["draft"]) == (30, "open", True)
        assert pr["checks"]["state"] == "passing"

    def test_the_question_is_asked_without_a_terminal(self, gh):
        calls = gh(stdout="{}")
        repo._gh(repo.Path(self.ROOT), "wrap")
        ((cmd, kw),) = calls
        assert cmd[:4] == ["gh", "pr", "view", "wrap"]
        assert "statusCheckRollup" in cmd[-1]
        assert kw["cwd"] == self.ROOT
        assert kw["timeout"] == repo.GH_TIMEOUT
        assert kw["stdin"] == repo.subprocess.DEVNULL
        assert kw["env"]["GH_PROMPT_DISABLED"] == "1"

    def test_no_pull_request_is_an_answer_not_a_silence(self, gh):
        gh(returncode=1, stderr='no pull requests found for branch "side"\n')
        assert repo._gh(repo.Path(self.ROOT), "side") == {"pr": None}

    @pytest.mark.parametrize(
        "said",
        [
            "gh: To use GitHub CLI in a GitHub Actions workflow, set the GH_TOKEN\nauth required",
            "fatal: not a git repository (or any of the parent directories)",
            "no git remotes found",
        ],
    )
    def test_nobody_to_ask_is_said_once_and_quietly(self, gh, said):
        gh(returncode=1, stderr=said)
        assert repo._gh(repo.Path(self.ROOT), "main") == {"gh": False}

    def test_gh_that_is_not_installed(self, gh):
        gh(raises=FileNotFoundError("gh"))
        assert repo._gh(repo.Path(self.ROOT), "main") == {"gh": False}

    def test_a_slow_network_is_not_news(self, gh):
        gh(raises=subprocess.TimeoutExpired("gh", 12))
        assert repo._gh(repo.Path(self.ROOT), "main") == {}

    def test_any_other_complaint_is_swallowed(self, gh):
        gh(returncode=1, stderr="X509 certificate has expired")
        assert repo._gh(repo.Path(self.ROOT), "main") == {}

    def test_an_answer_that_is_not_json(self, gh):
        gh(stdout="<html>a proxy login page</html>")
        assert repo._gh(repo.Path(self.ROOT), "main") == {}


class TestLocal:
    """The branch, on a clock short enough that switching shows up at once."""

    def test_the_repository_and_the_branch(self, repository):
        rt = repository()
        assert repo.local(str(rt)) == {
            "name": "work",
            "branch": "main",
            "detached": False,
            "root": str(rt),
        }

    def test_nowhere_is_nothing(self):
        assert repo.local("") == {}

    def test_the_answer_is_cached_for_a_couple_of_seconds(self, repository):
        rt = repository()
        repo.local(str(rt))
        (rt / ".git" / "HEAD").write_text("ref: refs/heads/side\n")
        assert repo.local(str(rt))["branch"] == "main"

    def test_a_stale_answer_is_read_again(self, repository):
        rt = repository()
        repo.local(str(rt))
        (rt / ".git" / "HEAD").write_text("ref: refs/heads/side\n")
        repo._branch_cache["t"] -= repo.BRANCH_TTL + 1
        assert repo.local(str(rt))["branch"] == "side"

    def test_a_different_directory_is_a_different_question(self, repository):
        first, second = repository("one"), repository("two", head="ref: refs/heads/side\n")
        assert repo.local(str(first))["branch"] == "main"
        assert repo.local(str(second))["branch"] == "side"


class TestInfo:
    """What to draw right now, without waiting for anything."""

    @pytest.fixture
    def asked(self, monkeypatch):
        """The background refresh, caught rather than run."""
        started = []

        class _Thread:
            def __init__(self, target, args, daemon=False):
                self.target, self.args = target, args

            def start(self):
                started.append(self.args)

        monkeypatch.setattr(repo.threading, "Thread", lambda **kw: _Thread(**kw))
        return started

    def test_somewhere_that_is_not_a_repository_draws_nothing(self, home, asked):
        assert repo.info(str(home)) == {}
        assert asked == []

    def test_the_branch_comes_back_before_anything_is_asked(self, repository, asked):
        rt = repository()
        out = repo.info(str(rt))
        # The branch alone, and the rest asked for behind it: "asking" is the
        # only honest thing to draw, and it is not worth a stalled frame.
        assert out == {"name": "work", "branch": "main", "detached": False, "gh": True}
        assert asked == [(rt, "main", (str(rt), "main"))]

    def test_a_detached_head_has_no_pull_request_to_ask_about(self, repository, asked):
        out = repo.info(str(repository(head="4f2b9c1d8e7a6b5c4d3e2f1a\n")))
        assert out["detached"] is True
        assert "gh" not in out
        assert asked == []

    def test_a_branch_with_no_name_is_not_asked_about(self, repository, asked):
        repo.info(str(repository(head="ref: refs/tags/v1\n")))
        assert asked == []

    def test_github_turned_off_asks_nobody(self, repository, asked, monkeypatch):
        monkeypatch.setattr(repo, "CFG", Config({"plugins": {"github": {"network": False}}}))
        out = repo.info(str(repository()))
        assert "gh" not in out
        assert asked == []

    def test_the_last_answer_is_drawn_while_the_next_one_is_asked(self, repository, asked):
        rt = repository()
        pr = {"number": 30, "checks": {"state": "passing"}}
        repo._state.update(key=(str(rt), "main"), t=repo.time.time(), pr=pr)
        out = repo.info(str(rt))
        assert out["pr"] == pr
        assert asked == []  # fresh, and nothing is waiting on it

    def test_a_running_check_is_asked_about_more_often(self, repository, asked):
        rt = repository()
        pr = {"number": 30, "checks": {"state": "running"}}
        repo._state.update(key=(str(rt), "main"), t=repo.time.time() - repo.PR_BUSY_TTL - 1, pr=pr)
        repo.info(str(rt))
        assert asked == [(rt, "main", (str(rt), "main"))]

    def test_a_settled_pull_request_is_left_alone_for_a_minute(self, repository, asked):
        rt = repository()
        pr = {"number": 30, "checks": {"state": "passing"}}
        repo._state.update(key=(str(rt), "main"), t=repo.time.time() - repo.PR_BUSY_TTL - 1, pr=pr)
        repo.info(str(rt))
        assert asked == []

    def test_without_gh_the_question_is_asked_far_less(self, repository, asked):
        rt = repository()
        repo._state.update(key=(str(rt), "main"), t=repo.time.time() - repo.PR_TTL - 1, gh=False)
        out = repo.info(str(rt))
        assert out["gh"] is False
        assert asked == []  # PR_GONE_TTL has not passed yet

        repo._state["t"] = repo.time.time() - repo.PR_GONE_TTL - 1
        repo.info(str(rt))
        assert asked == [(rt, "main", (str(rt), "main"))]

    def test_a_refresh_already_in_flight_is_not_asked_twice(self, repository, asked):
        rt = repository()
        repo._state["busy"] = True
        repo.info(str(rt))
        assert asked == []


class TestRefresh:
    """What the background answer does to the state everyone else reads."""

    def test_a_pull_request_lands_in_the_state(self, monkeypatch):
        pr = {"number": 30, "checks": {"state": "passing"}}
        monkeypatch.setattr(repo, "_gh", lambda rt, br: {"pr": pr})
        repo._state["busy"] = True
        repo._refresh(repo.Path("/x"), "main", ("/x", "main"))
        assert repo._state["pr"] == pr
        assert repo._state["gh"] is True
        assert repo._state["busy"] is False

    def test_no_gh_clears_the_pull_request(self, monkeypatch):
        monkeypatch.setattr(repo, "_gh", lambda rt, br: {"gh": False})
        repo._state.update(pr={"number": 30}, busy=True)
        repo._refresh(repo.Path("/x"), "main", ("/x", "main"))
        assert repo._state["pr"] is None
        assert repo._state["gh"] is False

    def test_an_answer_with_nothing_in_it_still_stops_the_asking(self, monkeypatch):
        monkeypatch.setattr(repo, "_gh", lambda rt, br: {})
        repo._state.update(key=(), busy=True)
        repo._refresh(repo.Path("/x"), "main", ("/x", "main"))
        assert repo._state["key"] == ("/x", "main")
        assert repo._state["busy"] is False
