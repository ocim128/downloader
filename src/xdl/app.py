"""Orchestrator: batch lifecycle, stage wiring, skip decisions, report assembly.

The only component frontends talk to (docs/02): `run_batch(BatchRequest) ->
BatchReport`. Also owns cross-stage recovery: the engine raises
`LinkExpiredError`; app.py — never the engine — re-resolves and re-dispatches
once (layering rule 6).

Concurrency model: one item-task per input; resolves bounded by
`resolve_sem`, transfers bounded by `download_sem`. Resolves of later items
overlap transfers of earlier ones — the streaming property docs/02's
queue+workers formulation provides. (Deviation from the letter —
semaphore-bounded item tasks instead of a literal asyncio.Queue — noted in
COORDINATION.md; semantics identical for bounded fan-out.)
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path

import httpx

from xdl import __version__
from xdl.config import Settings
from xdl.engine.downloader import fetch
from xdl.engine.throttle import ThrottleState
from xdl.media.selector import is_hls, select
from xdl.models import (
    BatchReport,
    BatchRequest,
    Chosen,
    DownloadJob,
    DownloadRow,
    ErrorCode,
    ItemResult,
    MediaOutcome,
    QualityPolicy,
    ResolvedTweet,
    TweetRef,
    XdlError,
)
from xdl.report.events import EventKind, ProgressEvent
from xdl.resolve.base import FallbackChain, build_chain
from xdl.storage.naming import render_path
from xdl.storage.sidecar import build_payload, write_sidecar
from xdl.storage.state import StateStore
from xdl.urls import normalize as normalize_url

EventFn = Callable[[ProgressEvent], None]


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
) -> BatchReport:
    """Full pipeline: normalize -> dedupe -> resolve -> select -> download ->
    finalize, with state-based skip and per-item results."""
    settings = req.settings
    emit = on_event or (lambda event: None)
    policy = QualityPolicy.parse(req.quality)
    throttle = ThrottleState(
        min_gap=settings.engine.delay if settings.engine.delay > 0 else None
    )

    own_client = client is None
    http = client or build_client()
    state = StateStore(settings.db_path)
    chain = build_chain(settings, http)
    resolve_sem = asyncio.Semaphore(settings.engine.resolve_concurrency)
    download_sem = asyncio.Semaphore(settings.engine.concurrency)

    try:
        emit(ProgressEvent(EventKind.batch_start, counters={"total": len(req.inputs)}))
        items = await _normalize_inputs(http, req)
        batch_id = state.new_batch(req.origin, total_items=len(items))

        async def run_item(item: _Item) -> ItemResult:
            if item.error is not None:
                return _failed_result(item, item.error)
            if item.duplicate_of is not None:
                return ItemResult(
                    ref=item.ref,
                    status="skipped",
                    detail=(
                        "duplicate input (SKIPPED_DUP_INPUT, "
                        f"first seen as item #{item.duplicate_of})"
                    ),
                )
            async with resolve_sem:
                return await _process_item(
                    item, req, settings, policy, http, state, chain,
                    download_sem, throttle, emit, batch_id,
                )

        results = list(await asyncio.gather(*(run_item(i) for i in items)))
        report = _assemble_report(results)
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


async def _normalize_inputs(http: httpx.AsyncClient, req: BatchRequest) -> list[_Item]:
    items: list[_Item] = []
    seen: dict[str, int] = {}
    for number, raw in enumerate(req.inputs, start=1):
        try:
            ref = await normalize_url(http, raw, id_mode=req.id_mode)
        except XdlError as err:
            items.append(_Item(raw, number, error=err))
            continue
        first = seen.get(ref.tweet_id)
        if first is not None:
            items.append(_Item(raw, number, ref=ref, duplicate_of=first))
        else:
            seen[ref.tweet_id] = number
            items.append(_Item(raw, number, ref=ref))
    return items


# --------------------------------------------------------------------------- #
# per-item pipeline
# --------------------------------------------------------------------------- #


async def _process_item(
    item: _Item,
    req: BatchRequest,
    settings: Settings,
    policy: QualityPolicy,
    http: httpx.AsyncClient,
    state: StateStore,
    chain: FallbackChain,
    download_sem: asyncio.Semaphore,
    throttle: ThrottleState,
    emit: EventFn,
    batch_id: int,
) -> ItemResult:
    assert item.ref is not None
    ref = item.ref
    emit(ProgressEvent(EventKind.resolve_start, tweet_id=ref.tweet_id))

    # Skip check (resolve stage): count(ok) == media_count AND files exist.
    if not req.force and state.seen_complete(ref.tweet_id):
        ok_rows = state.ok_rows(ref.tweet_id)
        if all(Path(r.file_path).is_file() for r in ok_rows):
            tweet_row = state.get_tweet(ref.tweet_id)
            n = tweet_row.media_count if tweet_row else len(ok_rows)
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
        tweet = await chain.resolve(ref)
    except XdlError as err:
        emit(ProgressEvent(EventKind.item_done, tweet_id=ref.tweet_id, status="failed"))
        return _failed_result(item, err)

    downloadable = [m for m in tweet.media if m.kind in ("video", "gif")]
    state.upsert_tweet(ref.tweet_id, tweet.author.screen_name, len(downloadable))

    try:
        chosen = select(tweet.media, policy)
    except XdlError as err:  # NO_VIDEO etc.
        emit(ProgressEvent(EventKind.item_done, tweet_id=ref.tweet_id, status="failed"))
        return _failed_result(item, err)
    emit(ProgressEvent(EventKind.resolve_done, tweet_id=ref.tweet_id,
                       detail=tweet.author.screen_name))

    if req.dry_run:
        planned = [
            _render(settings, tweet, c.asset.index) for c in chosen
        ]
        return ItemResult(
            ref=ref,
            status="skipped",
            media=[MediaOutcome(index=c.asset.index, status="skipped", file=p)
                   for c, p in zip(chosen, planned, strict=True)],
            detail="dry-run: would write "
                   + ", ".join(str(p) for p in planned),
        )

    ok_by_index = {r.media_index: r for r in state.ok_rows(ref.tweet_id)}
    outcomes: list[MediaOutcome] = []
    for c in chosen:
        row = ok_by_index.get(c.asset.index)
        if row is not None and Path(row.file_path).is_file() and not req.force:
            outcomes.append(MediaOutcome(index=c.asset.index, status="skipped",
                                         file=Path(row.file_path)))
            continue
        async with download_sem:
            outcomes.append(
                await _download_media(tweet, c, req, settings, http, state,
                                      throttle, emit, batch_id)
            )

    result = _item_result_from(ref, outcomes)
    emit(ProgressEvent(EventKind.item_done, tweet_id=ref.tweet_id,
                       status=result.status, detail=result.detail))
    return result


def _render(settings: Settings, tweet: ResolvedTweet, index: int) -> Path:
    return render_path(
        settings.storage.root,
        tweet,
        index,
        layout=settings.storage.layout,
        template=settings.storage.filename_template,
    )


async def _download_media(
    tweet: ResolvedTweet,
    chosen: Chosen,
    req: BatchRequest,
    settings: Settings,
    http: httpx.AsyncClient,
    state: StateStore,
    throttle: ThrottleState,
    emit: EventFn,
    batch_id: int,
) -> MediaOutcome:
    index = chosen.asset.index
    tweet_id = tweet.ref.tweet_id

    if is_hls(chosen.variant):
        # Best variant is HLS: the ffmpeg remux path lands with media/hls
        # (M5 / Agent B's lane). Until then the item fails with the spec'd
        # error + install hint.
        return MediaOutcome(
            index=index,
            status="failed",
            error=XdlError(
                ErrorCode.HLS_REQUIRES_FFMPEG,
                "best quality is HLS and the ffmpeg remux path is not wired "
                "yet (install: winget install ffmpeg)",
            ),
        )

    # Determinism contract: reuse the state-anchored path when recorded.
    row = next((r for r in state.ok_rows(tweet_id) if r.media_index == index), None)
    if row is not None and Path(row.file_path).is_file():
        dest = Path(row.file_path)
    else:
        dest = _render(settings, tweet, index)

    job = DownloadJob(
        ref=tweet.ref,
        media_index=index,
        kind=chosen.asset.kind if chosen.asset.kind in ("video", "gif") else "video",
        url=chosen.variant.url,
        dest=dest,
        content_type=chosen.variant.content_type,
        bitrate=chosen.variant.bitrate,
        width=chosen.variant.width,
        height=chosen.variant.height,
    )

    # Idempotent replay: media final file already present (crash between
    # rename and state write) -> finalize without re-downloading.
    if dest.is_file():
        await _finalize(tweet, chosen, dest, dest.stat().st_size, req, state, batch_id)
        return MediaOutcome(index=index, status="ok", file=dest)

    state.mark_incomplete(tweet_id, index, dest, batch_id=batch_id,
                          variant_url=chosen.variant.url)
    emit(ProgressEvent(EventKind.download_start, tweet_id=tweet_id,
                       media_index=index, path=str(dest)))

    def on_progress(delta: int, total: int | None) -> None:
        emit(ProgressEvent(EventKind.download_progress, tweet_id=tweet_id,
                           media_index=index, offset=delta, total=total))

    try:
        final = await fetch(job, http, cb=on_progress, throttle=throttle)
    except XdlError as err:
        emit(ProgressEvent(EventKind.download_done, tweet_id=tweet_id,
                           media_index=index, status="failed", detail=err.code))
        if err.code is ErrorCode.LINK_EXPIRED:
            # Orchestrator-owned recovery (layering rule 6): one re-resolve
            # with a fresh signed URL, same destination.
            return await _recover_link_expired(
                tweet, chosen, req, settings, http, state, throttle, emit, batch_id
            )
        _record_failure(state, tweet, index, dest, chosen, err.code, batch_id)
        return MediaOutcome(index=index, status="failed", error=err)

    size = final.stat().st_size
    await _finalize(tweet, chosen, final, size, req, state, batch_id)
    emit(ProgressEvent(EventKind.download_done, tweet_id=tweet_id,
                       media_index=index, status="ok", path=str(final)))
    return MediaOutcome(index=index, status="ok", file=final)


async def _recover_link_expired(
    tweet: ResolvedTweet,
    chosen: Chosen,
    req: BatchRequest,
    settings: Settings,
    http: httpx.AsyncClient,
    state: StateStore,
    throttle: ThrottleState,
    emit: EventFn,
    batch_id: int,
) -> MediaOutcome:
    index = chosen.asset.index
    tweet_id = tweet.ref.tweet_id
    dest = _render(settings, tweet, index)
    try:
        fresh = await build_chain(req.settings, http).resolve(tweet.ref)
        rechosen = select(fresh.media, QualityPolicy())
        match = next((c for c in rechosen if c.asset.index == index), None)
        if match is None:
            raise XdlError(ErrorCode.NO_VIDEO, "media vanished on re-resolve")
    except XdlError as err:
        _record_failure(state, tweet, index, dest, chosen, err.code, batch_id)
        return MediaOutcome(index=index, status="failed", error=err)

    job = DownloadJob(
        ref=tweet.ref,
        media_index=index,
        kind=chosen.asset.kind if chosen.asset.kind in ("video", "gif") else "video",
        url=match.variant.url,
        dest=dest,
        content_type=match.variant.content_type,
        bitrate=match.variant.bitrate,
        width=match.variant.width,
        height=match.variant.height,
    )
    try:
        final = await fetch(job, http, throttle=throttle)
    except XdlError as err:
        _record_failure(state, tweet, index, dest, chosen, err.code, batch_id)
        return MediaOutcome(index=index, status="failed", error=err)
    await _finalize(tweet, match, final, final.stat().st_size, req, state, batch_id)
    emit(ProgressEvent(EventKind.download_done, tweet_id=tweet_id,
                       media_index=index, status="ok", path=str(final)))
    return MediaOutcome(index=index, status="ok", file=final)


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
    req: BatchRequest,
    state: StateStore,
    batch_id: int,
) -> None:
    """Write order (crash-safety): media rename (done) -> sidecar -> state."""
    if not req.no_meta:
        payload = build_payload(tweet, chosen, media_file, size_bytes=size)
        await write_sidecar(media_file, payload)
    state.record(
        DownloadRow(
            tweet_id=tweet.ref.tweet_id,
            media_index=chosen.asset.index,
            batch_id=batch_id,
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

    return ItemResult(
        ref=ref,
        status=status,
        media=outcomes,
        error=first_error,
        files=[o.file for o in ok if o.file is not None],
        detail=detail,
    )


def _failed_result(item: _Item, err: XdlError) -> ItemResult:
    ref = item.ref or TweetRef(tweet_id="?", screen_name=None, canonical_url=item.raw)
    detail = f"{err.code} — {err.detail}" if err.detail else err.code
    return ItemResult(ref=ref, status="failed", error=err, detail=detail)


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
