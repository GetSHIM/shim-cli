from __future__ import annotations

import pytest

from shim_cli import policy
from shim_cli.policy import (
    ALLOW,
    DENY,
    ENFORCE,
    EXECUTABLE_TEXT,
    INBOUND,
    LOCAL_TOOL,
    LOCAL_WRITE,
    MASK,
    OBSERVE,
    OUTBOUND,
    REPORT,
    USER_PROMPT,
    WARN,
    decide,
    direction_for,
)

# Every tool the tested clients name, by where its input goes. Claude Code
# 2.1.286: its own tool list, and the names its permission rules group as file,
# shell and web tools. VS Code: Copilot Chat's tool registry. A name missing
# here, an MCP tool or one a newer client adds, is outbound and never rewritten.
CLAUDE = {
    **dict.fromkeys(
        (
            "Agent",
            "AskUserQuestion",
            "CronCreate",
            "CronDelete",
            "CronList",
            "EnterPlanMode",
            "EnterWorktree",
            "ExitPlanMode",
            "ExitWorktree",
            "Glob",
            "Grep",
            "LS",
            "LSP",
            "ListAgents",
            "NotebookRead",
            "Read",
            "ScheduleWakeup",
            "Skill",
            "StructuredOutput",
            "Task",
            "TaskCreate",
            "TaskGet",
            "TaskList",
            "TaskOutput",
            "TaskStop",
            "TaskUpdate",
            "TodoWrite",
            "ToolSearch",
            "Workflow",
        ),
        LOCAL_TOOL,
    ),
    **dict.fromkeys(
        ("Bash", "BashOutput", "Monitor", "PowerShell", "mcp__ide__executeCode"),
        EXECUTABLE_TEXT,
    ),
    **dict.fromkeys(("Edit", "MultiEdit", "NotebookEdit", "Write"), LOCAL_WRITE),
    **dict.fromkeys(("WebFetch", "WebSearch"), OUTBOUND),
}
VSCODE = {
    **dict.fromkeys(
        (
            "copilot_getNotebookSummary",
            "execution_subagent",
            "explore_subagent",
            "file_search",
            "get_changed_files",
            "get_errors",
            "get_task_output",
            "get_terminal_output",
            "grep_search",
            "list_dir",
            "manage_todo_list",
            "read_file",
            "read_notebook_cell_output",
            "read_project_structure",
            "runSubagent",
            "search_subagent",
            "search_workspace_symbols",
            "skill",
            "terminal_last_command",
            "terminal_selection",
            "test_search",
            "tool_search",
            "view_image",
            "vscode_askQuestions",
        ),
        LOCAL_TOOL,
    ),
    **dict.fromkeys(
        (
            "create_and_run_task",
            "run_in_terminal",
            "run_playwright_code",
            "send_to_terminal",
        ),
        EXECUTABLE_TEXT,
    ),
    **dict.fromkeys(
        (
            "apply_patch",
            "create_directory",
            "create_file",
            "edit_notebook_file",
            "insert_edit_into_file",
            "multi_replace_string_in_file",
            "replace_string_in_file",
        ),
        LOCAL_WRITE,
    ),
    "fetch_webpage": OUTBOUND,
}
# Never rewritten: shim cannot know where their input goes.
UNKNOWN = (
    "mcp__probe__probe_echo",
    "mcp__filesystem__write_file",
    "SendMessage",
    "ReadMcpResourceTool",
    "SomeToolANewerClientAdds",
    "",
)


@pytest.mark.parametrize(("client", "table"), (("claude", CLAUDE), ("vscode", VSCODE)))
def test_every_known_tool_input_has_the_direction_of_where_it_goes(
    client: str, table: dict
) -> None:
    assert set(policy.TOOLS[client]) == set(table)
    for tool, expected in table.items():
        assert direction_for("PreToolUse", tool, client) == expected, tool
        assert policy.tool_kind(client, tool) == expected, tool


@pytest.mark.parametrize("client", ("claude", "vscode"))
@pytest.mark.parametrize("tool", UNKNOWN)
def test_an_mcp_or_unknown_tool_input_is_outbound_and_never_rewritten(
    client: str, tool: str
) -> None:
    assert direction_for("PreToolUse", tool, client) == OUTBOUND
    assert policy.tool_kind(client, tool) in (policy.MCP, "")


def test_tool_names_belong_to_their_own_client() -> None:
    # VS Code's terminal is a command there and nothing Claude Code knows.
    assert direction_for("PreToolUse", "run_in_terminal", "vscode") == EXECUTABLE_TEXT
    assert direction_for("PreToolUse", "run_in_terminal", "claude") == OUTBOUND
    assert direction_for("PreToolUse", "Bash", "vscode") == OUTBOUND
    assert policy.tool_kind("claude", "mcp__x__y") == policy.MCP
    assert policy.tool_kind("vscode", "mcp__x__y") == ""


@pytest.mark.parametrize(
    ("event", "tool", "client", "expected"),
    (
        ("UserPromptSubmit", "", "claude", USER_PROMPT),
        ("userPromptTransformed", "", "copilot", USER_PROMPT),
        ("PostToolUse", "Read", "claude", INBOUND),
        ("PostToolUse", "Bash", "claude", INBOUND),
        ("PostToolUse", "Write", "claude", INBOUND),
        ("PostToolUseFailure", "Bash", "claude", INBOUND),
        ("PostToolUse", "mcp__server__tool", "claude", INBOUND),
        ("PostToolUse", "run_in_terminal", "vscode", INBOUND),
        # The person's own answer, not content from elsewhere.
        ("PostToolUse", "AskUserQuestion", "claude", LOCAL_TOOL),
        ("PostToolUse", "vscode_askQuestions", "vscode", LOCAL_TOOL),
        ("PostToolUse", "AskUserQuestion", "vscode", INBOUND),
    ),
)
def test_prompts_and_results_are_decided_by_event(
    event: str, tool: str, client: str, expected: str
) -> None:
    assert direction_for(event, tool, client) == expected


def test_unknown_events_are_refused_rather_than_guessed() -> None:
    with pytest.raises(ValueError, match="unsupported hook event"):
        direction_for("SessionStart", "Read", "claude")


@pytest.mark.parametrize(
    ("direction", "mode", "action"),
    (
        (LOCAL_WRITE, OBSERVE, ALLOW),
        (LOCAL_WRITE, WARN, REPORT),
        (LOCAL_WRITE, ENFORCE, DENY),
        (EXECUTABLE_TEXT, OBSERVE, ALLOW),
        (EXECUTABLE_TEXT, WARN, REPORT),
        (EXECUTABLE_TEXT, ENFORCE, DENY),
        (USER_PROMPT, OBSERVE, ALLOW),
        (USER_PROMPT, WARN, REPORT),
        (USER_PROMPT, ENFORCE, DENY),
        # Report only: a local tool's input is an address or a question.
        (LOCAL_TOOL, OBSERVE, ALLOW),
        (LOCAL_TOOL, WARN, REPORT),
        (LOCAL_TOOL, ENFORCE, REPORT),
        (OUTBOUND, OBSERVE, ALLOW),
        (OUTBOUND, WARN, REPORT),
        (OUTBOUND, ENFORCE, MASK),
        (INBOUND, OBSERVE, ALLOW),
        (INBOUND, WARN, REPORT),
        (INBOUND, ENFORCE, MASK),
    ),
)
def test_each_direction_takes_exactly_one_action_per_mode(
    direction: str, mode: str, action: str
) -> None:
    assert decide(direction, mode) == action


def test_a_local_tool_is_reported_by_default() -> None:
    assert LOCAL_TOOL in policy.DIRECTIONS
    assert policy.DEFAULT_MODES[LOCAL_TOOL] == WARN
    assert policy.REWRITABLE[LOCAL_TOOL] is False


def test_observe_never_acts_where_acting_is_possible() -> None:
    for direction in policy.DIRECTIONS:
        if direction == policy.MODEL_OUTPUT:
            continue
        assert decide(direction, OBSERVE) == ALLOW


def test_model_output_is_a_direction_that_can_only_be_counted() -> None:
    assert policy.MODEL_OUTPUT in policy.DIRECTIONS
    assert policy.DEFAULT_MODES[policy.MODEL_OUTPUT] == OBSERVE
    assert policy.REWRITABLE[policy.MODEL_OUTPUT] is False


def test_observing_model_output_is_reporting_it() -> None:
    """Nothing can be done about it, so observing has to mean counting."""
    assert decide(policy.MODEL_OUTPUT, OBSERVE) == REPORT


@pytest.mark.parametrize("mode", (WARN, ENFORCE))
def test_model_output_refuses_a_mode_that_promises_to_act(mode: str) -> None:
    with pytest.raises(ValueError, match="model-output can only observe"):
        decide(policy.MODEL_OUTPUT, mode)


def test_a_stop_event_is_not_a_tool_event() -> None:
    with pytest.raises(ValueError, match="unsupported hook event"):
        policy.direction_for("Stop", "", "claude")


def test_unknown_directions_and_modes_are_refused() -> None:
    with pytest.raises(ValueError, match="unsupported policy direction"):
        decide("sideways", WARN)
    with pytest.raises(ValueError, match="unsupported policy mode"):
        decide(INBOUND, "paranoid")
