"""CLI entry point. Thin: parses, calls app.run_batch, renders, exits."""

from __future__ import annotations

import contextlib
import sys
from typing import Optional

import typer

from xdl import __version__

app = typer.Typer(
    name="xdl",
    help="Private, local batch downloader for X (Twitter) video posts.",
    no_args_is_help=True,
    add_completion=False,
)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"xdl {__version__}")
        raise typer.Exit


@app.callback()
def _root(
    version: Optional[bool] = typer.Option(  # noqa: UP045 (typer needs Optional)
        None,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="Print version and exit.",
    ),
) -> None:
    """xdl — download X (Twitter) videos locally."""


def main() -> None:
    # UTF-8 output guard for legacy Windows consoles (docs/technical/07).
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            with contextlib.suppress(OSError, ValueError):
                stream.reconfigure(encoding="utf-8")
    app()


if __name__ == "__main__":
    main()
