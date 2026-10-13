"""Safe command-line root for paper research workflows."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from jev_trading_lab.polymarket.pipeline import observe_markets, replay_fixture
from jev_trading_lab.stocks.slice import observe_public
from jev_trading_lab.stocks.slice import replay_fixture as replay_stocks

app = typer.Typer(
    help="Jev Trading Lab paper research tools.",
    no_args_is_help=True,
)
polymarket = typer.Typer(
    help="Offline Polymarket research tools.", no_args_is_help=True
)
app.add_typer(polymarket, name="polymarket")
stocks = typer.Typer(help="Paper-only stock observation tools.", no_args_is_help=True)
app.add_typer(stocks, name="stocks")


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


@stocks.command("replay")
def stocks_replay(
    fixture: Annotated[
        Path, typer.Option("--fixture", help="Offline stock observation fixture.")
    ],
    output_dir: Annotated[
        Path, typer.Option("--output-dir", help="Append-only paper result directory.")
    ],
) -> None:
    """Replay one offline, paper-only stock observation."""

    try:
        summary = replay_stocks(fixture, output_dir)
    except Exception:
        typer.echo("stocks replay failed: invalid input or artifact state", err=True)
        raise typer.Exit(code=1) from None
    typer.echo(json.dumps(summary, sort_keys=True, separators=(",", ":")))


@stocks.command("observe")
def stocks_observe(
    output_dir: Annotated[
        Path, typer.Option("--output-dir", help="Append-only paper result directory.")
    ],
    symbols: Annotated[
        str, typer.Option("--symbols", help="Comma-separated subset of AAPL,MSFT,NVDA.")
    ] = "AAPL,MSFT,NVDA",
) -> None:
    """Observe one public Yahoo snapshot and simulate paper fills."""

    try:
        summary = observe_public(symbols, output_dir)
    except Exception:
        typer.echo("stocks observe failed: public data or artifact error", err=True)
        raise typer.Exit(code=1) from None
    typer.echo(json.dumps(summary, sort_keys=True, separators=(",", ":")))


def main() -> None:
    """Run the paper research command-line application."""
    app()


if __name__ == "__main__":
    main()
