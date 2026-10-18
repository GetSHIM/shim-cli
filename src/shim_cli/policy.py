from __future__ import annotations

from dataclasses import dataclass, field

USER_PROMPT = "user-prompt"
OUTBOUND = "outbound"
INBOUND = "inbound"
LOCAL_WRITE = "local-write"
EXECUTABLE_TEXT = "executable-text"
# The input of a tool that acts on this machine: an address, a pattern, a
# question. Rewriting it breaks the call and keeps nothing from leaving.
LOCAL_TOOL = "local-tool"
MODEL_OUTPUT = "model-output"
DIRECTIONS = (
    USER_PROMPT,
    OUTBOUND,
    INBOUND,
    LOCAL_WRITE,
    EXECUTABLE_TEXT,
    LOCAL_TOOL,
    MODEL_OUTPUT,
)

OBSERVE = "observe"
WARN = "warn"
ENFORCE = "enforce"
MODES = (OBSERVE, WARN, ENFORCE)

ALLOW = "allow"
REPORT = "report"
MASK = "mask"
DENY = "deny"

DEFAULT_MODES = {
    USER_PROMPT: WARN,
    OUTBOUND: ENFORCE,
    INBOUND: ENFORCE,
    LOCAL_WRITE: WARN,
    EXECUTABLE_TEXT: WARN,
    LOCAL_TOOL: WARN,
    MODEL_OUTPUT: OBSERVE,
}

REWRITABLE = {
    USER_PROMPT: False,
    OUTBOUND: True,
    INBOUND: True,
    LOCAL_WRITE: False,
    EXECUTABLE_TEXT: False,
    LOCAL_TOOL: False,
    MODEL_OUTPUT: False,
}

# The client has already shown the text; a mode that promises otherwise is a lie.
OBSERVE_ONLY = frozenset({MODEL_OUTPUT, "Stop"})

# Each client's own tool names, by where the tool's input goes. A name not
# here, an MCP tool or one a newer client adds, is outbound and never
# rewritten: shim cannot know where its input goes, so enforce refuses it.
# Claude Code 2.1.286: its tool list, and the names its permission rules group
# as file, shell and web tools. VS Code: Copilot Chat 0.69.0's tool registry,
# which keeps the `read_file` and `run_in_terminal` measured on 1.137.0.
TOOLS = {
    "claude": {
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
    },
    "vscode": {
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
    },
}
# Not a direction: an MCP tool's input goes wherever its server sends it.
MCP = "mcp"
_MCP_PREFIX = {"claude": "mcp__"}
# The tool whose result is what the person typed in answer to the agent.
_ANSWER_TOOL = {"claude": "AskUserQuestion", "vscode": "vscode_askQuestions"}

_PROMPT_EVENTS = frozenset({"UserPromptSubmit", "userPromptTransformed"})
_INPUT_EVENTS = frozenset({"PreToolUse", "preToolUse"})
_RESULT_EVENTS = frozenset(
    {"PostToolUse", "postToolUse", "PostToolUseFailure", "PostToolBatch"}
)


@dataclass(frozen=True)
class Policy:
    entities: tuple
    modes: dict
    tool_entities: dict
    ledger: bool = False
    diet: tuple = ()
    custom: tuple = ()
    reveal: dict = field(default_factory=dict)
    # "report" counts injection markers; "note" also tells the model, beside
    # the tool result, that it holds text that reads as instructions.
    markers: str = "report"

    def mode_for(self, direction: str, tool: str = "", event: str = "") -> str:
        for key in (tool, event, direction):
            if key and key in self.modes:
                return self.modes[key]
        if "default" in self.modes:
            return self.modes["default"]
        return DEFAULT_MODES.get(direction, WARN)

    def chose_mode(self, direction: str, tool: str = "", event: str = "") -> bool:
        """True when a settings file named this mode, rather than a default.

        The shipped default for tool traffic is enforce because masking costs
        the user nothing. Where the client cannot mask at all, enforce means
        refusing every call, so that default is not one a user silently agreed
        to.
        """
        keys = (tool, event, direction, "default")
        return any(key and key in self.modes for key in keys)

    def entities_for(self, tool: str = "", event: str = "") -> tuple:
        for key in (tool, event):
            if key and key in self.tool_entities:
                return self.tool_entities[key]
        return self.entities


def tool_kind(client: str, tool: str) -> str:
    """Where `client`'s tool sends its input: a direction, MCP, or "" for a
    tool shim does not know."""
    kind = TOOLS.get(client, {}).get(tool, "")
    prefix = _MCP_PREFIX.get(client)
    if not kind and prefix and tool.startswith(prefix):
        return MCP
    return kind


def direction_for(event: str, tool: str, client: str) -> str:
    if event in _PROMPT_EVENTS:
        return USER_PROMPT
    if event in _RESULT_EVENTS:
        # An answer the person typed is theirs, not content from elsewhere.
        return LOCAL_TOOL if tool == _ANSWER_TOOL.get(client) else INBOUND
    if event in _INPUT_EVENTS:
        kind = tool_kind(client, tool)
        return OUTBOUND if kind in (MCP, "") else kind
    raise ValueError("unsupported hook event")


# The `shim` subcommands that change or remove what shim enforces. An agent
# that runs one can turn shim off, so the hook refuses it in every mode.
SHIM_CONTROLS = ("config", "revert", "uninstall")
# Words after which the rest of a command still runs as one: the shell's
# reserved words, zsh's precommand modifiers, and `command`, `exec` and `env`
# with their options.
_PREFIXES = frozenset(
    {"!", "{", "coproc", "do", "elif", "else", "if", "then", "time", "until"}
    | {"while", "-", "nocorrect", "noglob", "command", "exec", "env"}
)
# Their options that take the next word as a value: `exec -a NAME`, and env's
# `-u NAME`, `-C DIR`, `-P PATH`, `-S STRING` and GNU long forms, which GNU
# also accepts as any unambiguous prefix. env -S's string is the command
# itself, and is not read.
_VALUED = "aCPSu"
_VALUED_LONG = frozenset(
    name[:end]
    for name in ("--argv0", "--chdir", "--split-string", "--unset")
    for end in range(3, len(name) + 1)
)


def _skip_heredocs(text: str, index: int, delimiters: list) -> int:
    """Past the bodies of the here-documents opened on the line just ended."""
    for delimiter, tabs in delimiters:
        while index < len(text):
            end = text.find("\n", index)
            end = len(text) if end < 0 else end
            line = text[index:end]
            index = end + 1
            if (line.lstrip("\t") if tabs else line) == delimiter:
                break
    delimiters.clear()
    return min(index, len(text))


def _simple_commands(text: str) -> list:
    """The words of each simple command in a POSIX shell command line.

    ponytail: reads quoting, separators, subshells, `$( )`, backticks,
    redirections and here-documents, which is enough to name the program each
    command runs. Aliases, functions, variables, `eval` and `sh -c` are not
    followed; the docs say so.
    """
    commands: list = [[]]
    word: list = []
    started = False
    quote = ""
    closers: list = []  # (closer, quote to resume) for each open (, $( or `
    delimiters: list = []  # here-documents whose bodies start at the next newline
    redirect = ""  # the redirection the next word belongs to
    index = 0

    def finish() -> None:
        nonlocal started, redirect
        if started:
            if redirect in ("<<", "<<-"):
                delimiters.append(("".join(word), redirect == "<<-"))
            elif not redirect:
                commands[-1].append("".join(word))
            redirect = ""
        word.clear()
        started = False

    def separate() -> None:
        nonlocal redirect
        finish()
        redirect = ""
        if commands[-1]:
            commands.append([])

    while index < len(text):
        char = text[index]
        index += 1
        if quote == "'":
            if char == "'":
                quote = ""
            else:
                word.append(char)
            continue
        if char == "\\":
            following = text[index : index + 1]
            index += 1
            if following == "\n":  # a line continuation
                continue
            if quote == '"' and following not in '$`"\\':
                word.append(char)
            word.append(following)
            started = True
            continue
        opens = char == "$" and text[index : index + 1] == "("
        if quote == '"':
            if char == '"':
                quote = ""
            elif opens or char == "`":
                index += opens
                separate()
                closers.append((")" if opens else "`", '"'))
                quote = ""
            else:
                word.append(char)
                started = True
            continue
        if char in "'\"":
            quote = char
            started = True
        elif char in " \t":
            finish()
        elif char == "#" and not started:
            end = text.find("\n", index)
            index = len(text) if end < 0 else end
        elif opens or char == "(":
            index += opens
            separate()
            closers.append((")", ""))
        elif char == ")" or (char == "`" and closers and closers[-1][0] == "`"):
            separate()
            if closers and closers[-1][0] == char:
                quote = closers.pop()[1]
        elif char == "`":
            separate()
            closers.append(("`", ""))
        elif char in ";&|\n":
            separate()
            if char == "\n":
                index = _skip_heredocs(text, index, delimiters)
        elif char in "<>":
            if started and "".join(word).isdigit():  # `2>`: the descriptor
                word.clear()
                started = False
            finish()
            operator = char
            while text[index : index + 1] in ("<", ">"):
                operator += text[index]
                index += 1
            if text[index : index + 1] in ("&", "|", "-"):
                operator += text[index]
                index += 1
            redirect = operator
        else:
            word.append(char)
            started = True
    finish()
    return commands


def _takes_value(option: str) -> bool:
    """Whether a prefix's option takes the next word as its value: a valued
    letter last in its cluster (`-iu NAME`), or a valued long form."""
    if option.startswith("--"):
        return option in _VALUED_LONG
    for index, letter in enumerate(option[1:], 2):
        if letter in _VALUED:
            return index == len(option)
    return False


def _control(words: list) -> str:
    position = 0
    while position < len(words):
        word = words[position]
        name, equals, _value = word.partition("=")
        if equals and name.isidentifier():
            position += 1
        elif word in _PREFIXES:
            position += 1
            while position < len(words) and words[position].startswith("-"):
                position += 2 if _takes_value(words[position]) else 1
        else:
            break
    if position >= len(words):
        return ""
    # zsh runs `=shim` as the shim on PATH, and macOS's default disk finds
    # `SHIM` or `ſhim` as `shim`.
    program = words[position].rsplit("/", 1)[-1].removeprefix("=")
    if program.casefold() != "shim":
        return ""
    arguments = words[position + 1 :]
    # Before its subcommand shim takes only `--version` and `--help`, which
    # exit, and `--`, which ends its options and still runs the subcommand.
    if arguments[:1] == ["--"]:
        arguments = arguments[1:]
    subcommand = arguments[0] if arguments else ""
    return subcommand if subcommand in SHIM_CONTROLS else ""


def shim_control(command: str) -> str:
    """Which of SHIM_CONTROLS a shell command line runs as `shim`, or ""."""
    for words in _simple_commands(command):
        if found := _control(words):
            return found
    return ""


def decide(direction: str, mode: str) -> str:
    if direction not in REWRITABLE:
        raise ValueError("unsupported policy direction")
    if mode not in MODES:
        raise ValueError("unsupported policy mode")
    if direction == MODEL_OUTPUT:
        if mode != OBSERVE:
            raise ValueError("model-output can only observe")
        # Observing is the whole feature here: the count must reach the summary.
        return REPORT

    if mode == OBSERVE:
        return ALLOW
    if mode == WARN or direction == LOCAL_TOOL:
        return REPORT

    if REWRITABLE[direction]:
        return MASK
    return DENY
