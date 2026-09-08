"""The Codex transport verdict may not outlive the capture it rests on.

`docs/probe-2026-09-codex-watch.md` says `shim watch` can sit in front of Codex,
and that the shipped implementation does not. Both halves are read out of the
captured run rather than restated here, so a fixture that is regenerated with
different results fails the claims instead of quietly changing them.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "probe"
    / "codex-watch"
    / "probe-2026-09-08.json"
)
FORBIDDEN = ("authorization", "cf-ray", "chatgpt-account-id")


def _document() -> dict:
    if not FIXTURE.is_file():
        pytest.skip("codex watch probe not captured")
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _case(name: str) -> dict:
    for case in _document()["cases"]:
        if case["case"] == name:
            return case
    raise AssertionError(f"no {name} case in the capture")


def test_the_config_override_routes_codex_through_the_proxy() -> None:
    """T1: the half that works."""
    case = _case("config-override")

    assert case["reached_the_proxy"]
    paths = {record["path"].split("?")[0] for record in case["records"]}
    assert "/backend-api/codex/responses" in paths


def test_the_environment_variable_alone_routes_nothing() -> None:
    """T1, and the reason `shim watch -- codex` measures nothing today."""
    case = _case("environment-only")

    assert case["environment_variable"]
    assert not case["config_override"]
    assert not case["reached_the_proxy"]
    assert case["records"] == []


def test_the_websocket_upgrade_falls_back_to_http_in_the_same_turn() -> None:
    """T2: a 426, then the same path over HTTP, after it."""
    records = _case("config-override")["records"]
    upgrades = [item for item in records if item["kind"] == "upgrade"]
    posts = [
        item
        for item in records
        if item["kind"] == "request" and item["method"] == "POST"
    ]

    assert upgrades and upgrades[0]["answered"] == 426
    assert posts, "the turn never fell back to HTTP"
    assert posts[0]["at"] > upgrades[0]["at"]
    assert posts[0]["path"].split("?")[0] == upgrades[0]["path"].split("?")[0]


def test_cloudflare_accepted_a_request_re_sent_by_python() -> None:
    """T3, the verdict: a 200 through the same client the proxy uses."""
    records = _case("config-override")["records"]
    upstream = [item for item in records if item["kind"] == "request"]

    assert any(item["upstream_status"] == 200 for item in upstream)
    assert all(item["cf_ray_present"] for item in upstream), "Cloudflare fronted it"
    assert not any(item["upstream_status"] == 403 for item in upstream)


def test_the_only_rejection_was_the_account_quota_not_the_transport() -> None:
    """A 429 naming a usage limit is the application answering, not a bot wall."""
    records = _case("config-override")["records"]
    rejected = [
        item
        for item in records
        if item["kind"] == "request" and item["upstream_status"] == 429
    ]

    assert rejected, "the capture is expected to carry the quota rejection"
    assert rejected[0]["error_type"] == "usage_limit_reached"


def test_the_usage_shape_was_not_captured() -> None:
    """T4 is open, and the document must not imply otherwise."""
    records = _case("config-override")["records"]

    assert not any(item.get("usage_keys") for item in records)


def test_a_claude_subscription_sends_a_bearer_and_no_api_key() -> None:
    """R7b, for PRD-16's billing-mode classification."""
    records = _document()["anthropic_headers"]["records"]
    requests = [item for item in records if item["kind"] == "request"]

    assert requests
    names = set(requests[0]["header_names"])
    assert "authorization" in names
    assert "x-api-key" not in names
    assert requests[0]["upstream_status"] == 200


def test_the_capture_holds_no_credential_or_account_value() -> None:
    text = FIXTURE.read_text(encoding="utf-8") if FIXTURE.is_file() else ""
    if not text:
        pytest.skip("codex watch probe not captured")

    document = json.loads(text)
    for case in document["cases"]:
        for record in case["records"]:
            for name in FORBIDDEN:
                assert name not in record, f"{name} must be a name, never a value"
    assert "Bearer " not in text
    assert "plan_type" not in text
