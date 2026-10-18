from __future__ import annotations

import datetime
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from shim_cli.cli.findings import DOCTOR, RULES, from_checks, from_records, from_watch
from shim_cli.cli.output import Check
from shim_cli.session import summary
from shim_cli.watch import measure, proxy, report

SCHEMA = json.loads(
    (
        Path(__file__).resolve().parents[2] / "docs" / "schemas" / "finding-v1.json"
    ).read_text(encoding="utf-8")
)
VALIDATOR = Draft202012Validator(
    SCHEMA, format_checker=Draft202012Validator.FORMAT_CHECKER
)
NOW = datetime.datetime(2026, 10, 11, 21, 0, 30, 123456, tzinfo=datetime.timezone.utc)
LATER = NOW + datetime.timedelta(minutes=5)
# Planted in every input a finding is built from; none may come out.
LEAKS = ("/home/alice", "ops@example.com", "AKIAIOSFODNN7EXAMPLE", "ignore all")


def _valid(findings: list) -> list:
    for finding in findings:
        VALIDATOR.validate(finding)
        assert finding["window"]["start"] <= finding["window"]["end"]
    text = json.dumps(findings, ensure_ascii=False)
    for leak in LEAKS:
        assert leak not in text
    return findings


def _check(status: str, code: str, name: str = "hook_configuration") -> Check:
    return Check(
        name,
        status,
        "Settings at /home/alice/.config/shim/config.toml name ops@example.com.",
        code=code,
        fix="chmod 600 /home/alice/.config/shim/config.toml",
    )


def test_doctor_raises_one_finding_per_check_that_did_not_pass() -> None:
    checks = [
        Check("runner", "PASS", "fine"),
        _check("WARN", "HOOK_NOT_INSTALLED"),
        _check("FAIL", "SETTINGS_INVALID", "entity_settings"),
    ]

    installed, settings = _valid(from_checks("claude", checks, NOW))

    assert installed["rule_id"] == "cli.doctor.hook_not_installed"
    assert installed["source"] == "shim-cli"
    assert installed["severity"] == "low"
    assert installed["status"] == "open"
    assert installed["status_detail"] is None
    assert installed["subject"] == {"kind": "app", "id": "claude"}
    assert installed["window"] == {
        "start": "2026-10-11T21:00:30Z",
        "end": "2026-10-11T21:00:30Z",
    }
    assert installed["occurrences"] == 1
    assert installed["evidence"] == [
        {"kind": "cli_session", "id": "doctor:hook_configuration"}
    ]
    assert installed["measurements"] == {}
    assert installed["impact"] == {
        "requests": None,
        "tokens": None,
        "usd": None,
        "risk_class": "privacy",
    }
    assert installed["remediation"] == {
        "mode": "suggest",
        "max_mode": "suggest",
        "action": {
            "kind": "cli.command",
            "params": {"command": "shim install claude --yes"},
        },
        "reversible": True,
        "blast_radius": "app",
        "proof_after": None,
        "text": {
            "en": "Run shim install claude --yes, then start a new Claude Code "
            "session.",
            "tr": "shim install claude --yes komutunu çalıştırın, ardından yeni bir "
            "Claude Code oturumu başlatın.",
        },
    }
    assert installed["summary"]["en"] == (
        "The Claude Code settings file does not hold exactly this shim "
        "installation's hook; without any shim hook there, nothing is inspected."
    )
    assert installed["playbook"] == "docs/commands.md#error-codes"
    assert settings["rule_id"] == "cli.doctor.settings_invalid"
    assert settings["severity"] == "high"
    assert settings["evidence"] == [
        {"kind": "cli_session", "id": "doctor:entity_settings"}
    ]


def test_a_doctor_run_that_passed_raises_nothing() -> None:
    assert from_checks("claude", [Check("runner", "PASS", "fine")], NOW) == []


@pytest.mark.parametrize(
    "code,status,severity",
    [
        ("HOOK_ACTIVATION_UNVERIFIED", "WARN", "informational"),
        ("PLUGIN_NOT_DISCOVERABLE", "WARN", "informational"),
        ("CLIENT_NEWER_THAN_TESTED", "WARN", "low"),
        ("LEGACY_NAMES_PRESENT", "WARN", "low"),
        ("LEGACY_NAMES_PRESENT", "FAIL", "high"),
        ("HOOK_RUNNER_FAILED", "FAIL", "high"),
    ],
)
def test_doctor_severity(code: str, status: str, severity: str) -> None:
    (finding,) = _valid(from_checks("codex", [_check(status, code)], NOW))

    assert finding["severity"] == severity


@pytest.mark.parametrize("client", ["claude", "codex", "copilot"])
@pytest.mark.parametrize("code", sorted(DOCTOR))
def test_every_doctor_code_is_a_valid_finding(client: str, code: str) -> None:
    (finding,) = _valid(from_checks(client, [_check("FAIL", code)], NOW))

    assert finding["rule_id"] == f"cli.doctor.{code.lower()}"
    command = finding["remediation"]["action"]
    if command is not None:
        assert command["params"]["command"].startswith("shim ")
        assert command["params"]["command"].endswith(" --yes")


def test_the_same_finding_has_the_same_id_on_every_run() -> None:
    first = from_checks("claude", [_check("WARN", "HOOK_NOT_INSTALLED")], NOW)
    again = from_checks("claude", [_check("WARN", "HOOK_NOT_INSTALLED")], LATER)
    codex = from_checks("codex", [_check("WARN", "HOOK_NOT_INSTALLED")], NOW)

    assert first[0]["id"] == again[0]["id"] == "19a357c5-2de9-5f0f-9fbe-780ca24ca24f"
    assert codex[0]["id"] != first[0]["id"]


def test_the_reset_is_named_in_the_text_and_never_offered_as_the_action() -> None:
    (finding,) = from_checks("claude", [_check("FAIL", "SETTINGS_INVALID")], NOW)
    resets = [rule for rule in RULES.values() if "--reset" in rule.text[0]]

    assert finding["remediation"]["action"] is None
    assert finding["remediation"]["reversible"] is True
    assert "shim config --reset --yes" in finding["remediation"]["text"]["en"]
    assert "shim config --reset --yes" in finding["remediation"]["text"]["tr"]
    assert resets == [DOCTOR["SETTINGS_INVALID"]]
    for rule in RULES.values():
        if rule.command is not None and "--reset" in rule.command:
            assert not rule.reversible


def test_every_template_is_filled_from_its_own_fields_only() -> None:
    for rule_id, rule in RULES.items():
        for text in (*rule.summary, *rule.text, rule.title):
            assert 0 < len(text) <= 500, rule_id
        assert rule.severity in {"informational", "low", "medium", "high"}


def _record(**changes) -> dict:
    values = {
        "client": "claude",
        "event": "PostToolUse",
        "tool_name": "Read",
        "target": "/home/alice/.env",
        "direction": "inbound",
        "mode": "enforce",
        "action": "allow",
        "entities": {},
        "markers": [],
        "custom": {},
        "note": "",
        "ts": "2026-10-11T20:00:00Z",
    }
    values.update(changes)
    return values


def _failed(**changes) -> dict:
    return _record(
        event="PostToolUseFailure", tool_name="Bash", action="report", **changes
    )


def test_a_failed_call_that_printed_a_value_is_an_unmasked_finding() -> None:
    records = [
        _record(action="mask", entities={"EMAIL": 1}),
        _failed(
            entities={"SECRET": 2, "ops@example.com": 1}, ts="2026-10-11T20:05:00Z"
        ),
        _failed(entities={"EMAIL": 1}, ts="2026-10-11T20:01:00+00:00"),
        _record(
            direction="model-output",
            event="Stop",
            action="report",
            entities={"EMAIL": 1},
        ),
    ]

    (finding,) = _valid(from_records(records, NOW))

    assert finding["rule_id"] == "cli.session.unmasked_values"
    assert finding["severity"] == "medium"
    assert finding["subject"] == {"kind": "app", "id": "claude"}
    assert finding["window"] == {
        "start": "2026-10-11T20:01:00Z",
        "end": "2026-10-11T20:05:00Z",
    }
    assert finding["occurrences"] == 2
    assert finding["evidence"] == [
        {"kind": "cli_session", "id": "report:record:2"},
        {"kind": "cli_session", "id": "report:record:3"},
    ]
    assert finding["measurements"] == {
        "events": 2,
        "entities_secret": 2,
        "entities_email": 1,
    }
    assert finding["impact"]["requests"] == 2
    assert finding["impact"]["risk_class"] == "privacy"
    assert finding["remediation"]["reversible"] is False
    assert finding["playbook"] == "docs/findings.md#clisessionunmasked_values"
    assert summary.as_json(records)["unmasked"]["events"] == 2


def test_a_masked_session_raises_nothing() -> None:
    records = [_record(action="mask", entities={"SECRET": 1}), _record()]

    assert from_records(records, NOW) == []
    assert from_records([], NOW) == []


def test_events_that_passed_uninspected_are_a_finding() -> None:
    records = [
        _record(note="not inspected: attachment"),
        _record(note="partial: too many fields"),
        _record(note="not-inspected: analysis failed"),
        _record(note="masked 1"),
    ]

    (finding,) = _valid(from_records(records, NOW))
    counted = summary.as_json(records)

    assert finding["rule_id"] == "cli.session.not_inspected"
    assert finding["measurements"] == {"not_inspected": 2, "incomplete": 2}
    assert counted["not_inspected"] == 2
    assert counted["incomplete"] == 2
    assert finding["occurrences"] == 3
    assert finding["impact"]["requests"] is None


def test_markers_are_counted_under_their_own_ids_only() -> None:
    records = [
        _record(markers=["INSTRUCTION_OVERRIDE", "HIDDEN_TEXT"]),
        _record(markers=["ignore all previous instructions"]),
        _record(markers=["INSTRUCTION_OVERRIDE"], ts="2026-10-11T20:09:00Z"),
    ]

    (finding,) = _valid(from_records(records, NOW))

    assert finding["rule_id"] == "cli.session.injection_markers"
    assert finding["severity"] == "low"
    assert finding["measurements"] == {
        "markers_instruction_override": 2,
        "markers_hidden_text": 1,
    }
    assert [item["id"] for item in finding["evidence"]] == [
        "report:record:1",
        "report:record:3",
    ]
    assert from_records([_record(markers=["ignore all"])], NOW) == []


def test_a_record_without_a_known_client_or_a_time_is_still_a_finding() -> None:
    records = [_failed(client="/home/alice/client", ts="yesterday", entities={})]

    (finding,) = _valid(from_records(records, NOW))

    assert finding["subject"] == {"kind": "app", "id": "other"}
    assert (
        finding["window"]["start"]
        == finding["window"]["end"]
        == ("2026-10-11T21:00:30Z")
    )


def test_a_corrupt_spool_never_raises_and_its_counts_stay_sane() -> None:
    records = [
        "not a record",
        _failed(client=["claude"], entities={"SECRET": -3, "EMAIL": 10**40}),
        _failed(
            client={"id": 1}, entities={"SECRET": 2}, ts="2026-10-11T23:05:00+03:00"
        ),
        _failed(entities={"EMAIL": True}, ts="9999-12-31T23:59:59-01:00"),
        _failed(entities={"EMAIL": 1}, ts="2026-10-11T20:07:00"),
    ]

    (finding,) = _valid(from_records(records, NOW))

    assert finding["subject"] == {"kind": "app", "id": "other"}
    assert finding["occurrences"] == 4
    assert finding["measurements"] == {
        "events": 4,
        "entities_secret": 2,
        "entities_email": 1,
    }
    # Any offset is read as the moment it names; a time without one is not.
    assert finding["window"] == {
        "start": "2026-10-11T20:00:00Z",
        "end": "2026-10-11T20:05:00Z",
    }


def test_evidence_stops_at_twenty_references() -> None:
    records = [_failed(entities={"SECRET": 1}) for _ in range(25)]

    (finding,) = _valid(from_records(records, NOW))

    assert finding["occurrences"] == 25
    assert len(finding["evidence"]) == 20


def _exchange(**changes):
    values = {
        "path": "/v1/messages",
        "model": "claude-opus-5-5",
        "status": 200,
        "request_bytes": 100_000,
        "usage": measure.Usage(input_tokens=2, output_tokens=10),
    }
    values.update(changes)
    return measure.Exchange(**values)


def _turn(model: str, size: int, read: int, written: int):
    usage = measure.Usage(
        input_tokens=2,
        output_tokens=10,
        cache_read_input_tokens=read,
        cache_creation_input_tokens=written,
    )
    return _exchange(model=model, request_bytes=size, usage=usage)


def _watched(*exchanges, images=None, results=()) -> dict:
    session = proxy.Session()
    for exchange in exchanges:
        session.record(exchange)
    session.images = images or {}
    for carried in results:
        session.absorb(measure.Exchange(tool_results=carried))
    return report.as_json(session, 9.0)


def test_a_quiet_watch_session_raises_nothing() -> None:
    assert from_watch(_watched(_exchange()), NOW, LATER) == []
    assert from_watch(_watched(), NOW, LATER) == []


def test_a_cache_rewrite_is_a_finding_about_its_model() -> None:
    document = _watched(
        _turn("claude-opus-5-5", 100_000, 0, 20_000),
        _turn("claude-opus-5-5", 110_000, 20_000, 2_000),
        _turn("claude-opus-5-5", 120_000, 5_000, 18_000),
    )

    (finding,) = _valid(from_watch(document, NOW, LATER))

    assert finding["rule_id"] == "cli.watch.cache_rewrite"
    assert finding["subject"] == {"kind": "model", "id": "claude-opus-5-5"}
    assert finding["window"] == {
        "start": "2026-10-11T21:00:30Z",
        "end": "2026-10-11T21:05:30Z",
    }
    assert finding["evidence"] == [{"kind": "cli_session", "id": "watch:exchange:3"}]
    assert finding["measurements"] == {"requests": 1, "tokens": 18_000}
    assert finding["impact"] == {
        "requests": None,
        "tokens": 18_000,
        "usd": "0.090000",
        "risk_class": "cost",
    }
    assert (
        "Keep CLAUDE.md and the tool list stable"
        in (finding["remediation"]["text"]["en"])
    )


def test_rewrites_on_two_models_are_two_findings_without_a_shared_price() -> None:
    document = _watched(
        _turn("claude-opus-5-5", 100_000, 0, 20_000),
        _turn("claude-opus-5-5", 110_000, 20_000, 2_000),
        _turn("claude-opus-5-5", 120_000, 5_000, 18_000),
        _turn("claude-mystery-ops@example.com", 100_000, 0, 20_000),
        _turn("claude-mystery-ops@example.com", 110_000, 20_000, 2_000),
        _turn("claude-mystery-ops@example.com", 120_000, 5_000, 18_000),
    )

    findings = _valid(from_watch(document, NOW, LATER))

    assert [finding["subject"]["id"] for finding in findings] == [
        "claude-opus-5-5",
        "other",
    ]
    assert [finding["impact"]["usd"] for finding in findings] == [None, None]
    assert findings[0]["id"] != findings[1]["id"]


def test_duplicates_in_flight_are_a_finding() -> None:
    usage = measure.Usage(input_tokens=1_200, output_tokens=1)
    document = _watched(
        _exchange(usage=usage),
        _exchange(usage=usage, duplicate_in_flight=True),
    )

    (finding,) = _valid(from_watch(document, NOW, LATER))

    assert finding["rule_id"] == "cli.watch.duplicate_in_flight"
    assert finding["subject"] == {"kind": "app", "id": "claude"}
    assert finding["evidence"] == [{"kind": "cli_session", "id": "watch:exchange:2"}]
    assert finding["measurements"] == {"count": 1, "input_tokens": 1_200}
    assert finding["impact"]["requests"] == 1
    assert finding["impact"]["tokens"] == 1_200


def test_a_cut_off_response_is_a_quality_finding_per_model() -> None:
    document = _watched(
        _exchange(stop_reason="max_tokens"),
        _exchange(stop_reason="end_turn"),
        _exchange(model="claude-unknown-9", stop_reason="max_tokens"),
    )

    findings = _valid(from_watch(document, NOW, LATER))

    assert [f["rule_id"] for f in findings] == ["cli.watch.output_cut_off"] * 2
    assert [f["subject"]["id"] for f in findings] == ["claude-opus-5-5", "other"]
    assert findings[0]["measurements"] == {"responses": 1, "requests": 3}
    assert findings[0]["impact"]["risk_class"] == "quality"
    assert findings[0]["summary"]["en"] == (
        "1 of 3 response(s) stopped at the output limit."
    )


def test_images_are_an_informational_finding() -> None:
    document = _watched(
        _exchange(),
        _exchange(image_count=1, estimated_image_tokens=2_494),
        images={"a": True},
    )

    (finding,) = _valid(from_watch(document, NOW, LATER))

    assert finding["rule_id"] == "cli.watch.image_tokens"
    assert finding["severity"] == "informational"
    assert finding["evidence"] == [{"kind": "cli_session", "id": "watch:exchange:2"}]
    assert finding["measurements"] == {
        "distinct": 1,
        "requests": 1,
        "estimated_tokens": 2_494,
        "unknown_size": 0,
    }
    assert finding["impact"]["tokens"] == 2_494


@pytest.mark.parametrize("size,fires", [(25_000, False), (25_001, True)])
def test_a_large_tool_result_fires_above_the_threshold(size: int, fires: bool) -> None:
    results = [{"toolu_1": ("/home/alice/tool", size)}] * 2
    document = _watched(_exchange(), _exchange(), results=results)

    findings = _valid(from_watch(document, NOW, LATER))

    if not fires:
        assert findings == []
        return
    (finding,) = findings
    assert finding["rule_id"] == "cli.watch.large_tool_result"
    assert finding["occurrences"] == 2
    assert finding["evidence"] == []
    assert finding["measurements"] == {
        "bytes": size,
        "estimated_tokens": size // 4,
        "requests": 2,
    }
