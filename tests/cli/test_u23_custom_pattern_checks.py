"""U23 (F097, F133): `shim config` and `shim doctor` check a custom pattern as
the hook runs it."""

from __future__ import annotations

import json
from pathlib import Path

from click import unstyle
from typer.testing import CliRunner

from shim_cli.cli.app import app

runner = CliRunner()


def _settings(monkeypatch, tmp_path: Path, body: str | None = None) -> Path:
    target = tmp_path / "settings" / "config.toml"
    monkeypatch.setenv("SHIM_CONFIG", str(target))
    if body is not None:
        target.parent.mkdir(mode=0o700, parents=True)
        target.write_text(body, encoding="utf-8")
        target.chmod(0o600)
    return target


def _doctor_check() -> dict:
    result = runner.invoke(app, ["doctor", "claude", "--json"])
    checks = {item["name"]: item for item in json.loads(result.output)["checks"]}
    return checks["custom_patterns"]


def test_doctor_checks_a_pattern_with_its_ignore_case_flag(
    monkeypatch, tmp_path
) -> None:
    body = (
        'enabled_entities = ["CUSTOM"]\n[[custom]]\nname = "TICKET"\n'
        "pattern = '\\b(?:id|ID)*;'\nignore_case = {}\n"
    )
    _settings(monkeypatch, tmp_path, body.format("false"))
    assert _doctor_check()["status"] == "PASS"

    _settings(monkeypatch, tmp_path / "other", body.format("true"))
    check = _doctor_check()

    assert check["status"] == "FAIL"
    assert check["code"] == "CUSTOM_PATTERN_UNSAFE"


def test_config_refuses_a_quadratic_pattern_before_writing_it(
    monkeypatch, tmp_path
) -> None:
    target = _settings(monkeypatch, tmp_path)

    result = runner.invoke(
        app, ["config", "--custom", r"ORDER_REF=\d+-\d+-\d+", "--yes"], color=False
    )

    assert result.exit_code == 2
    assert "ORDER_REF backtracks on repeated input" in unstyle(result.output)
    assert not target.exists()
