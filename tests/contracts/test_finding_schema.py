"""The finding schema is the gateway's, byte for byte, and every rule has a
playbook that exists. A copy that drifted, or an anchor that leads nowhere,
gives an agent a shape or a page shim does not have.
"""

from __future__ import annotations

import ast
import hashlib
import re
from pathlib import Path

from shim_cli.cli.findings import DOCTOR, RULES

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

ROOT = Path(__file__).resolve().parents[2]
SCHEMA = ROOT / "docs" / "schemas" / "finding-v1.json"
GATEWAY_COMMIT = "d000bebb278af5ee0acc9fe742d33ba05bbdc184"
FINDING_SCHEMA_SHA256 = (
    "c89d03eeb8e3456f00dee116bcaccf33a4f124d2e0c8e838d139818db0872536"
)


def _slug(heading: str) -> str:
    """GitHub's anchor for a heading."""
    text = heading.replace("`", "").strip().lower()
    return re.sub(r"[^\w\- ]", "", text).replace(" ", "-")


def _anchors(path: Path) -> set[str]:
    text = path.read_text(encoding="utf-8")
    return {
        _slug(line.lstrip("#")) for line in text.splitlines() if line.startswith("#")
    }


def test_the_schema_is_the_gateways_copy() -> None:
    digest = hashlib.sha256(SCHEMA.read_bytes()).hexdigest()

    assert digest == FINDING_SCHEMA_SHA256, (
        f"copy docs/schemas/finding-v1.json from the gateway again (GetSHIM/shim "
        f"{GATEWAY_COMMIT} was the last copy) and update both constants"
    )


def test_every_playbook_names_a_heading_that_exists() -> None:
    for rule_id, rule in RULES.items():
        document, anchor = rule.playbook.split("#")
        assert (ROOT / document).is_file(), rule_id
        assert anchor in _anchors(ROOT / document), rule_id


def test_every_findings_heading_names_a_rule() -> None:
    text = (ROOT / "docs" / "findings.md").read_text(encoding="utf-8")
    headings = re.findall(r"^## `([^`]+)`$", text, re.M)

    assert len(headings) == len(re.findall(r"^## ", text, re.M))
    assert set(headings) == {
        rule_id for rule_id in RULES if not rule_id.startswith("cli.doctor.")
    }


def test_every_doctor_code_has_a_rule() -> None:
    codes = set()
    for path in (ROOT / "src" / "shim_cli" / "cli").glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "Check":
                codes |= {
                    keyword.value.value
                    for keyword in node.keywords
                    if keyword.arg == "code" and isinstance(keyword.value, ast.Constant)
                }

    assert codes == set(DOCTOR)


def test_the_validator_is_a_development_dependency_only() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert not any(
        requirement.startswith("jsonschema")
        for requirement in project["project"]["dependencies"]
    )
    assert any(
        requirement.startswith("jsonschema")
        for requirement in project["dependency-groups"]["dev"]
    )
