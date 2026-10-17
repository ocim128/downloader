"""rich-based rendering: progress bars during the run, summary table after.

Subscribes to the orchestrator's neutral ProgressEvents — the same stream the
web UI will consume. No color/TTY -> rich auto-degrades to plain lines.
"""

from __future__ import annotations

import sys
from typing import IO

from rich.console import Console
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    SpinnerColumn,
    TaskID,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)
from rich.table import Table

from xdl.models import BatchReport, ItemResult
from xdl.report.events import EventKind, ProgressEvent

_STATUS_STYLE = {
    "ok": "green",
    "skipped": "cyan",
    "partial": "yellow",
    "failed": "red",
}


class ConsoleReporter:
    def __init__(self, console: Console | None = None) -> None:
        self.console = console or Console()
        self._bars: dict[tuple[str, int], TaskID] = {}
        self._progress = Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            DownloadColumn(),
            TransferSpeedColumn(),
            TimeRemainingColumn(),
            console=self.console,
            disable=not self.console.is_terminal,
        )

    def __enter__(self) -> ConsoleReporter:
        self._progress.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._progress.stop()

    def handle(self, event: ProgressEvent) -> None:
        key = (event.tweet_id or "", event.media_index or 0)
        if event.kind is EventKind.download_start:
            label = f"[{(event.tweet_id and event.tweet_id[-6:]) or '?'}"
            if (event.media_index or 1) > 1:
                label += f" #{event.media_index}"
            label += "]"
            self._bars[key] = self._progress.add_task(label, total=event.total)
        elif event.kind is EventKind.download_progress:
            task_id = self._bars.get(key)
            if task_id is not None:
                self._progress.update(task_id, completed=event.offset)
                if event.total:
                    self._progress.update(task_id, total=event.total)
        elif event.kind is EventKind.download_done:
            task_id = self._bars.get(key)
            if task_id is not None:
                if event.status == "ok":
                    task = self._progress.tasks[task_id]
                    self._progress.update(task_id, completed=task.total or 1,
                                          total=task.total or 1)
                else:
                    self._progress.update(task_id, description=f"[red]{event.detail}")

    def summary(self, report: BatchReport) -> None:
        table = Table(title="Results", title_justify="left")
        table.add_column("#", justify="right", style="dim")
        table.add_column("id", style="dim")
        table.add_column("author")
        table.add_column("status")
        table.add_column("detail", overflow="fold")
        for i, result in enumerate(report.results, start=1):
            table.add_row(
                str(i),
                _short_id(result),
                _author(result),
                f"[{_STATUS_STYLE.get(result.status, 'white')}]{result.status}[/]",
                result.detail,
            )
        self.console.print(table)
        self.console.print(
            f"  ok {report.ok} · skipped {report.skipped} · "
            f"partial {report.partial} · failed {report.failed} · "
            f"exit {report.exit_code}"
        )
        if report.failed or report.partial:
            self.console.print("  [dim]next: xdl retry-failed[/dim]")


def _short_id(result: ItemResult) -> str:
    tid = result.ref.tweet_id
    return f"…{tid[-4:]}" if len(tid) > 4 else tid


def _author(result: ItemResult) -> str:
    return result.ref.screen_name or "?"


def print_no_tty_fallback(report: BatchReport, stream: IO[str] | None = None) -> None:
    """Plain per-item lines for piped output (CI-friendly)."""
    stream = stream or sys.stdout
    for i, result in enumerate(report.results, start=1):
        stream.write(
            f"{i}\t{_short_id(result)}\t{_author(result)}\t{result.status}\t{result.detail}\n"
        )
    stream.write(
        f"ok {report.ok} · skipped {report.skipped} · partial {report.partial} · "
        f"failed {report.failed} · exit {report.exit_code}\n"
    )
