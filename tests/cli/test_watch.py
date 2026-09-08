import os
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from shim_cli.cli.app import app
from shim_cli.watch import proxy


@pytest.mark.parametrize(
    "client,variable", [("claude", "ANTHROPIC_BASE_URL"), ("codex", "OPENAI_BASE_URL")]
)
@pytest.mark.parametrize("as_json", [False, True])
def test_override_refuses_before_start_without_leaking(
    monkeypatch, client, variable, as_json
):
    monkeypatch.setenv(variable, "https://synthetic:0123456789abcdef@example.com")
    start = Mock()
    child = Mock()
    monkeypatch.setattr(proxy, "start", start)
    monkeypatch.setattr("subprocess.Popen", child)
    result = CliRunner().invoke(
        app, ["watch", *(["--json"] if as_json else []), "--", client]
    )
    assert result.exit_code == 2
    assert variable in result.output
    assert "example.com" not in result.output
    assert "0123456789abcdef" not in result.output
    start.assert_not_called()
    child.assert_not_called()


@pytest.mark.parametrize(
    "client,variable", [("claude", "ANTHROPIC_BASE_URL"), ("codex", "OPENAI_BASE_URL")]
)
@pytest.mark.parametrize("value", [None, ""])
def test_empty_override_starts_without_mutating_parent(
    monkeypatch, client, variable, value
):
    if value is None:
        monkeypatch.delenv(variable, raising=False)
    else:
        monkeypatch.setenv(variable, value)
    monkeypatch.setattr("shutil.which", lambda _: "/synthetic/client")
    running = SimpleNamespace(
        base_url="http://127.0.0.1:1234", session=proxy.Session(), stop=Mock()
    )
    monkeypatch.setattr(proxy, "start", lambda *_: running)
    child = Mock(return_value=SimpleNamespace(wait=lambda: 0))
    monkeypatch.setattr("subprocess.Popen", child)
    result = CliRunner().invoke(app, ["watch", "--json", "--", client])
    assert result.exit_code == 0, result.output
    assert child.call_args.kwargs["env"][variable] == running.base_url
    assert os.environ.get(variable) == value
    running.stop.assert_called_once()
