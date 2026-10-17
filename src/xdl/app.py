"""Orchestrator: batch lifecycle, stage wiring, skip decisions, report assembly.

The only component frontends talk to (docs/02): `run_batch(BatchRequest) ->
BatchReport` plus the resolve-only `analyze(urls, settings)` helper. Also owns
cross-stage recovery: the engine raises LINK_EXPIRED; app.py — never the
engine — re-resolves and re-dispatches once (layering rule 6).

Resilience (docs/technical/08): per-item retries with exponential backoff +
jitter (interruptible); one worker's 429 pauses ALL workers via ThrottleState
(3x429/60s escalates to >=120 s); a circuit breaker drains the batch after 8
consecutive item failures; Ctrl+C drains gracefully (CancelToken polled at
item/chunk granularity).

Concurrency model: one item-task per input. The two bounds are INDEPENDENT
pools — `resolve_sem` covers only resolve+select (`_prepare_item`), and it is
released before the transfer phase (`_download_item`), which holds only
`download_sem`. So resolves of later items overlap transfers of earlier ones
at full `resolve_concurrency`, and a transfer stalled in a 429 cooldown cannot
occupy a resolve slot. (Earlier versions nested the download inside the
resolve slot, which silently collapsed effective resolve parallelism to
`resolve_concurrency - concurrency`; see DECISIONS.md.)
"""

from __future__ import annotations

import asyncio
import random
import shutil
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypeVar
from urllib.parse import urlsplit

import httpx

from xdl import __version__
from xdl.config import Settings
from xdl.engine.downloader import fetch
from xdl.engine.throttle import ThrottleState, parse_retry_after
from xdl.media import convert, hls
from xdl.media.selector import is_hls, select
from xdl.models import (
    BatchReport,
    BatchRequest,
    CancelToken,
    Chosen,
    DownloadRow,
    ErrorCode,
    ItemResult,
    MediaOutcome,
    QualityPolicy,
    ResolvedTweet,
    TweetRef,
    Variant,
    XdlError,
)
from xdl.report.events import EventKind, ProgressEvent
from xdl.resolve.base import FallbackChain, build_chain
from xdl.storage.naming import render_path
from xdl.storage.sidecar import build_payload, write_sidecar
from xdl.storage.state import StateStore
from xdl.urls import normalize as normalize_url

EventFn = Callable[[ProgressEvent], None]

#: 8 consecutive item failures, 0 successes between -> breaker opens
#: (docs/technical/08). Constant for now — config knob lands with M6.
_CIRCUIT_THRESHOLD = 8
_BACKOFF_BASE = 1.0  # seconds; waits 1, 2, 4, 8, 16 ...
_BACKOFF_CAP = 300.0
_JITTER = (0.75, 1.25)

T = TypeVar("T")


def build_client() -> httpx.AsyncClient:
    # Redirects off: media URLs are final CDN links; a 3xx is a smell
    # (docs/technical/03). The t.co expander is the only sanctioned redirect
    # follower, and it is one-hop.
    return httpx.AsyncClient(
        http2=False,  # MockTransport parity; HTTP/2 flip at integration tuning
        timeout=httpx.Timeout(connect=10, read=60, write=30, pool=15),
        limits=httpx.Limits(max_connections=8, max_keepalive_connections=6),
        follow_redirects=False,
        headers={"user-agent": f"xdl/{__version__} (+local; private batch downloader)",
                 "accept": "*/*"},
    )


async def run_batch(
    req: BatchRequest,
    *,
    client: httpx.AsyncClient | None = None,
    on_event: EventFn | None = None,
    cancel: CancelToken | None = None,
) -> BatchReport:
    """Full pipeline: normalize -> dedupe -> resolve -> select -> download ->
    finalize, with state-based skip, retries, circuit breaker, graceful
    cancel, and per-item results."""
    settings = req.settings
    emit = on_event or (lambda event: None)
    policy = QualityPolicy.parse(req.quality)
    throttle = ThrottleState(
        min_gap=settings.engine.delay if settings.engine.delay > 0 else None
    )

    own_client = client is None
    http = client or build_client()
    state = StateStore(settings.db_path)
    try:
        emit(ProgressEvent(EventKind.batch_start, counters={"total": len(req.inputs)}))
        items = await _normalize_inputs(http, req, throttle)
        batch_id = state.new_batch(req.origin, total_items=len(items))

        ctx = _RunCtx(
            req=req,
            settings=settings,
            policy=policy,
            http=http,
            state=state,
            chain=build_chain(settings, http),
            download_sem=asyncio.Semaphore(settings.engine.concurrency),
            throttle=throttle,
            emit=emit,
            batch_id=batch_id,
            cancel=cancel,
            ffmpeg=shutil.which("ffmpeg"),
        )
        resolve_sem = asyncio.Semaphore(settings.engine.resolve_concurrency)

        async def _run_item(item: _Item) -> ItemResult:
            if item.error is not None:
                return _failed_result(item, item.error)
            if item.duplicate_of is not None:
                return ItemResult(
                    ref=item.ref or _anon_ref(item.raw),
                    status="skipped",
                    detail=(
                        "duplicate input (SKIPPED_DUP_INPUT, "
                        f"first seen as item #{item.duplicate_of})"
                    ),
                )
            if cancel is not None and cancel.cancelled:
                return _cancelled_result(item.ref, item.raw)
            async with resolve_sem:
                if cancel is not None and cancel.cancelled:
                    return _cancelled_result(item.ref, item.raw)
                if ctx.breaker.open:
                    return _circuit_result(ctx, item)
                prepared = await _prepare_item(item, ctx)
            # The resolve slot is released HERE: transfers hold only
            # download_sem, so the two bounds are independent pools.
            if isinstance(prepared, ItemResult):
                ctx.breaker.record(prepared.status)
                return prepared
            result = await _download_item(prepared, ctx)
            ctx.breaker.record(result.status)
            return result

        async def run_item(item: _Item) -> ItemResult:
            """Item-level containment boundary + per-item timing.

            Every *typed* failure is handled closer to its source; anything that
            still escapes is a bug or an unmapped exception type, and it must cost
            one item — not the batch. (A raw OSError/httpx error used to unwind
            through `gather` and take down the whole run.)
            """
            started = time.perf_counter()
            try:
                result = await _run_item(item)
            except XdlError as err:
                result = _failed_result(item, err)
                ctx.breaker.record(result.status)
            except Exception as err:
                result = _unexpected_result(item, err)
                # A crash is a failure as far as upstream health goes.
                ctx.breaker.record(result.status)
                ctx.emit(ProgressEvent(EventKind.item_done,
                                       tweet_id=result.ref.tweet_id,
                                       status="failed", detail=result.detail))
            result.elapsed_ms = int((time.perf_counter() - started) * 1000)
            return result

        results = list(await asyncio.gather(*(run_item(i) for i in items)))
        report = _assemble_report(results)
        report.cancelled = cancel is not None and cancel.cancelled
        state.finish_batch(batch_id, _batch_status(report))
        emit(
            ProgressEvent(
                EventKind.batch_done,
                status=str(report.exit_code),
                counters={
                    "ok": report.ok,
                    "skipped": report.skipped,
                    "partial": report.partial,
                    "failed": report.failed,
                },
            )
        )
        return report
    finally:
        if own_client:
            await http.aclose()
        state.close()


async def analyze(
    urls: list[str],
    settings: Settings,
    *,
    client: httpx.AsyncClient | None = None,
) -> list[ResolvedTweet | XdlError]:
    """Resolve-only helper for frontends (no state writes, no downloads).

    One result per input line, in order; normalize/resolve failures come back
    as XdlError values, not exceptions.

    Fanned out over `resolve_concurrency` (this backs `POST /analyze`, which
    accepts up to 500 lines — serial resolution made that a multi-minute
    request). `gather` preserves order, so the caller's zip stays 1:1.
    """
    own_client = client is None
    http = client or build_client()
    chain = build_chain(settings, http)
    throttle = ThrottleState(
        min_gap=settings.engine.delay if settings.engine.delay > 0 else None
    )
    sem = asyncio.Semaphore(max(1, settings.engine.resolve_concurrency))

    async def one(raw: str) -> ResolvedTweet | XdlError:
        async with sem:
            try:
                ref = await normalize_url(http, raw, gate=throttle.gate)
                await throttle.gate()
                return await chain.resolve(ref)
            except (XdlError, httpx.HTTPError) as err:
                return (
                    err
                    if isinstance(err, XdlError)
                    else XdlError(ErrorCode.NETWORK, f"connection failed: {err}")
                )

    try:
        return list(await asyncio.gather(*(one(raw) for raw in urls)))
    finally:
        if own_client:
            await http.aclose()


# --------------------------------------------------------------------------- #
# run context / resilience primitives
# --------------------------------------------------------------------------- #


@dataclass
class _RunCtx:
    """Per-run shared state threaded through the item pipeline."""

    req: BatchRequest
    settings: Settings
    policy: QualityPolicy
    http: httpx.AsyncClient
    state: StateStore
    chain: FallbackChain
    download_sem: asyncio.Semaphore
    throttle: ThrottleState
    emit: EventFn
    batch_id: int
    cancel: CancelToken | None = None
    breaker: _CircuitBreaker = field(default_factory=lambda: _CircuitBreaker())
    ffmpeg: str | None = None  # resolved once per run; None = not installed


class _CircuitBreaker:
    """8 consecutive item failures with 0 successes between -> open; the batch
    drains and unstarted items report SKIPPED_CIRCUIT_OPEN (docs/technical/08).
    Stateless between runs — the next run starts half-open by construction."""

    def __init__(self, threshold: int = _CIRCUIT_THRESHOLD) -> None:
        self.threshold = threshold
        self.consecutive = 0
        self.open = False

    def record(self, status: str) -> None:
        # ok/skipped items reset (a skip proves nothing about upstream health,
        # but also proves the batch isn't uniformly poisoned).
        if status in ("failed", "partial"):
            self.consecutive += 1
            if self.consecutive >= self.threshold:
                self.open = True
        else:
            self.consecutive = 0


def _backoff_delay(attempt: int) -> float:
    """1, 2, 4, 8, 16 ... seconds x jitter(0.75-1.25), capped (docs/tech/08)."""
    base: float = _BACKOFF_BASE * (2 ** (attempt - 1))
    delay: float = min(base, _BACKOFF_CAP)
    jitter: float = random.uniform(_JITTER[0], _JITTER[1])
    return delay * jitter


async def _sleep(seconds: float, cancel: CancelToken | None = None) -> None:
    """Interruptible backoff sleep: Ctrl+C wakes it early (raises CANCELLED)."""
    if cancel is None:
        await asyncio.sleep(seconds)
        return
    deadline = time.monotonic() + seconds
    while True:
        if cancel.cancelled:
            raise XdlError(ErrorCode.CANCELLED, "cancelled during backoff")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        await asyncio.sleep(min(remaining, 0.2))


async def _with_retries[T](
    op: Callable[[], Awaitable[T]],
    *,
    retries: int,
    throttle: ThrottleState,
    cancel: CancelToken | None = None,
    on_error: Callable[[XdlError], None] | None = None,
    on_attempt: Callable[[], None] | None = None,
    what: str,
) -> T:
    """Retry `op` on retryable XdlErrors: exponential backoff + jitter, waits
    through `throttle.gate()` so global cooldowns compose; at most
    `retries` total attempts. INTEGRITY_MISMATCH restarts clean at most once
    (docs/technical/08); LINK_EXPIRED never retries here — the orchestrator's
    one-shot re-resolve owns it (layering rule 6).

    `on_attempt` fires once per attempt (attempt counting for the report);
    `on_error` fires per retryable failure (429 escalation).
    """
    attempt = 0
    integrity_restarts = 0
    while True:
        if cancel is not None and cancel.cancelled:
            raise XdlError(ErrorCode.CANCELLED, f"cancelled before {what}")
        if on_attempt is not None:
            on_attempt()
        try:
            return await op()
        except XdlError as err:
            if err.code is ErrorCode.CANCELLED:
                raise
            if err.code is ErrorCode.INTEGRITY_MISMATCH:
                if integrity_restarts >= 1:
                    raise
                integrity_restarts += 1
            elif err.code is ErrorCode.LINK_EXPIRED or not err.retryable:
                raise
            if on_error is not None:
                on_error(err)
            attempt += 1
            if attempt >= retries:
                raise XdlError(
                    err.code,
                    f"{err.detail} (gave up after {attempt} attempts)",
                    retry_after=err.retry_after,
                ) from err
            await throttle.gate()
            await _sleep(_backoff_delay(attempt), cancel)


def _same_media_object(url_a: str, url_b: str) -> bool:
    """Fresh signed URLs always differ; object identity is host + path."""
    a, b = urlsplit(url_a), urlsplit(url_b)
    return (a.hostname or "").lower() == (b.hostname or "").lower() and a.path == b.path


# --------------------------------------------------------------------------- #
# normalize + dedupe
# --------------------------------------------------------------------------- #


class _Item:
    __slots__ = ("duplicate_of", "error", "number", "raw", "ref")

    def __init__(
        self,
        raw: str,
        number: int,
        ref: TweetRef | None = None,
        error: XdlError | None = None,
        duplicate_of: int | None = None,
    ) -> None:
        self.raw = raw
        self.number = number
        self.ref = ref
        self.error = error
        self.duplicate_of = duplicate_of


async def _normalize_inputs(
    http: httpx.AsyncClient, req: BatchRequest, throttle: ThrottleState | None = None
) -> list[_Item]:
    """Parse + dedupe every input, preserving input order.

    Normalization is mostly pure regex work, so it is fanned out: only t.co
    expansion touches the network, and that hop is paced via `throttle.gate`.
    The bound exists for the expanders, not for parsing.
    """
    sem = asyncio.Semaphore(max(1, req.settings.engine.resolve_concurrency))

    async def normalize_one(raw: str) -> TweetRef | XdlError:
        async with sem:
            try:
                return await normalize_url(
                    http,
                    raw,
                    id_mode=req.id_mode,
                    gate=throttle.gate if throttle is not None else None,
                )
            except XdlError as err:
                return err
            except httpx.HTTPError as err:
                # A t.co expansion can fail at the transport layer; one bad link
                # must not abort the whole batch (analyze() has always mapped this).
                return XdlError(ErrorCode.NETWORK, f"connection failed: {err}")

    outcomes = await asyncio.gather(*(normalize_one(raw) for raw in req.inputs))

    items: list[_Item] = []
    seen: dict[str, int] = {}
    for number, (raw, outcome) in enumerate(
        zip(req.inputs, outcomes, strict=True), start=1
    ):
        if isinstance(outcome, XdlError):
            items.append(_Item(raw, number, error=outcome))
            continue
        first = seen.get(outcome.tweet_id)
        if first is not None:
            items.append(_Item(raw, number, ref=outcome, duplicate_of=first))
        else:
            seen[outcome.tweet_id] = number
            items.append(_Item(raw, number, ref=outcome))
    return items


# --------------------------------------------------------------------------- #
# per-item pipeline
# --------------------------------------------------------------------------- #


@dataclass
class _Prepared:
    """An item that resolved and selected — everything the transfer stage needs.

    Splitting this out of `_process_item` is what lets `resolve_sem` cover only
    the metadata phase: resolve capacity and transfer capacity are independent
    pools, so a slow transfer can no longer occupy a resolve slot.
    """

    item: _Item
    tweet: ResolvedTweet
    chosen: list[Chosen]
    ok_by_index: dict[int, DownloadRow]


async def _prepare_item(item: _Item, ctx: _RunCtx) -> _Prepared | ItemResult:
    """Resolve + select under the caller's resolve slot.

    Returns the prepared item, or a terminal ItemResult (skip / failure /
    dry-run) that needs no transfer.
    """
    assert item.ref is not None
    ref = item.ref
    emit = ctx.emit
    emit(ProgressEvent(EventKind.resolve_start, tweet_id=ref.tweet_id))

    # Skip check (resolve stage): count(ok) == media_count AND files exist.
    if not ctx.req.force and ctx.state.seen_complete(ref.tweet_id):
        ok_rows = ctx.state.ok_rows(ref.tweet_id)
        if all(Path(r.file_path).is_file() for r in ok_rows):
            # seen_complete() already proved len(ok_rows) == media_count, so no
            # second trip to the tweets table is needed for the count.
            n = len(ok_rows)
            result = ItemResult(
                ref=ref,
                status="skipped",
                media=[MediaOutcome(index=r.media_index, status="skipped",
                                    file=Path(r.file_path)) for r in ok_rows],
                files=[Path(r.file_path) for r in ok_rows],
                detail=f"all {n} media already downloaded",
            )
            emit(ProgressEvent(EventKind.item_done, tweet_id=ref.tweet_id,
                               status="skipped"))
            return result
        # stale ok-rows (files vanished): fall through and re-download

    try:
        tweet = await _resolve_with_retry(ctx, ref)
    except XdlError as err:
        emit(ProgressEvent(EventKind.item_done, tweet_id=ref.tweet_id, status="failed"))
        return _failed_result(item, err)

    downloadable = [m for m in tweet.media if m.kind in ("video", "gif")]

    try:
        chosen = select(tweet.media, ctx.policy)
    except XdlError as err:  # NO_VIDEO etc.
        # No tweets row for nothing-downloadable posts: a media_count=0 anchor
        # would make seen_complete() report 0 == 0 and false-skip every re-run
        # (round-2 B-lane filing: NO_VIDEO retry false-complete).
        emit(ProgressEvent(EventKind.item_done, tweet_id=ref.tweet_id, status="failed"))
        return _failed_result(item, err)
    ctx.state.upsert_tweet(ref.tweet_id, tweet.author.screen_name, len(downloadable))
    emit(ProgressEvent(EventKind.resolve_done, tweet_id=ref.tweet_id,
                       detail=tweet.author.screen_name))

    if ctx.req.dry_run:
        planned = [_render(ctx, tweet, c.asset.index) for c in chosen]
        return ItemResult(
            ref=ref,
            status="skipped",
            media=[MediaOutcome(index=c.asset.index, status="skipped", file=p)
                   for c, p in zip(chosen, planned, strict=True)],
            detail="dry-run: would write "
                   + ", ".join(str(p) for p in planned),
        )

    ok_by_index = {r.media_index: r for r in ctx.state.ok_rows(ref.tweet_id)}
    return _Prepared(item=item, tweet=tweet, chosen=chosen, ok_by_index=ok_by_index)


async def _download_item(prepared: _Prepared, ctx: _RunCtx) -> ItemResult:
    """Transfer every chosen media of a prepared item (outside the resolve slot)."""
    ref = prepared.tweet.ref
    outcomes: list[MediaOutcome] = []
    for c in prepared.chosen:
        row = prepared.ok_by_index.get(c.asset.index)
        if row is not None and Path(row.file_path).is_file() and not ctx.req.force:
            outcomes.append(MediaOutcome(index=c.asset.index, status="skipped",
                                         file=Path(row.file_path)))
            continue
        async with ctx.download_sem:
            outcomes.append(
                await _download_media(prepared.tweet, c, ctx, known_row=row)
            )
        if ctx.cancel is not None and ctx.cancel.cancelled:
            break  # drain: don't start the next media of this item

    result = _item_result_from(ref, outcomes)
    ctx.emit(ProgressEvent(EventKind.item_done, tweet_id=ref.tweet_id,
                           status=result.status, detail=result.detail))
    return result


async def _resolve_with_retry(ctx: _RunCtx, ref: TweetRef) -> ResolvedTweet:
    async def op() -> ResolvedTweet:
        await ctx.throttle.gate()  # resolve pacing + global cooldown
        return await ctx.chain.resolve(ref)

    def on_error(err: XdlError) -> None:
        if err.code is ErrorCode.RATE_LIMITED:
            # The resolver layer can't see the throttle; the orchestrator
            # turns a resolve-stage 429 into the global pause for everyone.
            ctx.throttle.report_pause(
                err.retry_after if err.retry_after is not None else 30.0
            )

    return await _with_retries(
        op,
        retries=ctx.settings.engine.retries,
        throttle=ctx.throttle,
        cancel=ctx.cancel,
        on_error=on_error,
        what=f"resolve {ref.tweet_id}",
    )


def _render(ctx: _RunCtx, tweet: ResolvedTweet, index: int, ext: str = ".mp4") -> Path:
    return render_path(
        ctx.settings.storage.root,
        tweet,
        index,
        layout=ctx.settings.storage.layout,
        template=ctx.settings.storage.filename_template,
        ext=ext,
    )


# --------------------------------------------------------------------------- #
# media acquisition: progressive fetch / HLS remux / conversions
# --------------------------------------------------------------------------- #


@dataclass
class _TransferStats:
    """Per-media instrumentation: attempts spent and wall time (report data)."""

    attempts: int = 0
    started: float = field(default_factory=time.perf_counter)

    def count_attempt(self) -> None:
        self.attempts += 1

    @property
    def elapsed_ms(self) -> int:
        return int((time.perf_counter() - self.started) * 1000)


async def _download_media(
    tweet: ResolvedTweet,
    chosen: Chosen,
    ctx: _RunCtx,
    *,
    known_row: DownloadRow | None = None,
) -> MediaOutcome:
    """Transfer one media asset, stamping attempts + elapsed time onto the result."""
    stats = _TransferStats()
    outcome = await _transfer_media(tweet, chosen, ctx, known_row=known_row, stats=stats)
    outcome.attempts = max(1, stats.attempts)
    outcome.elapsed_ms = stats.elapsed_ms
    return outcome


async def _transfer_media(
    tweet: ResolvedTweet,
    chosen: Chosen,
    ctx: _RunCtx,
    *,
    known_row: DownloadRow | None = None,
    stats: _TransferStats,
) -> MediaOutcome:
    req = ctx.req
    index = chosen.asset.index
    tweet_id = tweet.ref.tweet_id
    mode: str | None = "mp3" if req.audio_only else ("gif" if req.gif else None)
    ext = {None: ".mp4", "mp3": ".mp3", "gif": ".gif"}[mode]

    if mode is not None and ctx.ffmpeg is None:
        return MediaOutcome(
            index=index,
            status="failed",
            error=XdlError(
                ErrorCode.FFMPEG_REQUIRED,
                f"--{'audio-only' if mode == 'mp3' else 'gif'} needs ffmpeg — "
                f"{hls.FFMPEG_INSTALL_HINT}",
            ),
        )

    # Determinism contract: reuse the state-anchored path when recorded — but
    # only for the same output kind (an .mp4 row must not anchor an MP3 run).
    # `known_row` is the caller's already-fetched ok row for this index; the
    # download loop holds the full map, so re-querying here would be N+1.
    row = known_row if known_row is not None and known_row.file_path.suffix == ext else None
    if row is not None and Path(row.file_path).is_file():
        dest = Path(row.file_path)
    else:
        dest = _render(ctx, tweet, index, ext=ext)

    # Conversion modes stage the container under a transient .src name; a
    # leftover from an interrupted earlier attempt resumes the pipeline.
    src = dest.with_name(dest.stem + ".src.mp4") if mode else dest

    # --force: re-download. The existing final becomes <name>.bak (last bak
    # wins) and any stale .part is discarded — the object may have changed.
    if req.force:
        if dest.is_file():
            bak = dest.with_name(dest.name + ".bak")
            bak.unlink(missing_ok=True)
            dest.rename(bak)
        src.with_name(src.name + ".part").unlink(missing_ok=True)

    # Idempotent replay: media final file already present (crash between
    # rename and state write) -> finalize without re-downloading.
    if dest.is_file():
        if mode is not None:
            src.unlink(missing_ok=True)  # stale transient container
        await _finalize(tweet, chosen, dest, dest.stat().st_size, ctx)
        ctx.emit(ProgressEvent(EventKind.download_done, tweet_id=tweet_id,
                               media_index=index, status="ok", path=str(dest)))
        return MediaOutcome(index=index, status="ok", file=dest)

    ctx.state.mark_incomplete(tweet_id, index, dest, batch_id=ctx.batch_id,
                              variant_url=chosen.variant.url)
    ctx.emit(ProgressEvent(EventKind.download_start, tweet_id=tweet_id,
                           media_index=index, path=str(dest)))

    def on_progress(delta: int, total: int | None) -> None:
        ctx.emit(ProgressEvent(EventKind.download_progress, tweet_id=tweet_id,
                               media_index=index, offset=delta, total=total))

    try:
        effective = await _acquire(
            tweet, chosen, src, ctx, on_progress, on_attempt=stats.count_attempt
        )
        if mode is not None and not dest.is_file():
            await _transcode(tweet, index, src, dest, mode, ctx)
            src.unlink(missing_ok=True)  # transient container, artifact is dest
    except XdlError as err:
        ctx.emit(ProgressEvent(EventKind.download_done, tweet_id=tweet_id,
                               media_index=index, status="failed",
                               detail=err.code))
        if mode is not None:
            # A failed transcode is terminal (TRANSCODE_FAILED isn't
            # retryable): drop the transient container rather than litter.
            src.unlink(missing_ok=True)
        if err.code is not ErrorCode.CANCELLED:
            # CANCELLED keeps the `incomplete` row + .part for resume.
            _record_failure(ctx.state, tweet, index, dest, chosen, err.code,
                            ctx.batch_id)
        return MediaOutcome(index=index, status="failed", error=err)

    size = dest.stat().st_size
    await _finalize(tweet, effective, dest, size, ctx)
    ctx.emit(ProgressEvent(EventKind.download_done, tweet_id=tweet_id,
                           media_index=index, status="ok", path=str(dest)))
    return MediaOutcome(index=index, status="ok", file=dest)


async def _acquire(
    tweet: ResolvedTweet,
    chosen: Chosen,
    src: Path,
    ctx: _RunCtx,
    on_progress: Callable[[int, int | None], None] | None = None,
    on_attempt: Callable[[], None] | None = None,
) -> Chosen:
    """Move the chosen variant's container to `src` (fetch or HLS remux).

    Returns the Chosen actually fetched (a re-resolved fresh match after
    LINK_EXPIRED recovery). 403 on a media URL never retries here — the
    orchestrator re-resolves once and re-dispatches (layering rule 6),
    resuming the .part when the object (host+path) is unchanged, restarting
    clean otherwise.
    """
    index = chosen.asset.index
    tweet_id = tweet.ref.tweet_id
    variant = chosen.variant
    master_text: str | None = None

    if not is_hls(variant):
        upgraded = await _maybe_upgrade_to_hls(tweet, chosen, ctx)
        if upgraded is not None:
            variant, master_text = upgraded

    recovering = False  # LINK_EXPIRED one-shot recovery (rule 6)
    while True:
        if is_hls(variant):
            if ctx.ffmpeg is None:
                raise XdlError(
                    ErrorCode.HLS_REQUIRES_FFMPEG,
                    f"best quality is HLS and ffmpeg wasn't found — "
                    f"{hls.FFMPEG_INSTALL_HINT} (or re-run with --quality to "
                    "cap below HLS)",
                )
            if not src.is_file():
                await _acquire_hls(variant.url, src, ctx, tweet_id, index,
                                   master_text=master_text)
            return Chosen(asset=chosen.asset, variant=variant)

        if src.is_file():
            return Chosen(asset=chosen.asset, variant=variant)  # staged earlier

        async def op(url: str = variant.url, dest: Path = src) -> Path:
            return await fetch(url, dest, ctx.http, cb=on_progress,
                               throttle=ctx.throttle, cancel=ctx.cancel)

        try:
            await _with_retries(
                op,
                retries=ctx.settings.engine.retries,
                throttle=ctx.throttle,
                cancel=ctx.cancel,
                on_attempt=on_attempt,
                what=f"download {tweet_id}/{index}",
            )
            return Chosen(asset=chosen.asset, variant=variant)
        except XdlError as err:
            if err.code is not ErrorCode.LINK_EXPIRED or recovering:
                raise
            recovering = True
            fresh = await _re_resolve_media(ctx, tweet, index)
            if not _same_media_object(variant.url, fresh.variant.url):
                # Different rendition: a stale .part would append a mismatch.
                src.with_name(src.name + ".part").unlink(missing_ok=True)
            chosen, variant = fresh, fresh.variant


async def _re_resolve_media(ctx: _RunCtx, tweet: ResolvedTweet, index: int) -> Chosen:
    """Fresh resolve for LINK_EXPIRED recovery; the media must still exist."""
    tweet_id = tweet.ref.tweet_id

    async def op() -> ResolvedTweet:
        await ctx.throttle.gate()
        return await ctx.chain.resolve(tweet.ref)

    fresh = await _with_retries(
        op,
        retries=ctx.settings.engine.retries,
        throttle=ctx.throttle,
        cancel=ctx.cancel,
        what=f"re-resolve {tweet_id}",
    )
    rechosen = select(fresh.media, ctx.policy)  # the BATCH policy, not default
    match = next((c for c in rechosen if c.asset.index == index), None)
    if match is None:
        raise XdlError(ErrorCode.NO_VIDEO, "media vanished on re-resolve")
    return match


async def _maybe_upgrade_to_hls(
    tweet: ResolvedTweet, chosen: Chosen, ctx: _RunCtx
) -> tuple[Variant, str] | None:
    """Field-correct HLS policy (docs/technical/04 banner): the MP4 fast-path
    always wins; consider HLS only when the best MP4 is sub-720p AND ffmpeg is
    available AND the asset carries an m3u8 — then fetch the master and take
    HLS only if its best rendition is genuinely taller (RESOLUTION compare;
    payloads publish no HLS bitrate). Best-effort: any failure keeps the MP4.

    Returns the m3u8 variant plus the master text already fetched, so the remux
    does not re-request the same playlist.
    """
    height = chosen.variant.height
    if ctx.ffmpeg is None or height is None or height >= 720:
        return None
    m3u8 = next((v for v in chosen.asset.variants if is_hls(v)), None)
    if m3u8 is None:
        return None
    try:
        await ctx.throttle.gate()
        r = await ctx.http.get(m3u8.url)
        if r.status_code != 200:
            return None
        best = hls.choose_rendition(hls.parse_master(r.text, m3u8.url), ctx.policy)
    except (XdlError, httpx.HTTPError):
        return None
    if (best.height or 0) > height:
        return m3u8, r.text
    return None


async def _acquire_hls(
    master_url: str,
    src: Path,
    ctx: _RunCtx,
    tweet_id: str,
    index: int,
    *,
    master_text: str | None = None,
) -> None:
    """Fetch manifests through the shared client/pacing, remux via ffmpeg
    (media/hls owns the parse/DRM-gate/.part invariant)."""

    async def fetch_text(url: str) -> str:
        await ctx.throttle.gate()
        if ctx.cancel is not None and ctx.cancel.cancelled:
            raise XdlError(ErrorCode.CANCELLED, "cancelled before m3u8 fetch")
        try:
            r = await ctx.http.get(url)
        except httpx.HTTPError as err:
            # This is a direct client call, not the engine: without the mapping a
            # transport error escapes every XdlError handler up to the batch.
            raise XdlError(ErrorCode.NETWORK, f"m3u8 fetch failed: {err}") from err
        if r.status_code == 429:
            ctx.throttle.report_429(r.headers.get("retry-after"))
            raise XdlError(
                ErrorCode.RATE_LIMITED,
                "m3u8 fetch rate limited",
                retry_after=parse_retry_after(r.headers.get("retry-after")),
            )
        if r.status_code != 200:
            raise XdlError(ErrorCode.UPSTREAM_ERROR, f"m3u8 fetch HTTP {r.status_code}")
        return r.text

    def frame_cb(frame: dict[str, str]) -> None:
        size = frame.get("total_size", "")
        if size.isdigit():
            ctx.emit(ProgressEvent(EventKind.download_progress, tweet_id=tweet_id,
                                   media_index=index, offset=int(size)))

    await hls.download_hls(
        master_url,
        src,
        policy=ctx.policy,
        ffmpeg=ctx.ffmpeg,
        timeout_min=ctx.settings.hls.ffmpeg_timeout_min,
        fetch_text=fetch_text,
        frame_cb=frame_cb,
        master_text=master_text,
    )


async def _transcode(
    tweet: ResolvedTweet,
    index: int,
    src: Path,
    dest: Path,
    mode: str,
    ctx: _RunCtx,
) -> None:
    """ffmpeg conversion (docs/technical/04): MP3 (V0) or real GIF
    (palettegen/paletteuse); same .part + atomic-rename invariant."""
    assert ctx.ffmpeg is not None
    part = dest.with_name(dest.name + ".part")

    def frame_cb(frame: dict[str, str]) -> None:
        size = frame.get("total_size", "")
        if size.isdigit():
            ctx.emit(ProgressEvent(EventKind.download_progress,
                                   tweet_id=tweet.ref.tweet_id,
                                   media_index=index, offset=int(size)))

    if mode == "mp3":
        argv = convert.mp3_argv(src, part, ctx.ffmpeg, progress=True)
    else:
        argv = convert.gif_argv(src, part, ctx.ffmpeg, progress=True)
    await convert.run_ffmpeg(
        argv, timeout_min=ctx.settings.hls.ffmpeg_timeout_min, frame_cb=frame_cb
    )
    await asyncio.to_thread(part.replace, dest)


def _record_failure(
    state: StateStore,
    tweet: ResolvedTweet,
    index: int,
    dest: Path,
    chosen: Chosen,
    code: ErrorCode,
    batch_id: int,
) -> None:
    state.record(
        DownloadRow(
            tweet_id=tweet.ref.tweet_id,
            media_index=index,
            batch_id=batch_id,
            variant_url=chosen.variant.url,
            file_path=dest,
            status="failed",
            error=code,
            source=tweet.source,
        )
    )


async def _finalize(
    tweet: ResolvedTweet,
    chosen: Chosen,
    media_file: Path,
    size: int,
    ctx: _RunCtx,
) -> None:
    """Write order (crash-safety): media rename (done) -> sidecar -> state."""
    if not ctx.req.no_meta:
        payload = build_payload(tweet, chosen, media_file, size_bytes=size)
        await write_sidecar(media_file, payload)
    ctx.state.record(
        DownloadRow(
            tweet_id=tweet.ref.tweet_id,
            media_index=chosen.asset.index,
            batch_id=ctx.batch_id,
            variant_url=chosen.variant.url,
            file_path=media_file,
            size_bytes=size,
            status="ok",
            source=tweet.source,
        )
    )


# --------------------------------------------------------------------------- #
# result assembly
# --------------------------------------------------------------------------- #


def _item_result_from(ref: TweetRef, outcomes: list[MediaOutcome]) -> ItemResult:
    ok = [o for o in outcomes if o.status == "ok"]
    skipped = [o for o in outcomes if o.status == "skipped"]
    failed = [o for o in outcomes if o.status == "failed"]
    first_error = failed[0].error if failed else None

    if failed and ok:
        status = "partial"  # >=1 ok AND >=1 failed (round 3)
        detail = f"{len(ok)}/{len(outcomes)} media ok; {len(failed)} failed"
        if first_error is not None:
            detail += f" {first_error.code}"
    elif failed:
        status = "failed"
        if first_error is not None:
            detail = (
                f"{first_error.code} — {first_error.detail}"
                if first_error.detail
                else first_error.code
            )
        else:
            detail = ""
    elif skipped and not ok:
        status = "skipped"
        detail = f"all {len(skipped)} media already downloaded"
    else:
        status = "ok"
        detail = f"{len(ok)} media file(s) downloaded"

    # Retries are otherwise invisible: a clean transfer and one that needed
    # three attempts produced byte-identical reports before this.
    retried = max((o.attempts for o in outcomes), default=1)
    if retried > 1:
        detail += f" ({retried} attempts)"

    return ItemResult(
        ref=ref,
        status=status,
        media=outcomes,
        error=first_error,
        files=[o.file for o in ok if o.file is not None],
        detail=detail,
    )


def _failed_result(item: _Item, err: XdlError) -> ItemResult:
    ref = item.ref or _anon_ref(item.raw)
    detail = f"{err.code} — {err.detail}" if err.detail else err.code
    return ItemResult(ref=ref, status="failed", error=err, detail=detail)


def _unexpected_result(item: _Item, err: Exception) -> ItemResult:
    """Containment boundary for anything that is not an XdlError.

    The exception *type* is the actionable part (it points at the line that
    raised), so it leads the detail line; the message follows.
    """
    ref = item.ref or _anon_ref(item.raw)
    detail = f"UNEXPECTED — {type(err).__name__}: {err}"
    return ItemResult(
        ref=ref,
        status="failed",
        error=XdlError(ErrorCode.UNEXPECTED, detail),
        detail=detail,
    )


def _cancelled_result(ref: TweetRef | None, raw: str) -> ItemResult:
    return ItemResult(
        ref=ref or _anon_ref(raw),
        status="failed",
        error=XdlError(ErrorCode.CANCELLED, "interrupted before start"),
        detail="CANCELLED — interrupted before start (nothing lost; re-run resumes)",
    )


def _circuit_result(ctx: _RunCtx, item: _Item) -> ItemResult:
    assert item.ref is not None
    # Recorded incomplete (only when untracked) so `xdl retry-failed` picks
    # the item up naturally later (docs/technical/08).
    ctx.state.mark_tweet_incomplete_if_untracked(item.ref.tweet_id)
    detail = (
        "SKIPPED_CIRCUIT_OPEN — upstream failing consecutively; "
        "run xdl retry-failed later"
    )
    ctx.emit(ProgressEvent(EventKind.item_done, tweet_id=item.ref.tweet_id,
                           status="skipped", detail=detail))
    return ItemResult(ref=item.ref, status="skipped", detail=detail)


def _anon_ref(raw: str) -> TweetRef:
    return TweetRef(tweet_id="?", screen_name=None, canonical_url=raw)


def _assemble_report(results: list[ItemResult]) -> BatchReport:
    # Folding per docs/technical/07: 0 all ok/skipped/dup; 1 mixed; 2 all failed.
    any_bad = any(r.status in ("failed", "partial") for r in results)
    if not any_bad:
        code = 0
    elif all(r.status == "failed" for r in results):
        code = 2
    else:
        code = 1
    return BatchReport(results=results, exit_code=code)


def _batch_status(report: BatchReport) -> str:
    if report.failed == 0 and report.partial == 0:
        return "done"
    if report.ok or report.partial or report.skipped:
        return "partial"
    return "failed"
