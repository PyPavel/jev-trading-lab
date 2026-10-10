"""Safe command-line root for paper research workflows."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from jev_trading_lab.polymarket.pipeline import observe_markets, replay_fixture

app = typer.Typer(
    help="Jev Trading Lab paper research tools.",
    no_args_is_help=True,
)
polymarket = typer.Typer(
    help="Offline Polymarket research tools.", no_args_is_help=True
)
app.add_typer(polymarket, name="polymarket")


@app.callback()
def research() -> None:
    """Access paper research workflows."""


@polymarket.command("replay")
def replay(
    fixture: Annotated[
        Path,
        typer.Option("--fixture", help="Strict canned feasibility fixture."),
    ],
    output_dir: Annotated[
        Path,
        typer.Option("--output-dir", help="Private replay artifact directory."),
    ],
) -> None:
    """Replay a canned fixture without network access or credentials."""

    try:
        summary = replay_fixture(fixture, output_dir)
    except Exception:
        typer.echo("replay failed: invalid input or artifact state", err=True)
        raise typer.Exit(code=1) from None
    typer.echo(json.dumps(summary, sort_keys=True, separators=(",", ":")))


@polymarket.command("observe")
def observe(
    output_dir: Annotated[
        Path,
        typer.Option("--output-dir", help="Private paper observation directory."),
    ],
    limit: Annotated[
        int,
        typer.Option("--limit", min=1, max=20, help="Maximum candidate observations."),
    ] = 1,
) -> None:
    """Run one bounded public-data and model observation pass; never place orders."""

    try:
        summaries = observe_markets(limit=limit, output_dir=output_dir)
    except Exception:
        typer.echo("observe failed: bounded polling or artifact state error", err=True)
        raise typer.Exit(code=1) from None
    typer.echo(json.dumps(summaries, sort_keys=True, separators=(",", ":")))


def main() -> None:
    """Run the paper research command-line application."""
    app()


if __name__ == "__main__":
    main()
