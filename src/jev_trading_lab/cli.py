"""Safe command-line root for paper research workflows."""

import typer

app = typer.Typer(
    help="Jev Trading Lab paper research tools. No real-money execution is available.",
    no_args_is_help=True,
)


@app.callback()
def research() -> None:
    """Access paper research workflows."""


def main() -> None:
    """Run the paper research command-line application."""
    app()


if __name__ == "__main__":
    main()
