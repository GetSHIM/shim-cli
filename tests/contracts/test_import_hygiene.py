from __future__ import annotations

import ast
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ALLOWED_THIRD_PARTY = frozenset({"phonenumbers", "tomli"})

SOURCE_ROOT = Path(__file__).parents[2] / "src" / "shim_cli"
TOP_LEVEL_OWNERS = frozenset(
    {
        "cli",
        "clients",
        "config",
        "events",
        "guard",
        "hook",
        "policy",
        "session",
        "settings_files",
        "watch",
    }
)
ALLOWED_INTERNAL_IMPORTS = {
    "cli": (
        "shim_cli.clients",
        "shim_cli.config",
        "shim_cli.events.diet",
        "shim_cli.guard",
        "shim_cli.session",
        "shim_cli.settings_files",
        "shim_cli.watch",
    ),
    "hook": (
        "shim_cli.clients",
        "shim_cli.config",
        "shim_cli.events",
        "shim_cli.guard",
        "shim_cli.session",
    ),
    "config": (
        "shim_cli.events.diet",
        "shim_cli.guard.entities",
        "shim_cli.policy",
        "shim_cli.settings_files",
    ),
    "clients": (
        "shim_cli.events",
        "shim_cli.policy",
        "shim_cli.session",
        "shim_cli.settings_files",
    ),
    "events": (
        "shim_cli.guard",
        "shim_cli.policy",
        "shim_cli.session.record",
    ),
}
ALLOWED_INTERNAL_EXCEPTIONS = frozenset(
    {
        ("clients/user_prompt_hook.py", "shim_cli.guard.GuardDecision"),
        ("clients/claude/hook.py", "shim_cli.guard.GuardDecision"),
        ("clients/codex/hook.py", "shim_cli.guard.GuardDecision"),
        ("clients/copilot/hook.py", "shim_cli.guard.GuardDecision"),
    }
)

_PROBE = """
import importlib.util
import json
import sys

from shim_cli import hook

payload = json.dumps(
    {"hook_event_name": "UserPromptSubmit", "prompt": PROMPT}
).encode()
if CLIENT == "copilot":
    payload = json.dumps(
        {"prompt": PROMPT, "transformedPrompt": PROMPT}
    ).encode()

hook._output(payload, CLIENT)

third_party = []
for name in sorted({module.split(".")[0] for module in sys.modules}):
    if name.startswith("_") or name == "shim_cli":
        continue
    try:
        specification = importlib.util.find_spec(name)
    except (ImportError, ValueError):
        continue
    if specification and specification.origin and "site-packages" in specification.origin:
        third_party.append(name)

shim_modules = sorted(m for m in sys.modules if m.startswith("shim_cli."))
sys.stdout.write(json.dumps(
    {"third_party": third_party, "shim": shim_modules}
))
"""


def _probe(client: str, prompt: str) -> dict:
    source = _PROBE.replace("CLIENT", repr(client)).replace("PROMPT", repr(prompt))
    result = subprocess.run(
        (sys.executable, "-I", "-B", "-c", source),
        capture_output=True,
        check=False,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr.decode()
    return json.loads(result.stdout)


def _module_name(path: Path) -> str:
    parts = list(path.relative_to(SOURCE_ROOT.parent).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _imported_modules(path: Path) -> set[tuple[str, bool]]:
    module = _module_name(path)
    package = module if path.name == "__init__.py" else module.rpartition(".")[0]
    imported = set()

    def collect(node: ast.AST, under_type_checking: bool = False) -> None:
        if isinstance(node, ast.Import):
            imported.update((alias.name, under_type_checking) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            name = node.module or ""
            if node.level:
                name = importlib.util.resolve_name("." * node.level + name, package)
            for alias in node.names:
                imported.add((f"{name}.{alias.name}", under_type_checking))

        if isinstance(node, ast.If) and (
            (isinstance(node.test, ast.Name) and node.test.id == "TYPE_CHECKING")
            or (
                isinstance(node.test, ast.Attribute)
                and isinstance(node.test.value, ast.Name)
                and node.test.value.id == "typing"
                and node.test.attr == "TYPE_CHECKING"
            )
        ):
            for child in node.body:
                collect(child, True)
            for child in node.orelse:
                collect(child, under_type_checking)
            return

        for child in ast.iter_child_nodes(node):
            collect(child, under_type_checking)

    collect(ast.parse(path.read_text(encoding="utf-8")))
    return imported


def test_internal_imports_follow_the_architecture() -> None:
    paths = tuple(SOURCE_ROOT.rglob("*.py"))
    actual_owners = set()
    for path in paths:
        owner = _module_name(path).split(".")[1:2]
        if owner:
            actual_owners.add(owner[0])
    assert actual_owners == TOP_LEVEL_OWNERS

    violations = []

    for path in paths:
        relative = path.relative_to(SOURCE_ROOT).as_posix()
        owner = _module_name(path).split(".")[1:2]
        if not owner:
            continue
        owner_name = owner[0]
        allowed = ALLOWED_INTERNAL_IMPORTS.get(owner_name, ())
        for imported, under_type_checking in _imported_modules(path):
            parts = imported.split(".")
            if parts[:1] != ["shim_cli"] or len(parts) < 2:
                continue
            target_owner = parts[1]
            if target_owner == owner_name or target_owner not in TOP_LEVEL_OWNERS:
                continue
            permitted = any(
                imported == prefix or imported.startswith(prefix + ".")
                for prefix in allowed
            )
            if not permitted and not (
                under_type_checking
                and (relative, imported) in ALLOWED_INTERNAL_EXCEPTIONS
            ):
                violations.append(f"{relative}: {owner_name} -> {imported}")

    assert not violations, "forbidden internal imports:\n" + "\n".join(
        sorted(violations)
    )


@pytest.mark.parametrize("client", ("codex", "claude", "copilot"))
@pytest.mark.parametrize(
    "prompt",
    (
        "Explain merge sort.",
        "Contact alice@example.com and call +90 532 123 45 67",
    ),
)
def test_the_hook_path_imports_no_unexpected_third_party_module(
    client: str, prompt: str
) -> None:
    observed = _probe(client, prompt)
    unexpected = set(observed["third_party"]) - ALLOWED_THIRD_PARTY

    assert not unexpected, (
        f"{client}: the hook path imported {sorted(unexpected)}. "
        f"Only {sorted(ALLOWED_THIRD_PARTY)} are permitted; "
        "move the import into the CLI."
    )


@pytest.mark.parametrize("client", ("codex", "claude", "copilot"))
def test_the_hook_path_never_imports_the_watch_proxy(client: str) -> None:
    observed = _probe(client, "Contact alice@example.com")

    watch = [name for name in observed["shim"] if name.startswith("shim_cli.watch")]

    assert not watch, f"{client}: the hook path imported {watch}"
