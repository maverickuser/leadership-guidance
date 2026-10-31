"""Command-line entry point. Stage commands are added in later PRs."""

from __future__ import annotations

import typer

from . import __version__

app = typer.Typer(add_completion=False, help="Archive Scarlet Ink posts you subscribe to as JSON.")


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    version: bool = typer.Option(False, "--version", help="Show the version and exit."),
) -> None:
    if version:
        typer.echo(__version__)
        raise typer.Exit()
    if ctx.invoked_subcommand is None:
        typer.echo(ctx.get_help())
