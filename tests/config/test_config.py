from pathlib import Path

import pytest

from shim_cli.config import (
    config_path,
    load_entities,
    load_policy,
    parse_settings,
    render_entities,
    render_settings,
)
from shim_cli.guard import DEFAULT_ENTITIES


def test_entity_settings_default_preset_and_round_trip_a_selection(
    tmp_path: Path,
) -> None:
    target = tmp_path / "shim-guard" / "config.toml"

    assert load_entities(target) == DEFAULT_ENTITIES

    target.parent.mkdir()
    target.write_bytes(render_entities(("SECRET", "EMAIL")))

    assert load_entities(target) == ("EMAIL", "SECRET")

    target.write_bytes(render_entities(()))
    assert load_entities(target) == ()


@pytest.mark.parametrize(
    "content",
    [
        b'enabled_entities = ["UNKNOWN"]\n',
        b'enabled_entities = ["EMAIL", "EMAIL"]\n',
        b'enabled_entities = ["EMAIL"]\nextra = true\n',
    ],
)
def test_invalid_entity_settings_fail_safely(tmp_path: Path, content: bytes) -> None:
    target = tmp_path / "config.toml"
    target.write_bytes(content)

    with pytest.raises(ValueError, match="settings"):
        load_entities(target)


def test_an_empty_settings_file_is_the_shipped_defaults(tmp_path: Path) -> None:
    target = tmp_path / "config.toml"
    target.write_bytes(b"")

    assert load_entities(target) == DEFAULT_ENTITIES


def test_unsafe_or_relative_settings_paths_are_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "source.toml"
    source.write_bytes(render_entities(("EMAIL",)))
    link = tmp_path / "config.toml"
    link.symlink_to(source)

    with pytest.raises(ValueError, match="safely"):
        load_entities(link)

    monkeypatch.setenv("SHIM_GUARD_CONFIG", "relative/config.toml")
    with pytest.raises(ValueError, match="path"):
        config_path()

    monkeypatch.setenv("SHIM_GUARD_CONFIG", "~shim_cli_missing_user/config.toml")
    with pytest.raises(ValueError, match="path"):
        config_path()


def test_the_new_configuration_variable_outranks_the_old_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    new = tmp_path / "new.toml"
    old = tmp_path / "old.toml"

    monkeypatch.setenv("SHIM_GUARD_CONFIG", str(old))
    assert config_path() == old

    monkeypatch.setenv("SHIM_CONFIG", str(new))
    assert config_path() == new

    monkeypatch.setenv("SHIM_CONFIG", "relative/config.toml")
    with pytest.raises(ValueError, match="path"):
        config_path()


def test_a_version_one_file_is_a_valid_version_two_file() -> None:
    document = parse_settings('enabled_entities = ["EMAIL", "SECRET"]\n')

    assert document == {
        "enabled_entities": ["EMAIL", "SECRET"],
        "mode": {},
        "entities": {},
        "ledger": False,
        "diet": ("json",),
        "custom": [],
    }


def test_policy_resolves_the_most_specific_override(tmp_path: Path) -> None:
    target = tmp_path / "config.toml"
    target.write_text(
        'enabled_entities = ["EMAIL", "SECRET", "DB_URI"]\n'
        "\n[mode]\n"
        'default = "observe"\n'
        'inbound = "enforce"\n'
        'PreToolUse = "warn"\n'
        'Bash = "observe"\n'
        "\n[entities]\n"
        'Bash = ["SECRET"]\n',
        encoding="utf-8",
    )
    policy = load_policy(target)

    assert policy.entities == ("EMAIL", "SECRET", "DB_URI")
    assert policy.mode_for("executable-text", "Bash", "PreToolUse") == "observe"
    assert policy.mode_for("outbound", "WebFetch", "PreToolUse") == "warn"
    assert policy.mode_for("inbound", "Read", "PostToolUse") == "enforce"
    assert policy.mode_for("local-write", "Write", "SomethingElse") == "observe"
    assert policy.entities_for("Bash") == ("SECRET",)
    assert policy.entities_for("Read") == ("EMAIL", "SECRET", "DB_URI")


def test_policy_falls_back_to_the_shipped_defaults(tmp_path: Path) -> None:
    policy = load_policy(tmp_path / "absent.toml")

    assert policy.entities == DEFAULT_ENTITIES
    assert policy.mode_for("user-prompt") == "warn"
    assert policy.mode_for("outbound") == "enforce"
    assert policy.mode_for("inbound") == "enforce"
    assert policy.mode_for("local-write") == "warn"
    assert policy.mode_for("executable-text") == "warn"


@pytest.mark.parametrize(
    "document",
    (
        'enabled_entities = ["EMAIL"]\n[mode]\ndefault = "paranoid"\n',
        'enabled_entities = ["EMAIL"]\n[mode]\ndefault = 1\n',
        'enabled_entities = ["EMAIL"]\n[entities]\nBash = "SECRET"\n',
        'enabled_entities = ["EMAIL"]\n[entities]\nBash = ["NOPE"]\n',
        'enabled_entities = ["EMAIL"]\nother = 1\n',
        'enabled_entities = "EMAIL"\n',
        '[mode]\ndefault = "warn"\nother = 1\n',
    ),
)
def test_invalid_policy_documents_fail_closed(document: str) -> None:
    with pytest.raises(ValueError):
        parse_settings(document)


@pytest.mark.parametrize(
    "document",
    (
        '[mode]\nlocal-write = "enforce"\n',
        "ledger = true\n",
        "diet = false\n",
        "",
    ),
)
def test_a_file_without_an_entity_list_still_parses(document: str) -> None:
    assert parse_settings(document)["enabled_entities"] == list(DEFAULT_ENTITIES)


def test_a_missing_config_file_means_the_shipped_defaults_not_empty_ones(
    tmp_path: Path,
) -> None:
    from shim_cli.events.diet import DEFAULT_TRANSFORMS

    policy = load_policy(tmp_path / "absent" / "config.toml")

    assert policy.entities == DEFAULT_ENTITIES
    assert policy.diet == DEFAULT_TRANSFORMS
    assert policy.ledger is False
    assert policy.mode_for("inbound") == "enforce"


@pytest.mark.parametrize("key", ("model-output", "Stop"))
@pytest.mark.parametrize("value", ("warn", "enforce"))
def test_a_mode_that_promises_to_act_on_model_output_is_refused(key, value) -> None:
    with pytest.raises(ValueError, match="shim settings are invalid"):
        parse_settings(f'[mode]\n"{key}" = "{value}"\n')


@pytest.mark.parametrize("key", ("model-output", "Stop"))
def test_observing_model_output_is_accepted(key: str) -> None:
    settings = parse_settings(f'[mode]\n"{key}" = "observe"\n')

    assert settings["mode"][key] == "observe"


CUSTOM = '[[custom]]\nname = "PROJECT_CODENAME"\npattern = "ATLAS-[0-9]{4}"\n'


def test_a_custom_pattern_survives_a_settings_round_trip() -> None:
    parsed = parse_settings(CUSTOM)

    rendered = render_settings(DEFAULT_ENTITIES, custom=parsed["custom"])

    assert parse_settings(rendered.decode())["custom"] == parsed["custom"]


@pytest.mark.parametrize(
    "body",
    (
        '[[custom]]\nname = "lowercase"\npattern = "x"\n',
        '[[custom]]\nname = "9LEADING"\npattern = "x"\n',
        '[[custom]]\nname = "' + "A" * 33 + '"\npattern = "x"\n',
        '[[custom]]\nname = "A"\n',
        '[[custom]]\nname = "A"\npattern = "x"\nliteral = "abc"\n',
        '[[custom]]\nname = "A"\npattern = "' + "x" * 257 + '"\n',
        '[[custom]]\nname = "A"\nliteral = "ab"\n',
        '[[custom]]\nname = "A"\npattern = "x"\nscore = 1.5\n',
        '[[custom]]\nname = "A"\npattern = "x"\nscore = "high"\n',
        '[[custom]]\nname = "A"\npattern = "x"\nignore_case = "yes"\n',
        '[[custom]]\nname = "A"\npattern = "x"\nwhole_word = false\n',
        '[[custom]]\nname = "A"\npattern = "(unclosed"\n',
        '[[custom]]\nname = "A"\npattern = "x"\nunknown = 1\n',
        '[[custom]]\nname = "A"\npattern = "x"\n[[custom]]\nname = "A"\npattern = "y"\n',
        "custom = 7\n",
    ),
)
def test_a_malformed_custom_entry_fails_closed(body: str) -> None:
    with pytest.raises(ValueError):
        parse_settings(body)


@pytest.mark.parametrize(
    "body",
    (
        '[[custom]]\nname = "A"\npattern = "x"\n',
        '[[custom]]\nname = "A_1"\nliteral = "abc"\n',
        '[[custom]]\nname = "A"\nliteral = "abc"\nwhole_word = false\n',
        '[[custom]]\nname = "A"\npattern = "x"\nscore = 0\n',
        '[[custom]]\nname = "A"\npattern = "x"\nscore = 1\n',
        '[[custom]]\nname = "A"\npattern = "x"\nignore_case = true\n',
    ),
)
def test_a_well_formed_custom_entry_is_accepted(body: str) -> None:
    assert parse_settings(body)["custom"]


def test_more_patterns_than_the_cap_fails_closed() -> None:
    from shim_cli.guard.entities import MAX_CUSTOM_PATTERNS

    body = "".join(
        f'[[custom]]\nname = "P{index}"\npattern = "x{index}"\n'
        for index in range(MAX_CUSTOM_PATTERNS + 1)
    )

    with pytest.raises(ValueError, match="shim settings are invalid"):
        parse_settings(body)


def test_exactly_the_cap_is_accepted() -> None:
    from shim_cli.guard.entities import MAX_CUSTOM_PATTERNS

    body = "".join(
        f'[[custom]]\nname = "P{index}"\npattern = "x{index}"\n'
        for index in range(MAX_CUSTOM_PATTERNS)
    )

    assert len(parse_settings(body)["custom"]) == MAX_CUSTOM_PATTERNS
