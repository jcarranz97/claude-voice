"""The four layers, the preset inversion, and the voice substitution.

Nothing here touches the developer's own configuration: ``CLAUDE_VOICE_HOME``
is a fresh directory per test, so ``CONFIG``, ``PRESET_FILE`` and
``USER_PRESETS`` all point inside it and a test writes real files rather than
patching the module's constants.
"""

from pathlib import Path

import pytest

import claude_voice.config as config

# Captured before anything has had a chance to compose a configuration.
_PRISTINE_GENERAL = dict(config.DEFAULTS["general"])


@pytest.fixture(autouse=True)
def defaults_intact():
    """Put ``DEFAULTS["general"]`` back the way it shipped, either side.

    ``_merge({}, DEFAULTS)`` copies only the top level, so the composed table
    holds the very same ``general`` dict as DEFAULTS whenever no layer defines
    one -- and ``_compose`` then writes the preset name straight into it. One
    ``resolve("es")`` therefore leaves every later lookup believing the config
    file names "es". That is a bug in the module, not in the tests; restoring
    on both sides keeps it from spreading between them.
    """
    config.DEFAULTS["general"].update(_PRISTINE_GENERAL)
    yield
    config.DEFAULTS["general"].update(_PRISTINE_GENERAL)


@pytest.fixture
def voices(home):
    """A directory of Piper voices, and a maker for more of them.

    ``voice("en_US-amy-medium")`` writes both halves -- the .onnx and the
    .onnx.json Piper refuses to load without -- and returns the model path.
    """
    d = home / "voices"
    d.mkdir()

    def _make(stem: str, with_json: bool = True):
        model = d / f"{stem}.onnx"
        model.write_bytes(b"onnx")
        if with_json:
            (d / f"{stem}.onnx.json").write_text("{}")
        return model

    _make.dir = d
    return _make


@pytest.fixture
def user_preset(home):
    """Write a language pack into the config directory's own presets/."""

    def _write(name: str, toml_source: str) -> Path:
        d = home / "presets"
        d.mkdir(exist_ok=True)
        p = d / f"{name}.toml"
        p.write_text(toml_source)
        return p

    return _write


class TestMerge:
    """`_merge` layers key by key, so a partial file overrides only itself."""

    def test_nested_tables_merge_rather_than_replace(self):
        base = {"tts": {"length_scale": 1.0, "max_chars": 400}}
        out = config._merge(base, {"tts": {"length_scale": 2.0}})
        assert out == {"tts": {"length_scale": 2.0, "max_chars": 400}}

    def test_a_scalar_replaces_a_table(self):
        assert config._merge({"a": {"b": 1}}, {"a": 2}) == {"a": 2}

    def test_a_table_replaces_a_scalar(self):
        assert config._merge({"a": 1}, {"a": {"b": 2}}) == {"a": {"b": 2}}

    def test_an_absent_overlay_is_the_base(self):
        assert config._merge({"a": 1}, None) == {"a": 1}

    def test_the_base_is_left_alone(self):
        # DEFAULTS is a module-level dict merged on every single load; a merge
        # that mutated it would leak one test's config into the next one.
        base = {"tts": {"length_scale": 1.0}}
        config._merge(base, {"tts": {"length_scale": 9.0}})
        assert base == {"tts": {"length_scale": 1.0}}


class TestRead:
    """`_read` never raises: a bad config file must not take the voice down."""

    def test_a_missing_file_is_an_empty_table(self, home):
        assert config._read(home / "nothing.toml") == {}

    def test_a_present_file_is_parsed(self, home):
        p = home / "some.toml"
        p.write_text('[tts]\nprimary_voice = "es"\n')
        assert config._read(p) == {"tts": {"primary_voice": "es"}}

    def test_malformed_toml_is_ignored_loudly(self, home, capsys):
        p = home / "broken.toml"
        p.write_text("[tts\nthis is not toml")
        assert config._read(p) == {}
        err = capsys.readouterr().err
        assert "ignoring" in err and "broken.toml" in err

    def test_without_tomllib_nothing_is_read(self, home, monkeypatch):
        # The 3.10 fallback: the package still runs, on DEFAULTS alone.
        p = home / "some.toml"
        p.write_text('[tts]\nprimary_voice = "es"\n')
        monkeypatch.setattr(config, "tomllib", None)
        assert config._read(p) == {}


class TestVoiceLike:
    """A voice for the language beats the exact filename that is absent."""

    def test_the_same_locale_wins_over_the_same_language(self, voices):
        voices("en_GB-alan-medium")
        best = voices("en_US-lessac-medium")
        assert config.voice_like(voices.dir / "en_US-amy-medium.onnx") == best

    def test_better_quality_wins_within_a_locale(self, voices):
        voices("en_US-alpha-low")
        best = voices("en_US-zulu-high")
        # Quality outranks the name, or the answer would be whatever sorts
        # first rather than whatever sounds best.
        assert config.voice_like(voices.dir / "en_US-amy-medium.onnx") == best

    def test_an_unknown_quality_word_sorts_last(self, voices):
        best = voices("en_US-alpha-low")
        voices("en_US-beta-experimental")
        assert config.voice_like(voices.dir / "en_US-amy-medium.onnx") == best

    def test_the_name_breaks_a_tie(self, voices):
        voices("en_US-zulu-medium")
        best = voices("en_US-alpha-medium")
        assert config.voice_like(voices.dir / "en_US-amy-medium.onnx") == best

    def test_another_language_is_not_a_candidate(self, voices):
        voices("es_MX-ald-medium")
        named = voices.dir / "en_US-amy-medium.onnx"
        assert config.voice_like(named) == named

    def test_a_voice_without_its_json_is_not_a_candidate(self, voices):
        voices("en_US-lessac-high", with_json=False)
        named = voices.dir / "en_US-amy-medium.onnx"
        assert config.voice_like(named) == named

    def test_a_file_with_no_locale_in_its_name_is_skipped(self, voices):
        voices("mystery")
        named = voices.dir / "en_US-amy-medium.onnx"
        assert config.voice_like(named) == named

    def test_an_empty_directory_leaves_the_named_voice(self, voices):
        named = voices.dir / "en_US-amy-medium.onnx"
        assert config.voice_like(named) == named

    def test_an_unreadable_directory_leaves_the_named_voice(self, voices, monkeypatch):
        def _boom(self, pattern):
            raise OSError("permission denied")

        monkeypatch.setattr(Path, "glob", _boom)
        named = voices.dir / "en_US-amy-medium.onnx"
        assert config.voice_like(named) == named


class TestConfigLookup:
    """`Config.get` walks a dotted path and stops at the first thing missing."""

    def test_a_dotted_key_is_read(self, cfg):
        assert cfg.get("tts.max_chars") == 400

    def test_a_missing_key_is_the_default(self, cfg):
        assert cfg.get("tts.nonesuch", "fallback") == "fallback"

    def test_a_missing_table_is_the_default(self, cfg):
        assert cfg.get("nonesuch.at.all") is None

    def test_walking_through_a_scalar_is_the_default(self, cfg):
        # general.name is a string, so general.name.deep has nowhere to go.
        assert cfg.get("general.name.deep", "stopped") == "stopped"

    def test_a_path_is_expanded(self, cfg):
        assert cfg.path("tts.voice_model").is_absolute()
        assert "~" not in str(cfg.path("tts.voice_model"))

    def test_a_path_falls_back_to_its_default(self, cfg):
        assert cfg.path("nothing.here", "~/elsewhere") == Path.home() / "elsewhere"

    def test_as_dict_is_the_composed_table(self, cfg):
        assert cfg.as_dict()["general"]["preset"] == "en"


class TestNamedValues:
    """The properties that exist so callers do not spell out dotted keys."""

    def test_it_answers_without_a_config_file(self, cfg):
        # Nothing written anywhere: DEFAULTS plus the bundled English pack.
        assert cfg.name == "Claude"
        assert cfg.preset == "en"
        assert cfg.primary_voice == "en-us"
        assert cfg.foreign_voice == ""
        assert cfg.length_scale == 1.0
        assert cfg.foreign_terms == set()
        assert cfg.overrides["nginx"]
        assert cfg.instruction.startswith("Voice output is ON")

    def test_the_config_file_is_read_back(self, write_config):
        cfg = write_config(
            '[general]\nname = "Borra"\n'
            "[tts]\nlength_scale = 1.06\n"
            '[pronunciation]\nforeign_terms = ["commit"]\n'
            '[pronunciation.overrides]\ncommit = "kQmIt"\n'
        )
        assert cfg.name == "Borra"
        assert cfg.length_scale == 1.06
        # A list is replaced whole; a table is merged, so the pack's own
        # entries are still there next to the new one.
        assert cfg.foreign_terms == {"commit"}
        assert cfg.overrides["commit"] == "kQmIt"
        assert "nginx" in cfg.overrides

    def test_the_language_falls_back_to_the_preset_name(self, write_config):
        cfg = write_config('[general]\nlanguage = ""\n')
        assert cfg.language == "en"

    def test_the_instruction_is_stripped(self, write_config):
        cfg = write_config('[instruction]\ntext = "  speak up  "\n')
        assert cfg.instruction == "speak up"

    def test_a_blank_instruction_is_empty(self, home):
        # No pack in the layering, so nothing supplies the injected text.
        assert config.resolve("").instruction == ""

    def test_the_named_voice_is_used_when_it_exists(self, write_config, voices):
        named = voices("en_US-amy-medium")
        cfg = write_config(f'[tts]\nvoice_model = "{named}"\n')
        assert cfg.voice_model == named
        assert cfg.voice_model_named == named

    def test_an_absent_voice_stands_aside_for_one_on_disk(self, write_config, voices):
        stand_in = voices("en_US-lessac-high")
        named = voices.dir / "en_US-amy-medium.onnx"
        cfg = write_config(f'[tts]\nvoice_model = "{named}"\n')
        assert cfg.voice_model == stand_in
        # What was asked for is still reported, because that is what --fetch
        # downloads and what a "missing" message has to name.
        assert cfg.voice_model_named == named


class TestPresetsOnDisk:
    """Bundled packs and hand-written ones, with yours shadowing ours."""

    def test_the_bundled_packs_are_listed(self, home):
        assert "en" in config.presets()
        assert "es" in config.presets()

    def test_a_user_pack_joins_the_cycle(self, user_preset):
        user_preset("zz", '[general]\nlanguage = "Zedish"\n')
        assert "zz" in config.presets()

    def test_a_shadowing_pack_is_not_a_second_entry(self, user_preset):
        user_preset("en", '[general]\nlanguage = "Mine"\n')
        assert config.presets().count("en") == 1

    def test_the_order_is_stable(self, user_preset):
        user_preset("aa", "")
        assert config.presets() == sorted(config.presets())

    def test_the_user_pack_wins_the_path(self, user_preset):
        mine = user_preset("en", "")
        assert config.preset_path("en") == mine

    def test_an_unknown_pack_points_at_the_bundled_directory(self, home):
        assert config.preset_path("zz") == config.BUNDLED_PRESETS / "zz.toml"

    def test_an_unreadable_preset_directory_is_skipped(self, monkeypatch):
        def _boom(self, pattern):
            raise OSError("permission denied")

        monkeypatch.setattr(Path, "glob", _boom)
        assert config.presets() == []


class TestConfiguredPreset:
    """What the config file names, independent of the language switch."""

    def test_without_a_config_file_it_is_the_default(self, home):
        assert config.configured_preset() == "en"

    def test_the_config_file_names_it(self, home):
        (home / "config.toml").write_text('[general]\npreset = "es"\n')
        assert config.configured_preset() == "es"

    def test_a_config_file_without_a_general_table_is_the_default(self, home):
        (home / "config.toml").write_text("[tts]\nlength_scale = 2.0\n")
        assert config.configured_preset() == "en"

    def test_a_blank_preset_is_the_default(self, home):
        assert config.configured_preset() == "en"
        (home / "config.toml").write_text('[general]\npreset = ""\n')
        assert config.configured_preset() == "en"


class TestActivePreset:
    """The marker file is the switch, and it sits above the config file."""

    def test_with_nothing_written_it_is_the_built_in_default(self, home):
        assert config.active_preset() == ("en", "default")

    def test_a_config_file_is_credited_as_the_source(self, home):
        (home / "config.toml").write_text('[general]\npreset = "es"\n')
        assert config.active_preset() == ("es", "config")

    def test_the_marker_overrides_the_config_file(self, home):
        (home / "config.toml").write_text('[general]\npreset = "en"\n')
        (home / "preset").write_text("es\n")
        assert config.active_preset() == ("es", "switch")

    def test_a_marker_naming_nothing_on_disk_is_ignored(self, home):
        # A stale switch must not take the voice down; the config's own preset
        # is still a working answer.
        (home / "preset").write_text("zz\n")
        assert config.active_preset() == ("en", "default")

    def test_an_unreadable_marker_is_ignored(self, home):
        (home / "preset").write_bytes(b"\xff\xfe not utf-8")
        assert config.active_preset() == ("en", "default")

    def test_an_empty_marker_is_ignored(self, home):
        (home / "preset").write_text("\n")
        assert config.active_preset() == ("en", "default")


class TestLayering:
    """The four layers, in the order the module docstring promises."""

    def test_defaults_alone_are_a_working_configuration(self, cfg):
        assert cfg.get("narrate.word_limit") == 50

    def test_the_pack_overrides_the_defaults(self, user_preset):
        user_preset("zz", '[tts]\nprimary_voice = "zz-zz"\n')
        assert config.resolve("zz").primary_voice == "zz-zz"

    def test_the_config_overrides_its_own_pack(self, home, user_preset):
        user_preset("zz", '[tts]\nprimary_voice = "from-pack"\n')
        (home / "config.toml").write_text(
            '[general]\npreset = "zz"\n[tts]\nprimary_voice = "mine"\n'
        )
        assert config.resolve("zz").primary_voice == "mine"

    def test_the_pack_overrides_a_config_written_for_another_language(self, home, user_preset):
        # The inversion: a config file written for zz carries zz in it, so
        # keeping it on top would speak zz inside the yy preset.
        user_preset("yy", '[tts]\nprimary_voice = "from-yy-pack"\n')
        (home / "config.toml").write_text(
            '[general]\npreset = "zz"\n[tts]\nprimary_voice = "mine"\n'
        )
        assert config.resolve("yy").primary_voice == "from-yy-pack"

    def test_the_inversion_only_touches_keys_the_pack_defines(self, home, user_preset):
        # A microphone device or a panel position is not language-flavoured,
        # so it has to survive a switch untouched.
        user_preset("yy", '[tts]\nprimary_voice = "from-yy-pack"\n')
        (home / "config.toml").write_text('[general]\npreset = "zz"\n[stt]\ndevice = "hw:2,0"\n')
        assert config.resolve("yy").get("stt.device") == "hw:2,0"

    def test_a_per_preset_table_holds_either_way(self, home, user_preset):
        user_preset("yy", '[tts]\nprimary_voice = "from-yy-pack"\n')
        (home / "config.toml").write_text(
            '[general]\npreset = "zz"\n[preset.yy.tts]\nprimary_voice = "kept"\n'
        )
        assert config.resolve("yy").primary_voice == "kept"

    def test_a_per_preset_table_beats_the_config_for_its_own_preset(self, home, user_preset):
        user_preset("zz", "")
        (home / "config.toml").write_text(
            '[general]\npreset = "zz"\n'
            '[tts]\nprimary_voice = "general"\n'
            '[preset.zz.tts]\nprimary_voice = "specific"\n'
        )
        assert config.resolve("zz").primary_voice == "specific"

    def test_another_presets_table_is_not_applied(self, home, user_preset):
        user_preset("zz", "")
        (home / "config.toml").write_text(
            '[general]\npreset = "zz"\n'
            '[tts]\nprimary_voice = "general"\n'
            '[preset.yy.tts]\nprimary_voice = "not this one"\n'
        )
        assert config.resolve("zz").primary_voice == "general"

    def test_the_preset_table_never_leaks_into_the_config_proper(self, home, user_preset):
        user_preset("zz", "")
        (home / "config.toml").write_text('[preset.zz.tts]\nprimary_voice = "x"\n')
        assert "preset" not in config.resolve("zz").as_dict()

    def test_the_composed_preset_is_the_one_asked_for(self, home, user_preset):
        user_preset("yy", '[general]\npreset = "wrong"\n')
        assert config.resolve("yy").preset == "yy"

    def test_no_preset_at_all_still_composes(self, home):
        # resolve("") is the degenerate case: defaults plus the config file,
        # with no language pack in the middle.
        (home / "config.toml").write_text('[tts]\nprimary_voice = "mine"\n')
        cfg = config.resolve("")
        assert cfg.primary_voice == "mine"
        assert cfg.preset == ""

    @pytest.mark.xfail(
        reason="_compose writes the preset name into the general table it shares with DEFAULTS",
        strict=True,
    )
    def test_resolving_does_not_switch(self, home, user_preset):
        # Asking what zz WOULD look like currently leaves the process believing
        # the config file names zz, because the composed general table is the
        # one inside DEFAULTS. Remove the xfail when _compose stops writing
        # through it -- and the defaults_intact fixture above with it.
        user_preset("zz", "")
        config.resolve("zz")
        assert config.active_preset() == ("en", "default")


class TestLoad:
    """`load` caches, because a hook is a process that runs once."""

    def test_the_second_call_is_the_same_object(self, home):
        assert config.load() is config.load()

    def test_reload_rebuilds(self, home):
        first = config.load()
        assert config.load(reload=True) is not first

    def test_reload_picks_up_the_switch(self, home):
        config.load(reload=True)
        (home / "preset").write_text("es\n")
        assert config.load().preset == "en"
        assert config.load(reload=True).preset == "es"


class TestShow:
    """`claude-voice config` prints what is in effect and where it came from."""

    def test_it_reports_the_defaults(self, home, capsys):
        config.load(reload=True)
        config.show()
        out = capsys.readouterr().out
        assert "(absent, using defaults)" in out
        assert "built-in default" in out
        assert "single language" in out
        assert "50 words" in out

    def test_it_reports_the_config_file(self, write_config, capsys):
        write_config('[general]\npreset = "es"\n')
        config.show()
        assert "from the config file" in capsys.readouterr().out

    def test_it_reports_the_switch(self, home, capsys):
        (home / "preset").write_text("es\n")
        config.load(reload=True)
        config.show()
        out = capsys.readouterr().out
        assert "switched, preset file" in out
        # The other packs on disk are listed, since they are what `l` cycles.
        assert "also on disk: en" in out

    def test_a_missing_voice_is_flagged(self, write_config, capsys):
        write_config('[tts]\nvoice_model = "/nowhere/at/all/en_US-amy-medium.onnx"\n')
        config.show()
        assert "MISSING" in capsys.readouterr().out

    def test_a_substituted_voice_names_what_it_stands_in_for(self, write_config, voices, capsys):
        voices("en_US-lessac-high")
        named = voices.dir / "en_US-amy-medium.onnx"
        write_config(f'[tts]\nvoice_model = "{named}"\n')
        config.show()
        out = capsys.readouterr().out
        assert "standing in for en_US-amy-medium.onnx" in out
        assert "MISSING" not in out

    def test_a_second_phonemizer_is_reported_with_its_term_count(self, write_config, capsys):
        write_config(
            '[tts]\nforeign_voice = "en-us"\n[pronunciation]\nforeign_terms = ["commit", "merge"]\n'
        )
        config.show()
        assert "+ en-us for 2 terms" in capsys.readouterr().out

    def test_a_sole_pack_is_listed_without_the_others_clause(self, home, monkeypatch, capsys):
        monkeypatch.setattr(config, "BUNDLED_PRESETS", home / "empty")
        monkeypatch.setattr(config, "USER_PRESETS", home / "empty")
        config.load(reload=True)
        config.show()
        assert "also on disk" not in capsys.readouterr().out
