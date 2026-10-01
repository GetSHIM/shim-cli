import subprocess
from enum import Enum
from importlib import metadata
from pathlib import Path
from typing import Annotated

import typer

import shim_cli
from shim_cli import __version__
from shim_cli.guard import ENTITY_TYPES


class _StringEnum(str, Enum):
    def __str__(self) -> str:
        return str.__str__(self)

    __format__ = str.__format__  # type: ignore[assignment]


class Client(_StringEnum):
    CLAUDE = "claude"
    CODEX = "codex"
    COPILOT = "copilot"


Entity = _StringEnum("Entity", {name: name for name in ENTITY_TYPES})


app = typer.Typer(
    name="shim",
    add_completion=False,
    help="Local traffic visibility and privacy controls for coding agents.",
    no_args_is_help=False,
)


@app.callback(invoke_without_command=True)
def root(
    context: typer.Context,
    version: bool = typer.Option(
        False, "--version", help="Show the version and exit.", is_eager=True
    ),
) -> None:
    if version:
        typer.echo(f"shim {__version__}")
        raise typer.Exit
    if shim_cli.WINDOWS and context.invoked_subcommand not in (None, "help", "update"):
        from shim_cli.cli.output import emit

        emit(
            "FAIL",
            "shim-cli does not support Windows yet. Run it inside WSL, or on macOS "
            "or Linux.",
            error=True,
        )
        raise typer.Exit(2)
    if context.invoked_subcommand is None:
        typer.echo(
            "shim — local traffic visibility and privacy controls for coding agents. Try: shim help"
        )


@app.command()
def help(context: typer.Context) -> None:
    """Show help."""
    typer.echo(context.find_root().get_help())


@app.command()
def update() -> None:
    """Update shim."""
    installer = (metadata.distribution("shim").read_text("INSTALLER") or "").strip()
    command = {
        "uv": ("uv", "tool", "upgrade", "shim"),
        "pip": ("pipx", "upgrade", "shim"),
    }.get(installer)
    if command is not None:
        try:
            raise typer.Exit(subprocess.run(command, check=False).returncode)
        except OSError:
            pass
    typer.echo(
        "Unable to update automatically. Run `uv tool upgrade shim` or "
        "`pipx upgrade shim`.",
        err=True,
    )
    raise typer.Exit(2)


@app.command()
def demo(
    client: Annotated[Client, typer.Argument(case_sensitive=True, show_choices=True)],
    json_output: bool = typer.Option(False, "--json", help="Write a JSON result."),
) -> None:
    """Run a synthetic detector check."""
    from shim_cli.cli.privacy import demo as run_demo

    run_demo(client=client.value, as_json=json_output)


@app.command()
def scan(
    json_output: bool = typer.Option(False, "--json", help="Write a JSON result."),
) -> None:
    """Scan UTF-8 stdin."""
    from shim_cli.cli.privacy import scan as run_scan

    run_scan(as_json=json_output)


@app.command()
def redact(
    json_output: bool = typer.Option(False, "--json", help="Write a JSON result."),
) -> None:
    """Redact UTF-8 stdin."""
    from shim_cli.cli.privacy import redact as run_redact

    run_redact(as_json=json_output)


@app.command("config")
def config_command(
    only: Annotated[
        list[Entity] | None,  # ty: ignore[invalid-type-form]
        typer.Option(
            "--only",
            case_sensitive=False,
            metavar="ENTITY",
            help="Enable only this entity; repeatable.",
        ),
    ] = None,
    enable: Annotated[
        list[Entity] | None,  # ty: ignore[invalid-type-form]
        typer.Option(
            "--enable",
            case_sensitive=False,
            metavar="ENTITY",
            help="Enable this entity; repeatable.",
        ),
    ] = None,
    disable: Annotated[
        list[Entity] | None,  # ty: ignore[invalid-type-form]
        typer.Option(
            "--disable",
            case_sensitive=False,
            metavar="ENTITY",
            help="Disable this entity; repeatable.",
        ),
    ] = None,
    ledger: Annotated[
        bool | None,
        typer.Option(
            "--ledger/--no-ledger",
            help="Keep session records past the end of the session. Off by default.",
        ),
    ] = None,
    diet: Annotated[
        bool | None,
        typer.Option(
            "--diet/--no-diet",
            help="Shrink tool results losslessly. Name individual transforms "
            "in the config file.",
        ),
    ] = None,
    custom: Annotated[
        list[str] | None,
        typer.Option(
            "--custom",
            metavar="NAME=PATTERN",
            help="Add or replace a named regular expression; repeatable.",
        ),
    ] = None,
    custom_literal: Annotated[
        list[str] | None,
        typer.Option(
            "--custom-literal",
            metavar="NAME=TEXT",
            help="Add or replace a named literal; repeatable.",
        ),
    ] = None,
    remove_custom: Annotated[
        list[str] | None,
        typer.Option(
            "--remove-custom",
            metavar="NAME",
            help="Remove a named pattern; repeatable.",
        ),
    ] = None,
    reveal: Annotated[
        list[str] | None,
        typer.Option(
            "--reveal",
            metavar="ENTITY=N",
            help="Keep the last N digits of IBAN, CREDIT_CARD or PHONE; repeatable.",
        ),
    ] = None,
    no_reveal: Annotated[
        list[str] | None,
        typer.Option(
            "--no-reveal",
            metavar="ENTITY",
            help="Stop revealing a tail for this entity; repeatable.",
        ),
    ] = None,
    reset: bool = typer.Option(False, "--reset", help="Restore all defaults."),
    yes: bool = typer.Option(False, "--yes", help="Apply without confirmation."),
    json_output: bool = typer.Option(False, "--json", help="Write a JSON result."),
) -> None:
    """Show or change detection settings."""
    from shim_cli.cli.configuration import configure

    configure(
        only=tuple(entity.value for entity in only or ()),
        enable=tuple(entity.value for entity in enable or ()),
        disable=tuple(entity.value for entity in disable or ()),
        reset=reset,
        ledger=ledger,
        diet=diet,
        custom=tuple(custom or ()),
        custom_literal=tuple(custom_literal or ()),
        remove_custom=tuple(remove_custom or ()),
        reveal=tuple(reveal or ()),
        no_reveal=tuple(no_reveal or ()),
        yes=yes,
        as_json=json_output,
    )


ledger_app = typer.Typer(help="Manage the opt-in record kept past a session.")
app.add_typer(ledger_app, name="ledger")


@ledger_app.command("show")
def ledger_show(
    json_output: bool = typer.Option(False, "--json", help="Write a JSON result."),
) -> None:
    """Show the retained session records."""
    from shim_cli.cli.report import show_ledger

    show_ledger(as_json=json_output)


@ledger_app.command("purge")
def ledger_purge(
    yes: bool = typer.Option(False, "--yes", help="Delete without confirmation."),
    json_output: bool = typer.Option(False, "--json", help="Write a JSON result."),
) -> None:
    """Delete retained session records."""
    from shim_cli.cli.report import purge as run_purge

    run_purge(yes=yes, as_json=json_output)


@app.command()
def report(
    json_output: bool = typer.Option(False, "--json", help="Write a JSON result."),
) -> None:
    """Show the latest session report."""
    from shim_cli.cli.report import report as run_report

    run_report(as_json=json_output)


@app.command()
def audit(
    since: str | None = typer.Option(
        None, "--since", metavar="YYYY-MM-DD", help="Skip records before this date."
    ),
    project: Annotated[
        Path | None,
        typer.Option("--project", help="Only sessions run in this folder or below it."),
    ] = None,
    json_output: bool = typer.Option(False, "--json", help="Write a JSON result."),
    purge: bool = typer.Option(
        False,
        "--purge",
        help="Delete the sessions that sent something, after a typed confirmation.",
    ),
) -> None:
    """Count what Claude Code sessions have already sent to the model."""
    from shim_cli.cli.audit import audit as run_audit

    run_audit(since=since, project=project, as_json=json_output, purge=purge)


@app.command(
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True}
)
def watch(
    context: typer.Context,
    json_output: bool = typer.Option(False, "--json", help="Write a JSON result."),
) -> None:
    """Run a client through the measuring proxy.

    The client and its own arguments follow `--`, so shim does not try to read
    them:

        shim watch -- claude
        shim watch -- claude -p "explain this repo"

    Claude Code only. Codex reads its endpoint from its own configuration, so
    the proxy would be bypassed and the session measured as empty; the Codex
    prompt hook is unaffected. The session runs normally and nothing is
    modified: the report prints when the client exits.
    """
    from shim_cli.cli.watch import watch as run_watch

    run_watch(command=tuple(context.args), as_json=json_output)


@app.command()
def install(
    client: Annotated[Client, typer.Argument(case_sensitive=True, show_choices=True)],
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview without writing."),
    yes: bool = typer.Option(False, "--yes", help="Apply without confirmation."),
) -> None:
    """Preview or install a client hook."""
    from shim_cli.cli.integrations import install as run_install

    run_install(client=client.value, dry_run=dry_run, yes=yes)


@app.command()
def status(
    client: Annotated[Client, typer.Argument(case_sensitive=True, show_choices=True)],
    json_output: bool = typer.Option(False, "--json", help="Write a JSON result."),
) -> None:
    """Show hook status."""
    from shim_cli.cli.integrations import status as run_status

    run_status(client=client.value, as_json=json_output)


@app.command()
def doctor(
    client: Annotated[Client, typer.Argument(case_sensitive=True, show_choices=True)],
    json_output: bool = typer.Option(False, "--json", help="Write a JSON result."),
) -> None:
    """Check client and hook health."""
    from shim_cli.cli.diagnostics import doctor as run_doctor

    run_doctor(client=client.value, as_json=json_output)


@app.command()
def revert(
    client: Annotated[Client, typer.Argument(case_sensitive=True, show_choices=True)],
    yes: bool = typer.Option(False, "--yes", help="Apply without confirmation."),
) -> None:
    """Remove shim's client hook."""
    from shim_cli.cli.integrations import revert as run_revert

    run_revert(client=client.value, yes=yes)


def main() -> None:
    app()
