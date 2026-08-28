"""The language switch: what is on disk, what can speak, and cycling between.

``lang`` reaches ``config`` by bare name, so ``lang._config`` is a second
import of the same file and its module globals have to be patched through that
object rather than through ``claude_voice.config``. The two agree on ``BASE``,
which is the tmp home, so every file a test writes is seen by both.
"""

import subprocess
import sys
import urllib.request
from pathlib import Path

import pytest

import claude_voice.lang as lang

# Captured before anything has had a chance to compose a configuration.
_PRISTINE_GENERAL = dict(lang._config.DEFAULTS["general"])


@pytest.fixture(autouse=True)
def defaults_intact():
    """Undo ``_compose`` writing the composed preset name into DEFAULTS.

    Listing the languages resolves every one of them, and a resolve leaves the
    preset name behind in the shared ``general`` table. Restoring on both
    sides keeps one test's listing out of the next test's answer.
    """
    lang._config.DEFAULTS["general"].update(_PRISTINE_GENERAL)
    yield
    lang._config.DEFAULTS["general"].update(_PRISTINE_GENERAL)


class World:
    """Language packs and downloaded voices, entirely inside the tmp home."""

    def __init__(self, home: Path):
        self.home = home
        self.bundled = home / "bundled"
        self.mine = home / "mine"
        self.voices = home / "voices"
        for d in (self.bundled, self.mine, self.voices):
            d.mkdir()

    def pack(self, name: str, language: str, model: str = "", user: bool = False) -> Path:
        """A minimal language pack. ``model`` is a Piper stem, or a filename."""
        model = model or f"{name}_XX-someone-medium"
        d = self.mine if user else self.bundled
        p = d / f"{name}.toml"
        p.write_text(
            f'[general]\npreset = "{name}"\nlanguage = "{language}"\n'
            f'[tts]\nvoice_model = "{self.voices / model}.onnx"\n'
            f'[ack]\nphrases = ["One moment."]\n'
        )
        return p

    def voice(self, stem: str) -> Path:
        model = self.voices / f"{stem}.onnx"
        model.write_bytes(b"onnx")
        (self.voices / f"{stem}.onnx.json").write_text("{}")
        return model

    def acks(self, name: str) -> Path:
        d = self.home / "acks" / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "ack00.wav").write_bytes(b"RIFF")
        return d


@pytest.fixture
def world(home, monkeypatch):
    """Two packs, English and Spanish, and no voice for either one yet.

    The bundled packs that ship in the wheel are patched out: a test that
    asserts on the cycle has to know exactly what is in it, and what a
    developer happens to have downloaded is not part of the fixture.
    """
    w = World(home)
    monkeypatch.setattr(lang._config, "BUNDLED_PRESETS", w.bundled)
    monkeypatch.setattr(lang._config, "USER_PRESETS", w.mine)
    w.pack("en", "English", "en_US-amy-medium")
    w.pack("es", "Espanol", "es_MX-ald-medium")
    lang._config.load(reload=True)
    return w


@pytest.fixture
def fake_mod(monkeypatch):
    """Stand in for ``lang._mod``, which loads sibling modules off disk.

    Every caller of it either drains the audio queue or synthesizes a cache;
    both are side effects a unit test must not have. The detached ack builder
    goes with it: a real one outlives the test and writes into the home
    directory the next test is busy deleting.
    """
    calls = []
    monkeypatch.setattr(lang.subprocess, "Popen", lambda cmd, **kw: calls.append(("spawn", cmd)))

    class _Voice:
        @staticmethod
        def ack_dir(name):
            calls.append(("ack_dir", name))
            return Path("/nonexistent/acks") / name

        @staticmethod
        def build_acks(name):
            calls.append(("build_acks", name))

    class _AudioQ:
        @staticmethod
        def drain():
            calls.append(("drain", None))

    def _mod(name):
        calls.append(("import", name))
        if name == "voice":
            return _Voice
        if name == "audioq":
            return _AudioQ
        raise AssertionError(f"unexpected module {name}")

    monkeypatch.setattr(lang, "_mod", _mod)
    _mod.calls = calls
    return _mod


class TestOptions:
    """The cycle is every pack on disk, in a stable order."""

    def test_it_lists_the_packs(self, world):
        assert lang.options() == ["en", "es"]

    def test_a_shadowing_pack_is_one_entry(self, world):
        world.pack("es", "Castellano", user=True)
        assert lang.options() == ["en", "es"]

    def test_current_is_the_active_preset(self, world):
        assert lang.current() == "en"


class TestLabel:
    """A language is named in its own language, since it labels its own key."""

    def test_it_reads_the_pack(self, world):
        assert lang.label("es") == "Espanol"

    def test_a_shadowing_pack_supplies_the_label(self, world):
        world.pack("es", "Castellano", user=True)
        assert lang.label("es") == "Castellano"

    def test_a_pack_that_cannot_be_read_falls_back_to_its_name(self, world, monkeypatch):
        def _boom(name):
            raise RuntimeError("unreadable")

        monkeypatch.setattr(lang._config, "resolve", _boom)
        assert lang.label("es") == "es"


class TestReady:
    """Having a voice is the whole question; the rest of a pack is text."""

    def test_an_unknown_preset_is_not_ready(self, world):
        assert lang.ready("zz") == (False, "no preset called zz")

    def test_a_preset_with_no_voice_says_how_to_get_one(self, world):
        ok, why = lang.ready("es")
        assert ok is False
        assert "Espanol" in why and "--fetch es" in why

    def test_the_named_voice_makes_it_ready(self, world):
        world.voice("es_MX-ald-medium")
        assert lang.ready("es") == (True, "")

    def test_any_voice_for_the_language_makes_it_ready(self, world):
        # The preset names one .onnx, but what is on your disk is what you get
        # to choose between -- config.voice_like() finds the stand-in.
        world.voice("es_ES-sharvard-medium")
        assert lang.ready("es") == (True, "")


class TestFollowing:
    """`--next` walks the cycle and steps over anything that cannot speak."""

    def test_with_two_it_is_the_other_one(self, world):
        world.voice("en_US-amy-medium")
        world.voice("es_MX-ald-medium")
        assert lang.following("en") == "es"
        assert lang.following("es") == "en"

    def test_it_defaults_to_the_current_language(self, world):
        world.voice("en_US-amy-medium")
        world.voice("es_MX-ald-medium")
        assert lang.following() == "es"

    def test_a_mute_language_is_skipped(self, world):
        world.voice("en_US-amy-medium")
        assert lang.following("en") == "en"

    def test_ignoring_speakability_keeps_the_mute_one_in(self, world):
        assert lang.following("en", speakable=False) == "es"

    def test_an_unknown_starting_point_lands_on_the_first(self, world):
        world.voice("es_MX-ald-medium")
        assert lang.following("zz") == "es"

    def test_with_nothing_speakable_there_is_no_next(self, world):
        assert lang.following("zz") == ""


class TestAcksCached:
    """Whether the preset's phrases have been synthesized yet."""

    def test_an_empty_cache_is_not_cached(self, world):
        assert lang.acks_cached("es") is False

    def test_a_wav_in_the_presets_directory_counts(self, world):
        # Goes through the real _mod(), which loads voice.py off disk to ask
        # it where the cache for a preset lives.
        world.acks("es")
        assert lang.acks_cached("es") is True


class TestWarmAcks:
    """Synthesis is seconds, and this is called from a keystroke."""

    def test_an_existing_cache_spawns_nothing(self, world, no_subprocess):
        world.acks("es")
        lang.warm_acks("es")

    def test_it_detaches_a_builder(self, world, monkeypatch):
        spawned = []
        monkeypatch.setattr(lang.subprocess, "Popen", lambda cmd, **kw: spawned.append((cmd, kw)))
        lang.warm_acks("es")
        cmd, kw = spawned[0]
        assert cmd[1:] == [str(lang.HERE / "voice.py"), "--build-acks", "es"]
        assert kw["start_new_session"] is True

    def test_a_spawn_failure_is_swallowed(self, world, monkeypatch):
        def _boom(*a, **kw):
            raise OSError("no fork for you")

        monkeypatch.setattr(lang.subprocess, "Popen", _boom)
        lang.warm_acks("es")


class TestSwitch:
    """Writing the marker file is the whole switch."""

    def test_it_refuses_a_language_with_no_voice(self, world, fake_mod):
        ok, why = lang.switch("es")
        assert ok is False
        assert "--fetch es" in why
        assert not (world.home / "preset").exists()

    def test_it_writes_the_marker_and_reports_the_language(self, world, fake_mod):
        world.voice("es_MX-ald-medium")
        assert lang.switch("es") == (True, "Espanol")
        assert (world.home / "preset").read_text().strip() == "es"
        assert lang.current() == "es"

    def test_it_silences_what_is_queued(self, world, fake_mod):
        # An English tail arriving after the HUD says Espanol reads as the
        # switch having failed.
        world.voice("es_MX-ald-medium")
        lang.switch("es")
        assert ("drain", None) in fake_mod.calls

    def test_a_queue_that_will_not_drain_does_not_stop_the_switch(
        self, world, fake_mod, monkeypatch
    ):
        def _boom(name):
            if name == "audioq":
                raise RuntimeError("no queue")
            return fake_mod(name)

        monkeypatch.setattr(lang, "_mod", _boom)
        world.voice("es_MX-ald-medium")
        ok, msg = lang.switch("es")
        assert ok is True
        assert msg == "Espanol"

    def test_a_marker_that_cannot_be_written_refuses(self, world, fake_mod, monkeypatch):
        def _boom(src, dst):
            raise OSError("read-only")

        monkeypatch.setattr(lang.os, "replace", _boom)
        world.voice("es_MX-ald-medium")
        ok, why = lang.switch("es")
        assert ok is False
        assert why.startswith("could not write the switch:")


class TestSwitchNext:
    """What the HUD's `l` key does."""

    def test_it_moves_to_the_other_language(self, world, fake_mod):
        world.voice("en_US-amy-medium")
        world.voice("es_MX-ald-medium")
        assert lang.switch_next() == (True, "Espanol")

    def test_it_names_the_language_that_needs_downloading(self, world, fake_mod):
        world.voice("en_US-amy-medium")
        ok, why = lang.switch_next()
        assert ok is False
        assert "no voice for Espanol" in why
        assert "--fetch es" in why

    def test_a_single_pack_has_nowhere_to_go(self, world, fake_mod, monkeypatch):
        monkeypatch.setattr(lang, "options", lambda: ["en"])
        world.voice("en_US-amy-medium")
        assert lang.switch_next() == (False, "only one language pack on disk")


class TestVoiceUrl:
    """The download path is the filename taken apart, not a table of voices."""

    def test_it_derives_the_repository_path(self, world):
        url = lang._voice_url(Path("/somewhere/es_MX-ald-medium.onnx"))
        assert url.endswith("/es/es_MX/ald/medium/es_MX-ald-medium")

    def test_a_name_it_cannot_take_apart_has_no_url(self, world):
        assert lang._voice_url(Path("/somewhere/mystery.onnx")) == ""


class TestFetch:
    """`--fetch` downloads the voice a preset names, then caches its phrases."""

    def test_an_unknown_preset_is_refused(self, world, fake_mod, capsys):
        assert lang.fetch("zz") is False
        assert "no preset called zz" in capsys.readouterr().out

    def test_a_voice_already_on_disk_is_not_downloaded(self, world, fake_mod, capsys):
        world.voice("es_MX-ald-medium")
        assert lang.fetch("es") is True
        assert "already present" in capsys.readouterr().out
        assert ("build_acks", "es") in fake_mod.calls

    def test_a_stand_in_does_not_count_as_present(self, world, fake_mod, monkeypatch, capsys):
        # Fetching is how you get that specific voice, even when another one
        # for the language is already speaking for it.
        world.voice("es_ES-sharvard-medium")
        monkeypatch.setattr(urllib.request, "urlretrieve", lambda url, dest: Path(dest).touch())
        assert lang.fetch("es") is True
        assert "fetching es_MX-ald-medium.onnx" in capsys.readouterr().out

    def test_it_downloads_both_halves_and_moves_them_into_place(
        self, world, fake_mod, monkeypatch, capsys
    ):
        asked = []

        def _get(url, dest):
            asked.append(url)
            Path(dest).write_bytes(b"payload")

        monkeypatch.setattr(urllib.request, "urlretrieve", _get)
        assert lang.fetch("es") is True
        assert [u.rsplit("/", 1)[-1] for u in asked] == [
            "es_MX-ald-medium.onnx",
            "es_MX-ald-medium.onnx.json",
        ]
        # The .part files are moved, not left behind: an interrupted fetch
        # must not leave half a voice that exists.
        assert (world.voices / "es_MX-ald-medium.onnx").exists()
        assert (world.voices / "es_MX-ald-medium.onnx.json").exists()
        assert not list(world.voices.glob("*.part"))

    def test_a_name_with_no_derivable_url_says_so(self, world, fake_mod, capsys):
        world.pack("zz", "Zedish", "mystery")
        assert lang.fetch("zz") is False
        assert "cannot derive a download URL" in capsys.readouterr().out

    def test_a_failed_download_is_reported_and_stops(self, world, fake_mod, monkeypatch, capsys):
        def _boom(url, dest):
            raise OSError("connection refused")

        monkeypatch.setattr(urllib.request, "urlretrieve", _boom)
        assert lang.fetch("es") is False
        assert "failed: connection refused" in capsys.readouterr().out
        assert ("build_acks", "es") not in fake_mod.calls


class TestShow:
    """The listing: every pack, its own name for itself, and why not."""

    def test_it_marks_the_active_one_and_its_source(self, world, fake_mod, capsys):
        world.voice("en_US-amy-medium")
        lang.show()
        out = capsys.readouterr().out
        assert "→ en  English  (built-in default)" in out
        assert "en_US-amy-medium.onnx" in out
        assert "acknowledgements not cached yet" in out

    def test_it_gives_the_reason_a_language_is_mute(self, world, fake_mod, capsys):
        lang.show()
        assert "— no Espanol voice downloaded" in capsys.readouterr().out

    def test_a_cached_preset_is_not_flagged(self, world, capsys):
        world.voice("en_US-amy-medium")
        world.acks("en")
        lang.show()
        assert "acknowledgements not cached yet" not in capsys.readouterr().out

    def test_it_credits_the_config_file(self, world, fake_mod, capsys):
        # The source note is only reached for a preset that can actually
        # speak; a mute one gets the reason instead.
        world.voice("en_US-amy-medium")
        (world.home / "config.toml").write_text('[general]\npreset = "en"\n')
        lang.show()
        assert "(from the config file)" in capsys.readouterr().out

    def test_it_credits_the_switch(self, world, fake_mod, capsys):
        world.voice("es_MX-ald-medium")
        lang.switch("es")
        lang.show()
        assert "→ es  Espanol  (switched)" in capsys.readouterr().out


class TestMain:
    """The command line, one branch per argument."""

    def _argv(self, monkeypatch, *args):
        monkeypatch.setattr(sys, "argv", ["lang.py", *args])

    def test_no_argument_lists(self, world, fake_mod, monkeypatch, capsys):
        self._argv(monkeypatch)
        assert lang.main() == 0
        assert "claude-voice lang <name>" in capsys.readouterr().out

    def test_next_switches(self, world, fake_mod, monkeypatch, capsys):
        world.voice("en_US-amy-medium")
        world.voice("es_MX-ald-medium")
        self._argv(monkeypatch, "--next")
        assert lang.main() == 0
        assert "Espanol" in capsys.readouterr().out

    def test_next_that_cannot_move_exits_nonzero(self, world, fake_mod, monkeypatch, capsys):
        world.voice("en_US-amy-medium")
        self._argv(monkeypatch, "next")
        assert lang.main() == 1
        assert "refused:" in capsys.readouterr().out

    def test_fetch_takes_a_name(self, world, fake_mod, monkeypatch, capsys):
        world.voice("es_MX-ald-medium")
        self._argv(monkeypatch, "--fetch", "es")
        assert lang.main() == 0
        assert "already present" in capsys.readouterr().out

    def test_fetch_without_a_name_is_the_current_language(
        self, world, fake_mod, monkeypatch, capsys
    ):
        world.voice("en_US-amy-medium")
        self._argv(monkeypatch, "fetch")
        assert lang.main() == 0
        assert "en_US-amy-medium.onnx already present" in capsys.readouterr().out

    def test_a_failed_fetch_exits_nonzero(self, world, fake_mod, monkeypatch):
        self._argv(monkeypatch, "--fetch", "zz")
        assert lang.main() == 1

    def test_help_prints_the_docstring(self, world, monkeypatch, capsys):
        self._argv(monkeypatch, "--help")
        assert lang.main() == 0
        assert "The language switch." in capsys.readouterr().out

    def test_a_bare_name_switches(self, world, fake_mod, monkeypatch, capsys):
        world.voice("es_MX-ald-medium")
        self._argv(monkeypatch, "es")
        assert lang.main() == 0
        assert "speaking Espanol" in capsys.readouterr().out

    def test_a_name_that_cannot_speak_is_refused(self, world, fake_mod, monkeypatch, capsys):
        self._argv(monkeypatch, "es")
        assert lang.main() == 1
        assert "refused:" in capsys.readouterr().out


def test_subprocess_is_never_reached_by_a_plain_listing(world, fake_mod, no_subprocess):
    """The listing has to be free: the HUD redraws it on every switch."""
    assert subprocess.run is not None  # the guard is installed
    lang.show()
