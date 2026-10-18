"""`shim config` never silently drops a setting or an option it was given."""

from __future__ import annotations

import json
import shlex
from pathlib import Path

import pytest
from typer.testing import CliRunner

from shim_cli.cli.app import app
from shim_cli.config import load_policy
from shim_cli.guard import evaluate

runner = CliRunner()

LITERAL = "ACME-PROJECT-ZEBRA"


def _settings(entity: str = "DB_URI") -> str:
    """Every section `shim config` has to carry across a save."""
    return (
        "ledger = true\n"
        'markers = "note"\n'
        'enabled_entities = ["EMAIL", "SECRET", "CUSTOM"]\n'
        "\n"
        "[mode]\n"
        'user-prompt = "enforce"\n'
        "\n"
        "[reveal]\n"
        "IBAN = 4\n"
        "\n"
        "[[custom]]\n"
        'name = "PROJECT_CODENAME"\n'
        "pattern = '\\bATLAS-[0-9]{4}\\b'\n"
        "\n"
        "[entities]\n"
        f'Bash = ["SECRET", "{entity}"]\n'
    )


# One misspelled entity name: the file is invalid, and fixable by hand.
INVALID = _settings(entity="CREDITCARD")


def _target(monkeypatch, tmp_path: Path) -> Path:
    target = tmp_path / "settings" / "config.toml"
    monkeypatch.setenv("SHIM_CONFIG", str(target))
    return target


def _file(monkeypatch, tmp_path: Path, text: str, mode: int = 0o600) -> Path:
    target = _target(monkeypatch, tmp_path)
    target.parent.mkdir(mode=0o700)
    target.write_text(text, encoding="utf-8")
    target.chmod(mode)
    return target


def _config(*argv: str) -> tuple[int, dict]:
    result = runner.invoke(app, ["config", *argv, "--json"])
    return result.exit_code, json.loads(result.stdout)


@pytest.mark.parametrize(
    "flags",
    (
        ["--only", "EMAIL"],
        ["--only", "EMAIL", "--only", "SECRET"],
        ["--only", "EMAIL", "--custom-literal", f"CODENAME={LITERAL}"],
    ),
    ids=("one", "two", "with-a-pattern"),
)
def test_only_refuses_an_invalid_file_and_leaves_it_untouched(
    flags: list[str], monkeypatch, tmp_path: Path
) -> None:
    target = _file(monkeypatch, tmp_path, INVALID)

    exit_code, payload = _config(*flags, "--yes")

    assert exit_code == 2
    assert payload["code"] == "SETTINGS_INVALID"
    assert payload["fix"] == "Edit the settings file the error names until it parses."
    assert target.read_text(encoding="utf-8") == INVALID


def test_only_on_a_valid_file_replaces_just_the_entity_list(
    monkeypatch, tmp_path: Path
) -> None:
    _file(monkeypatch, tmp_path, _settings())

    exit_code, _ = _config("--only", "EMAIL", "--yes")

    saved = load_policy()
    assert exit_code == 0
    assert saved.entities == ("EMAIL",)
    assert saved.modes == {"user-prompt": "enforce"}
    assert {tool: list(kinds) for tool, kinds in saved.tool_entities.items()} == {
        "Bash": ["SECRET", "DB_URI"]
    }
    assert saved.ledger is True
    assert saved.markers == "note"
    assert saved.reveal == {"IBAN": 4}
    assert [pattern.entry for pattern in saved.custom] == [
        {"name": "PROJECT_CODENAME", "pattern": r"\bATLAS-[0-9]{4}\b"}
    ]


@pytest.mark.parametrize(
    "flag, value",
    (
        ("--custom-literal", f"PROJECT={LITERAL}"),
        ("--custom", rf"PROJECT=\b{LITERAL}\b"),
    ),
)
@pytest.mark.parametrize("existing", (None, _settings()), ids=("new", "existing"))
def test_only_with_a_pattern_also_enables_custom(
    flag: str, value: str, existing: str | None, monkeypatch, tmp_path: Path
) -> None:
    if existing is None:
        _target(monkeypatch, tmp_path)
    else:
        _file(monkeypatch, tmp_path, existing)

    exit_code, _ = _config("--only", "EMAIL", flag, value, "--yes")

    saved = load_policy()
    decision = evaluate(
        f"ship {LITERAL} to alice@example.com",
        saved.entities,
        saved.custom,
        saved.reveal,
    )
    assert exit_code == 0
    assert set(saved.entities) == {"EMAIL", "CUSTOM"}
    assert {finding.entity_type for finding in decision.findings} == {
        "EMAIL",
        "CUSTOM",
    }
    assert LITERAL not in decision.redacted_text


RESET_COMPANIONS = (
    ["--only", "EMAIL"],
    ["--enable", "PHONE"],
    ["--disable", "PHONE"],
    ["--custom", rf"PROJECT=\b{LITERAL}\b"],
    ["--custom-literal", f"PROJECT={LITERAL}"],
    ["--remove-custom", "PROJECT_CODENAME"],
    ["--reveal", "IBAN=4"],
    ["--no-reveal", "IBAN"],
    ["--ledger"],
    ["--no-ledger"],
    ["--diet"],
    ["--no-diet"],
)


@pytest.mark.parametrize(
    "flags", RESET_COMPANIONS, ids=[flags[0] for flags in RESET_COMPANIONS]
)
def test_reset_refuses_every_other_option_and_writes_nothing(
    flags: list[str], monkeypatch, tmp_path: Path
) -> None:
    target = _file(monkeypatch, tmp_path, _settings())

    exit_code, payload = _config("--reset", *flags, "--yes")

    assert exit_code == 2
    assert payload["code"] == "OPTIONS_CONFLICT"
    assert payload["error"] == "--reset cannot be combined with other options."
    assert target.read_text(encoding="utf-8") == _settings()


def test_reset_with_another_option_creates_no_file(monkeypatch, tmp_path: Path) -> None:
    target = _target(monkeypatch, tmp_path)

    exit_code, payload = _config(
        "--reset", "--custom-literal", f"PROJECT={LITERAL}", "--yes"
    )

    assert (exit_code, payload["code"]) == (2, "OPTIONS_CONFLICT")
    assert not target.parent.exists()


def test_reset_alone_still_starts_over(monkeypatch, tmp_path: Path) -> None:
    _file(monkeypatch, tmp_path, INVALID)

    exit_code, payload = _config("--reset", "--yes")

    saved = load_policy()
    assert exit_code == 0
    assert payload["custom"] == [] and payload["reveal"] == {}
    assert (saved.modes, saved.ledger, saved.markers) == ({}, False, "report")


@pytest.mark.parametrize(
    "flags",
    (["--reset"], ["--only", "EMAIL"], ["--enable", "PHONE"]),
    ids=("reset", "only", "enable"),
)
def test_a_refused_file_reports_the_refusal_and_its_fix(
    flags: list[str], monkeypatch, tmp_path: Path
) -> None:
    target = _file(monkeypatch, tmp_path, _settings(), mode=0o664)

    exit_code, payload = _config(*flags, "--yes")

    folder, path = shlex.quote(str(target.parent)), shlex.quote(str(target))
    assert exit_code == 2
    assert payload["code"] == "SETTINGS_REFUSED"
    assert payload["fix"] == f"chmod 700 {folder} && chmod 600 {path}"
    assert "writable by another user" in payload["error"]
    assert target.read_text(encoding="utf-8") == _settings()


def test_a_refused_link_gets_the_fix_for_a_link_not_a_chmod(
    monkeypatch, tmp_path: Path
) -> None:
    real = tmp_path / "real.toml"
    real.write_text('enabled_entities = ["EMAIL"]\n', encoding="utf-8")
    real.chmod(0o600)
    target = _target(monkeypatch, tmp_path)
    target.parent.mkdir(mode=0o700)
    target.symlink_to(real)

    exit_code, payload = _config("--reset", "--yes")

    assert exit_code == 2
    assert payload["code"] == "SETTINGS_REFUSED"
    assert payload["fix"].startswith("Replace the link")
    assert target.is_symlink()
    assert real.read_text(encoding="utf-8") == 'enabled_entities = ["EMAIL"]\n'


def test_a_refused_file_is_reported_before_anything_is_asked(
    monkeypatch, tmp_path: Path
) -> None:
    target = _file(monkeypatch, tmp_path, _settings(), mode=0o664)

    result = runner.invoke(app, ["config", "--reset"], input="y\n")

    assert result.exit_code == 2
    assert "Save these settings?" not in result.output
    assert "writable by another user" in result.stderr
    assert target.read_text(encoding="utf-8") == _settings()
