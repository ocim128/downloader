"""Neutral progress events shared by the CLI renderer and the web UI.

The orchestrator emits these; frontends subscribe and render. Shape is part of
the cross-lane contract (docs/04 "ProgressEvent dataclass stream") — deviations
must be recorded in DECISIONS.md.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class EventKind(StrEnum):
    batch_start = "batch_start"
    resolve_start = "resolve_start"
    resolve_done = "resolve_done"
    download_start = "download_start"
    download_progress = "download_progress"
    download_done = "download_done"
    item_done = "item_done"
    batch_done = "batch_done"


@dataclass(frozen=True)
class ProgressEvent:
    kind: EventKind
    tweet_id: str | None = None
    media_index: int | None = None
    path: str | None = None  # final destination (download_start/done)
    offset: int = 0  # cumulative bytes transferred (download_progress)
    total: int | None = None  # total bytes; None = unknown length
    status: str | None = None  # item status on item_done (ok|skipped|partial|failed)
    detail: str | None = None
    counters: dict[str, int] = field(default_factory=dict)  # batch_* tallies
