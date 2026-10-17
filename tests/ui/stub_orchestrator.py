"""Stub orchestrator implementing the DOCUMENTED contract in
docs/04-project-structure.md (`run_batch(BatchRequest) -> BatchReport` +
`ProgressEvent`s) so `tests/ui/` runs hermetically against the frozen shapes.

Behavior: deterministic, sqlite-free, no subprocesses, no network.
- each input resolves to a synthetic tweet id (hash of the input line),
- every 10th item "fails" (batch_id+idx) % 10 == 9, the rest download "ok";
- events flow in the same order as the real orchestrator (resolve, download
  ticks, item_done, batch_done with counters);
- dry_run marks items "skipped" instead of downloading;
- `analyze` returns resolver-shaped cards without any download step.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from xdl.models import (
    BatchReport,
    BatchRequest,
    ErrorCode,
    ItemResult,
    MediaOutcome,
    TweetRef,
    XdlError,
)
from xdl.report.events import EventKind, ProgressEvent

EventFn = Callable[[ProgressEvent], None]


@dataclass
class StubOrchestrator:
    """The documented `run_batch` contract, in miniature.

    Round 2: happy-path endpoint tests moved to the real core
    (`tests/ui/real_core.py`). The stub remains ONLY for the fault-injection
    cases that cannot be asserted reliably against an instantaneous mock core
    (second-job 409 while running, deterministic SSE tick order, mid-batch
    crash, 429-style total failure) — see test_serve.py fault section.
    """

    tick: float = 0.01  # seconds between synthetic progress ticks
    fail_code: str | None = None  # if set, EVERY item fails with this code
    crash_after_resolve: bool = False  # raise mid-batch on the first item

    def __post_init__(self) -> None:
        self.batches: list[dict[str, Any]] = []
        self.counter = 0

    async def run_batch(
        self,
        req: BatchRequest,
        *,
        on_event: EventFn | None = None,
        **kwargs: Any,
    ) -> BatchReport:
        """Full pipeline shape: normalize -> dedupe -> resolve -> download."""
        inputs: list[str] = []
        for raw in req.inputs:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line not in inputs:
                inputs.append(line)

        self.counter += 1
        batch_id = self.counter
        row: dict[str, Any] = {
            "batch_id": batch_id,
            "origin": req.origin,
            "total": len(inputs),
            "ok": 0, "skipped": 0, "partial": 0, "failed": 0,
            "status": "running",
            "started_at": time.time(),
        }
        self.batches.append(row)

        def emit(kind: EventKind, **kw: Any) -> None:
            if on_event is not None:
                on_event(ProgressEvent(kind=kind, **kw))

        await asyncio.sleep(self.tick)
        emit(EventKind.batch_start, counters={"total": len(inputs)})

        results: list[ItemResult] = []
        for idx, raw in enumerate(inputs):
            if self.crash_after_resolve and idx == 0:
                # Fault injection: die mid-batch, after the first resolve.
                raise RuntimeError("simulated mid-batch crash")
            tweet_id = _synthetic_id(raw)
            ref = TweetRef(
                tweet_id=tweet_id,
                screen_name=f"user{idx}",
                canonical_url=f"https://x.com/user{idx}/status/{tweet_id}",
            )
            emit(EventKind.resolve_start, tweet_id=tweet_id)
            await asyncio.sleep(self.tick)
            emit(EventKind.resolve_done, tweet_id=tweet_id,
                 detail=ref.canonical_url)

            if self.fail_code is not None:
                fail = True
                error = XdlError(ErrorCode(self.fail_code), "injected failure (stub)")
            else:
                fail = (batch_id + idx) % 10 == 9 and not req.dry_run
                error = (
                    XdlError(ErrorCode.HLS_FAILED, "intentional stub failure")
                    if fail
                    else None
                )
            outcome_status: str = "failed" if fail else ("skipped" if req.dry_run else "ok")

            if outcome_status != "skipped":
                emit(EventKind.download_start, tweet_id=tweet_id, media_index=1,
                     path=f"downloads/{tweet_id}.mp4")
                for step in range(1, 5):
                    await asyncio.sleep(self.tick)
                    emit(EventKind.download_progress, tweet_id=tweet_id,
                         media_index=1, offset=step * 250, total=1_000)
                emit(EventKind.download_done, tweet_id=tweet_id, media_index=1,
                     status=outcome_status,
                     detail=error.detail if fail else "1000 bytes")

            outcome = MediaOutcome(
                index=1,
                status=outcome_status,
                file=None if outcome_status != "ok" else Path("downloads") / f"{tweet_id}.mp4",
                error=error,
            )
            item = ItemResult(
                ref=ref,
                status=outcome_status,
                media=[outcome],
                error=outcome.error,
                files=[outcome.file] if outcome.file else [],
                detail=outcome.error.detail if fail else "stub download",
            )
            results.append(item)
            emit(EventKind.item_done, tweet_id=tweet_id, status=item.status,
                 detail=item.detail)

        report = BatchReport(
            results=results, exit_code=0 if not results or not any(
                r.status == "failed" for r in results) else 1)
        counts = report.ok, report.skipped, report.partial, report.failed
        emit(EventKind.batch_done, counters={"ok": counts[0], "skipped": counts[1],
                                             "partial": counts[2], "failed": counts[3]})

        row.update(ok=report.ok, skipped=report.skipped, partial=report.partial,
                   failed=report.failed, status="done")
        return report

    async def analyze(self, urls: list[str]) -> list[dict[str, Any]]:
        """Flow-B card data: resolve-shaped, no downloads."""
        cards: list[dict[str, Any]] = []
        for idx, raw in enumerate(urls):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            tweet_id = _synthetic_id(line)
            cards.append({
                "input": line,
                "tweet_id": tweet_id,
                "author": f"user{idx}",
                "text": f"stub post {tweet_id}: a short one-line snippet",
                "poster": f"https://pbs.twimg.com/media/{tweet_id}.jpg",
                "canonical_url": f"https://x.com/user{idx}/status/{tweet_id}",
                "formats": ["Best MP4", "720p", "480p", "MP3"],
                "media_kinds": ["video"],
            })
        return cards

    async def list_batches(self, limit: int = 50) -> list[dict[str, Any]]:
        """GET /jobs backing (newest first)."""
        return self.batches[-limit:][::-1]

    async def item_files(self, tweet_id: str) -> list[str]:
        """reveal backing: ok file paths for a tweet."""
        tweet_id = _synthetic_id(tweet_id)
        return [str(Path("downloads") / f"{tweet_id}.mp4")]


def _synthetic_id(raw: str) -> str:
    digest = hashlib.sha256(raw.encode()).hexdigest()
    return str(int(digest[:18], 16))
