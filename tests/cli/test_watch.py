import os
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from shim_cli.cli.app import app
from shim_cli.watch import proxy


@pytest.mark.parametrize("client,variable", [("claude", "ANTHROPIC_BASE_URL")])
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


@pytest.mark.parametrize("client,variable", [("claude", "ANTHROPIC_BASE_URL")])
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


def test_the_json_report_carries_both_directions(monkeypatch, valid_findings):
    import json

    from shim_cli.watch import measure

    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    monkeypatch.setattr("shutil.which", lambda _: "/synthetic/client")
    session = proxy.Session()
    session.record(
        measure.Exchange(
            path="/v1/messages",
            model="claude-sonnet-5",
            status=200,
            entities_by_section={"messages": {"IBAN": 2}},
            response_entities={"thinking": {"IBAN": 1}},
            response_scan_status="known",
            stop_reason="max_tokens",
        )
    )
    running = SimpleNamespace(
        base_url="http://127.0.0.1:1234", session=session, stop=Mock()
    )
    monkeypatch.setattr(proxy, "start", lambda *_: running)
    monkeypatch.setattr(
        "subprocess.Popen", Mock(return_value=SimpleNamespace(wait=lambda: 0))
    )

    result = CliRunner().invoke(app, ["watch", "--json", "--", "claude"])

    document = json.loads(result.stdout)
    assert document["entities"] == {"IBAN": 2}
    assert document["entities_by_section"] == {"messages": {"IBAN": 2}}
    assert document["response_entities"] == {"thinking": {"IBAN": 1}}
    assert document["response_scan"] == "known"
    assert document["stop_reasons"] == {"max_tokens": 1}
    assert document["exchanges"][0]["response_scan_status"] == "known"
    (finding,) = valid_findings(document)
    assert finding["rule_id"] == "cli.watch.output_cut_off"
    assert finding["subject"] == {"kind": "model", "id": "claude-sonnet-5"}
    assert finding["window"]["start"] <= finding["window"]["end"]


@pytest.mark.parametrize("as_json", [False, True])
def test_codex_is_refused_rather_than_proxied_past(monkeypatch, as_json):
    """Setting `OPENAI_BASE_URL` reached nothing on Codex 0.151.0, so the old
    behaviour ran the session unmeasured and still exited 0."""
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.setattr("shutil.which", lambda _: "/synthetic/codex")
    start = Mock()
    child = Mock()
    monkeypatch.setattr(proxy, "start", start)
    monkeypatch.setattr("subprocess.Popen", child)

    result = CliRunner().invoke(
        app, ["watch", *(["--json"] if as_json else []), "--", "codex"]
    )

    assert result.exit_code == 2
    assert "codex" in result.output
    start.assert_not_called()
    child.assert_not_called()
    assert os.environ.get("OPENAI_BASE_URL") is None


def _through_the_proxy(monkeypatch, request_text: str, response_text: str) -> None:
    """Run `claude` as a child that sends one model request through the real
    proxy; only the HTTPS leg to the provider is replaced by a canned stream."""
    import http.client
    import json
    import urllib.parse

    event = json.dumps(
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": response_text},
        }
    )
    stream = f"event: content_block_delta\ndata: {event}\n\n".encode()

    class Response:
        status = 200

        def __init__(self) -> None:
            self.chunks = [stream, b""]

        def getheaders(self):
            return [("Content-Type", "text/event-stream")]

        def getheader(self, name):
            return "text/event-stream" if name == "Content-Type" else None

        def read1(self, _size):
            return self.chunks.pop(0)

    class Provider:
        def __init__(self, *_args, **_kwargs):
            pass

        def putrequest(self, *_args, **_kwargs):
            pass

        def putheader(self, *_args):
            pass

        def endheaders(self, body):
            # The proxy captures the request while it relays it.
            for _chunk in body:
                pass

        def getresponse(self):
            return Response()

        def close(self):
            pass

    def claude(_command, env):
        body = json.dumps(
            {
                "model": "claude-sonnet-5",
                "messages": [{"role": "user", "content": request_text}],
            }
        ).encode()
        address = urllib.parse.urlsplit(env["ANTHROPIC_BASE_URL"])
        connection = http.client.HTTPConnection(address.hostname, address.port)
        connection.request(
            "POST", "/v1/messages", body, {"Content-Type": "application/json"}
        )
        connection.getresponse().read()
        connection.close()
        return SimpleNamespace(wait=lambda: 0)

    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    monkeypatch.setattr("shutil.which", lambda _: "/synthetic/claude")
    monkeypatch.setattr(http.client, "HTTPSConnection", Provider)
    monkeypatch.setattr("subprocess.Popen", claude)


def test_watch_measures_with_the_users_entity_settings_and_custom_patterns(
    monkeypatch,
):
    """`shim scan`, the hook and `shim audit` read the settings file; watch
    counted with the built-in defaults, so the same text gave another answer."""
    import json

    configured = CliRunner().invoke(
        app,
        [
            "config",
            "--custom-literal",
            "PROJECT=ACME-PROJECT-ZEBRA",
            "--disable",
            "EMAIL",
            "--enable",
            "TR_LICENSE_PLATE",
            "--yes",
        ],
    )
    assert configured.exit_code == 0, configured.output
    text = "host ACME-PROJECT-ZEBRA, mail jane.doe@example.com, plaka 34 ABC 123"
    _through_the_proxy(monkeypatch, text, text)

    result = CliRunner().invoke(app, ["watch", "--json", "--", "claude"])

    assert result.exit_code == 0, result.output
    document = json.loads(result.stdout)
    assert document["entities"] == {"CUSTOM": 1, "TR_LICENSE_PLATE": 1}
    assert document["response_entities"] == {
        "text": {"CUSTOM": 1, "TR_LICENSE_PLATE": 1}
    }
    assert document["custom"] == {
        "request": {"PROJECT": 1},
        "response": {"PROJECT": 1},
    }


@pytest.mark.parametrize("as_json", [False, True])
def test_watch_refuses_unreadable_settings_before_starting_anything(
    monkeypatch, tmp_path, as_json
):
    import json

    settings = tmp_path / "shim.toml"
    settings.write_text("enabled_entities = [\n", encoding="utf-8")
    monkeypatch.setenv("SHIM_CONFIG", str(settings))
    monkeypatch.setenv("COLUMNS", "1000")
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    monkeypatch.setattr("shutil.which", lambda _: "/synthetic/claude")
    start = Mock()
    child = Mock()
    monkeypatch.setattr(proxy, "start", start)
    monkeypatch.setattr("subprocess.Popen", child)

    result = CliRunner().invoke(
        app, ["watch", *(["--json"] if as_json else []), "--", "claude"]
    )

    start.assert_not_called()
    child.assert_not_called()
    assert result.exit_code == 2
    if as_json:
        document = json.loads(result.stdout)
        assert document["code"] == "SETTINGS_INVALID"
        assert document["error"].startswith(f"Settings at {settings} are invalid")
    else:
        assert f"Settings at {settings} are invalid" in result.output
