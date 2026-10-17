"""CLI entry point. Thin: parses, calls app.run_batch, renders, exits.

Surface per docs/technical/07: `xdl [OPTIONS] [URLS]...` as the default
command (implemented as the typer callback with invoke_without_command), plus
subcommands retry-failed (db/config/serve land with their milestones).
"""

from __future__ import annotations

import contextlib
import sys
from pathlib import Path
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
def _root() -> None:
    """xdl — download X (Twitter) videos locally.

    Default surface: xdl [OPTIONS] URLS... (see xdl run --help via xdl --help).
    """


# A single-command typer app collapses to top level: `xdl [OPTIONS] URLS...`
# with normal click option interspersion (`xdl URL -o DIR` works). Subcommands
# live on `app` and are dispatched from main() when argv[0] names one.
solo = typer.Typer(help="Download videos from one or many X post URLs.")


@solo.command()
def default(
    urls: Optional[list[str]] = typer.Argument(None, help="One or more post URLs."),  # noqa: UP045
    file: Optional[Path] = typer.Option(  # noqa: UP045
        None,
        "--file",
        "-f",
        help="Batch file: one URL per line, '#'-comments, blanks ok ('-' = stdin).",
    ),
    id_mode: bool = typer.Option(False, "--id", help="Treat bare numeric arguments as tweet ids."),
    output: Optional[Path] = typer.Option(  # noqa: UP045
        None, "--output", "-o", help="Downloads root (default ./downloads)."
    ),
    quality: Optional[str] = typer.Option(  # noqa: UP045
        None, help="'best' | 'worst' | max height number (e.g. 720)."
    ),
    no_meta: bool = typer.Option(False, help="Skip sidecar JSON files."),
    force: bool = typer.Option(False, help="Re-download even if state says complete."),
    concurrency: Optional[int] = typer.Option(  # noqa: UP045
        None, "--concurrency", "-c", min=1, help="Parallel file downloads [default: 3]."
    ),
    resolve_concurrency: Optional[int] = typer.Option(  # noqa: UP045
        None, min=1, help="Parallel metadata lookups [default: 4]."
    ),
    delay: Optional[float] = typer.Option(  # noqa: UP045
        None, min=0.0, help="Fixed delay between resolves [default: jittered 0.2-0.5s]."
    ),
    retries: Optional[int] = typer.Option(  # noqa: UP045
        None, min=1, help="Max attempts per item [default: 5]."
    ),
    fallback: Optional[str] = typer.Option(  # noqa: UP045
        None, help="Resolver fallback: 'auto' | 'ytdlp' | 'fxtwitter' | 'none'."
    ),
    dry_run: bool = typer.Option(False, help="Resolve + print planned paths, download nothing."),
    verbose: int = typer.Option(0, "--verbose", "-v", count=True, help="-v info, -vv debug."),
    version: Optional[bool] = typer.Option(  # noqa: UP045
        None,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="Print version and exit.",
    ),
) -> None:
    """xdl — download X (Twitter) videos locally."""
    _execute(
        urls=urls,
        file=file,
        id_mode=id_mode,
        output=output,
        quality=quality,
        no_meta=no_meta,
        force=force,
        concurrency=concurrency,
        resolve_concurrency=resolve_concurrency,
        delay=delay,
        retries=retries,
        fallback=fallback,
        dry_run=dry_run,
        verbose=verbose,
    )


def _execute(
    *,
    urls: list[str] | None,
    file: Path | None,
    id_mode: bool,
    output: Path | None,
    quality: str | None,
    no_meta: bool,
    force: bool,
    concurrency: int | None,
    resolve_concurrency: int | None,
    delay: float | None,
    retries: int | None,
    fallback: str | None,
    dry_run: bool,
    verbose: int,
    only_failed: bool = False,
) -> None:
    import asyncio

    from rich.console import Console

    from xdl.app import run_batch
    from xdl.config import Settings
    from xdl.models import BatchRequest
    from xdl.report.render import ConsoleReporter, print_no_tty_fallback

    overrides = _overrides(output, concurrency, fallback)
    base = Settings.load(overrides)
    origin = "cli"
    if only_failed:
        from xdl.storage.state import StateStore

        state = StateStore(base.db_path)
        try:
            refs = state.failed_items()
        finally:
            state.close()
        if not refs:
            typer.echo("nothing to retry — no failed/incomplete items in state")
            raise typer.Exit(code=0)
        inputs = [r.canonical_url for r in refs]
        origin = "retry"
    else:
        inputs = _collect_inputs(urls, file)
        if not inputs:
            typer.secho(
                "no input URLs (pass URLs, -f FILE, or '-' for stdin)",
                fg=typer.colors.YELLOW,
                err=True,
            )
            raise typer.Exit(code=2)

    settings = Settings.load(overrides)
    if resolve_concurrency is not None:
        settings.engine.resolve_concurrency = resolve_concurrency
    if delay is not None:
        settings.engine.delay = delay
    if retries is not None:
        settings.engine.retries = retries

    request = BatchRequest(
        inputs=inputs,
        settings=settings,
        origin=origin,
        id_mode=id_mode,
        force=force,
        no_meta=no_meta,
        quality=quality,
        dry_run=dry_run,
    )

    console = Console()
    with ConsoleReporter(console) as reporter:
        report = asyncio.run(run_batch(request, on_event=reporter.handle))
    if not console.is_terminal:
        print_no_tty_fallback(report)
    else:
        reporter.summary(report)
    raise typer.Exit(code=report.exit_code)


def _overrides(
    output: Path | None,
    concurrency: int | None,
    fallback: str | None,
) -> dict[str, object]:
    overrides: dict[str, object] = {}
    if output is not None:
        overrides["storage.root"] = output
    if concurrency is not None:
        overrides["engine.concurrency"] = concurrency
    if fallback is not None:
        overrides["resolver.fallback"] = fallback
    return overrides


def _collect_inputs(
    urls: list[str] | None,
    file: Path | None,
) -> list[str]:
    inputs = [u for u in (urls or []) if u and u.strip()]
    if file is not None:
        if str(file) == "-":
            inputs.extend(sys.stdin.read().splitlines())
        elif file.is_file():
            inputs.extend(file.read_text(encoding="utf-8", errors="replace").splitlines())
        else:
            typer.secho(f"input file not found: {file}", fg=typer.colors.RED, err=True)
            raise typer.Exit(code=2)
    cleaned = []
    for line in inputs:
        line = line.strip()
        if line and not line.startswith("#"):  # comments + blanks tolerated
            cleaned.append(line)
    return cleaned


@app.command(name="retry-failed")
def retry_failed(
    output: Optional[Path] = typer.Option(  # noqa: UP045
        None, "--output", "-o", help="Downloads root (must match the original run)."
    ),
    quality: Optional[str] = typer.Option(None, help="'best' | 'worst' | height."),  # noqa: UP045
    no_meta: bool = typer.Option(False, help="Skip sidecar JSON files."),
    force: bool = typer.Option(False, help="Re-download even if complete."),
    concurrency: Optional[int] = typer.Option(  # noqa: UP045
        None, "--concurrency", "-c", min=1, help="Parallel file downloads [default: 3]."
    ),
    verbose: int = typer.Option(0, "--verbose", "-v", count=True, help="-v info, -vv debug."),
) -> None:
    """Re-run items whose state status is failed/incomplete."""
    _execute(
        urls=None,
        file=None,
        id_mode=False,
        output=output,
        quality=quality,
        no_meta=no_meta,
        force=force,
        concurrency=concurrency,
        resolve_concurrency=None,
        delay=None,
        retries=None,
        fallback=None,
        dry_run=False,
        verbose=verbose,
        only_failed=True,
    )


def main() -> None:
    # UTF-8 output guard for legacy Windows consoles (docs/technical/07).
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            with contextlib.suppress(OSError, ValueError):
                stream.reconfigure(encoding="utf-8")
    args = sys.argv[1:]
    subcommands = {c.name for c in app.registered_commands if c.name}
    if not args or args[0] in subcommands or args[0] in ("--help", "-h"):
        app()  # group: bare/help/subcommand surfaces
    else:
        solo()  # default command: xdl [OPTIONS] URLS...


if __name__ == "__main__":
    main()
