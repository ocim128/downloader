"""CLI entry point. Thin: parses, calls app.run_batch, renders, exits.

Surface per docs/technical/07: `xdl [OPTIONS] [URLS]...` as the default
command (implemented as the typer callback with invoke_without_command), plus
subcommands retry-failed / serve / config. Ctrl+C: first press drains the
batch gracefully (summary, exit 130); second press hard-exits (the `.part`
invariant makes that safe).
"""

from __future__ import annotations

import contextlib
import os
import signal
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Optional

import typer

from xdl import __version__

if TYPE_CHECKING:
    from xdl.config import Settings
    from xdl.models import CancelToken

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
    audio_only: bool = typer.Option(
        "--audio-only",
        help=(
            "Extract MP3 instead of keeping the video (ffmpeg -q:a 0 = V0, the "
            "best-possible VBR transcode; the source audio is already lossy AAC "
            "— best possible, not lossless)."
        ),
    ),
    gif: bool = typer.Option(
        "--gif",
        help=("Convert X's mp4-as-gif to a real GIF (ffmpeg palettegen/paletteuse, "
              "15 fps). Output is large."),
    ),
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
    if audio_only and gif:
        typer.secho(
            "use one of --audio-only / --gif, not both", fg=typer.colors.RED, err=True
        )
        raise typer.Exit(code=2)
    _execute(
        urls=urls,
        file=file,
        id_mode=id_mode,
        output=output,
        quality=quality,
        no_meta=no_meta,
        force=force,
        audio_only=audio_only,
        gif=gif,
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
    audio_only: bool = False,
    gif: bool = False,
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
    from xdl.models import BatchRequest, CancelToken
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

    _warn_stale_ytdlp(settings)

    request = BatchRequest(
        inputs=inputs,
        settings=settings,
        origin=origin,
        id_mode=id_mode,
        force=force,
        no_meta=no_meta,
        quality=quality,
        dry_run=dry_run,
        audio_only=audio_only,
        gif=gif,
    )

    token = CancelToken()
    console = Console()
    with _sigint_handler(token), ConsoleReporter(console) as reporter:
        report = asyncio.run(run_batch(request, on_event=reporter.handle, cancel=token))
    if not console.is_terminal:
        print_no_tty_fallback(report)
    else:
        reporter.summary(report)
    if report.cancelled:
        typer.echo(
            "interrupted (Ctrl+C) — partial downloads are resumable; "
            "re-run the command or use xdl retry-failed"
        )
        raise typer.Exit(code=130)
    raise typer.Exit(code=report.exit_code)


def _warn_stale_ytdlp(settings: Settings) -> None:
    """Startup check (docs/technical/02 Adapter D): warn when the installed
    yt-dlp is older than ~14 days — but only when it's actually in the chain."""
    if settings.resolver.fallback not in ("auto", "ytdlp"):
        return
    from xdl.resolve.ytdlp import stale_ytdlp_warning

    if message := stale_ytdlp_warning():
        typer.secho(f"warning: {message}", fg=typer.colors.YELLOW, err=True)


@contextlib.contextmanager
def _sigint_handler(token: CancelToken) -> Iterator[None]:
    """First SIGINT: mark the cancel token (graceful drain, exit 130 after
    the summary). Second SIGINT: immediate hard exit — the `.part` invariant
    makes it safe (docs/technical/03)."""
    count = 0

    def handler(signum: int, frame: object) -> None:
        nonlocal count
        count += 1
        if count >= 2:
            os._exit(130)
        token.cancel()

    try:
        previous = signal.signal(signal.SIGINT, handler)
    except ValueError:
        # Not the main thread (e.g. under some test runners): cancellation
        # still works via the token itself.
        yield
        return
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, previous)


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


@app.command(
    name="serve",
    context_settings={"ignore_unknown_options": True, "allow_extra_args": True},
)
def serve(ctx: typer.Context) -> None:
    """Start the local web UI on 127.0.0.1 (see `python -m xdl.ui --help`)."""
    # Delegation, not duplication: the UI lane owns the runner + its flags
    # (and the loopback-only bind guard lives there, once).
    from xdl.ui.__main__ import main as serve_main

    sys.argv = ["xdl serve", *ctx.args]
    serve_main()


@app.command(name="config")
def config_cmd() -> None:
    """Print the resolved settings (defaults <- config.toml <- env <- CLI)."""
    from xdl.config import Settings

    settings = Settings.load()
    config_file = settings.app_dir / "config.toml"
    typer.echo(f"# config file: {config_file} ({'present' if config_file.is_file() else 'absent'})")
    typer.echo(f"# state db:    {settings.db_path}")
    # Per-value source tracking (file/env/default) lands with the config
    # precedence milestone; today this renders the resolved merge.
    for section, fields in settings.model_dump().items():
        typer.echo(f"[{section}]")
        for key, value in fields.items():
            typer.echo(f"{key} = {value!r}")


def main() -> None:
    # Optional logging hook (B's lane): call setup_logging(settings) at
    # startup once xdl.report.logging ships.
    # TODO(core2): if module xdl.report.logging exists -> wire it here.
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
