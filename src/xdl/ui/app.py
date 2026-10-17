"""`xdl serve`: localhost-only web UI over the injected orchestrator
(docs/technical/10-local-web-ui.md).

Layering-rule 1 frontend: imports `config` + `report` + `models` shapes only;
the orchestrator, Flow-B analyzer, batch-history and file-reveal surfaces are
injected callables (tests inject a stub matching the documented contract;
`python -m xdl.ui` wires the real pipeline by importing `xdl.app`).

Security posture (spec):
- binds 127.0.0.1 only (the server runner hard-fails on 0.0.0.0/::),
- Host-header check -> 403 outside 127.0.0.1/localhost (DNS rebinding),
- CSP `default-src 'self'` (plus img-src allowlist for poster thumbs;
  DECISIONS.md #20),
- POST /jobs caps input at 500 URLs,
- no downloads-tree directory listing: `reveal` shells out to the OS manager.
"""

from __future__ import annotations

import asyncio
import heapq
import json as _json
import re
import subprocess
import sys
import time
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine
from dataclasses import asdict, dataclass, field
from itertools import count
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field

from xdl.config import Settings
from xdl.models import BatchReport, BatchRequest, ResolvedTweet, XdlError
from xdl.report.events import EventKind, ProgressEvent

# --------------------------------------------------------------------------- #
# Injected contract (documented shapes; stubbed in tests, real post-merge)
# --------------------------------------------------------------------------- #

#: run_batch(BatchRequest, *, on_event=<EventFn>) -> BatchReport
RunBatchFn = Callable[..., Awaitable[BatchReport]]
#: analyze(url lines) -> Flow-B card dicts (thumbnail/author/text/formats)
AnalyzeFn = Callable[[list[str]], Awaitable[list[dict[str, Any]]]]
#: list recent batches (status counts), newest first
ListBatchesFn = Callable[[int], Awaitable[list[dict[str, Any]]]]
#: ok file paths for one tweet id (reveal backing)
ItemFilesFn = Callable[[str], Awaitable[list[str]]]

_UI_DIR = Path(__file__).resolve().parent
TEMPLATES = Jinja2Templates(directory=str(_UI_DIR / "templates"))

MAX_URLS = 500
CSP = (
    "default-src 'self'; "
    "img-src 'self' https://pbs.twimg.com https://ton.twimg.com data:; "
    "style-src 'self'; script-src 'self'"
)
_ALLOWED_HOSTS = frozenset({"127.0.0.1", "localhost"})

#: RFC 9112 absolute-form request target ("POST http://host:port/path").
_ABS_TARGET_RE = re.compile(r"^https?://", re.IGNORECASE)


class _AbsoluteFormMiddleware:
    """Accept absolute-form request targets before routing sees them.

    A forward proxy (corporate Windows, some transparent proxies) forwards
    loopback requests as ``POST http://127.0.0.1:7788/jobs``. RFC 9112 says
    servers MUST accept that form; uvicorn's h11 parser instead leaves the
    whole string in ``scope["path"]``, so every route 404s. Normalize it here.
    Only loopback authorities are rewritten — anything else keeps the normal
    403/404 path.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: Callable[[], Awaitable[dict[str, Any]]],
        send: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        if scope["type"] == "http":
            _normalize_absolute_target(scope)
        await self.app(scope, receive, send)


def _normalize_absolute_target(scope: dict[str, Any]) -> None:
    """Rewrite ``scope["path"]`` in place when it holds an absolute-form URI."""
    target = scope.get("path") or ""
    if not _ABS_TARGET_RE.match(target):
        return
    try:
        parts = urlsplit(target)
        host = (parts.hostname or "").lower()
    except ValueError:  # malformed port etc. — let the router 404 it
        return
    if host not in _ALLOWED_HOSTS:
        return
    path = parts.path or "/"
    scope["path"] = unquote(path)
    scope["raw_path"] = path.encode("ascii", "ignore")
    if parts.query:
        scope["query_string"] = parts.query.encode("ascii", "ignore")


# --------------------------------------------------------------------------- #
# Job hub (in-memory; this is a one-page personal tool)
# --------------------------------------------------------------------------- #

#: Recent progress ticks kept for replay. Lifecycle events are always kept (they
#: are bounded by the item count); progress ticks arrive once per 64 KiB and are
#: worthless once superseded, so retaining every one turned a 500-item batch into
#: a 41k-event / 6.8 MB replay on every reconnect. Only the tail is kept.
_MAX_PROGRESS_REPLAY = 200

#: Finished jobs kept in memory (the state store is the durable history).
_MAX_JOBS = 50


@dataclass
class _Job:
    id: str
    inputs: list[str]  # normalized at create time; run_batch must see 1:1
    quality: str | None
    output: str | None
    concurrency: int | None
    started: float = field(default_factory=time.time)
    status: str = "running"  # running | ok | partial | failed
    events: list[dict[str, Any]] = field(default_factory=list)  # lifecycle only
    progress: deque[dict[str, Any]] = field(
        default_factory=lambda: deque(maxlen=_MAX_PROGRESS_REPLAY)
    )
    subscribers: list[asyncio.Queue[tuple[int, dict[str, Any]]]] = field(default_factory=list)
    report: BatchReport | None = None
    error: str = ""
    done: bool = False
    counts: dict[str, int] = field(default_factory=dict)  # item_done tallies, O(1)
    last_seq: int = 0
    _seq: count[int] = field(default_factory=lambda: count(start=1), repr=False)

    def emit(self, event: ProgressEvent) -> None:
        payload = asdict(event)
        payload["kind"] = event.kind.value
        payload["seq"] = next(self._seq)
        self.last_seq = payload["seq"]
        if event.kind is EventKind.download_progress:
            self.progress.append(payload)  # ring buffer
        else:
            self.events.append(payload)
        if event.kind is EventKind.item_done:
            status = payload.get("status")
            if isinstance(status, str):
                self.counts[status] = self.counts.get(status, 0) + 1
        for queue in self.subscribers:
            queue.put_nowait((payload["seq"], payload))

    def replay(self) -> list[dict[str, Any]]:
        """Chronological backlog: every lifecycle event + the recent progress tail."""
        return list(heapq.merge(self.events, self.progress, key=lambda e: e["seq"]))

    @property
    def finished(self) -> bool:
        return self.done and any(
            e["kind"] == EventKind.batch_done.value for e in self.events
        )


class _Hub:
    """Job registry; enforces one in-flight batch (personal tool)."""

    def __init__(self) -> None:
        self.jobs: dict[str, _Job] = {}
        self.order: list[str] = []
        self.running_id: str | None = None
        self._ids = count(start=1)
        self._tasks: set[asyncio.Task[None]] = set()

    def spawn(self, coro: Coroutine[Any, Any, None]) -> None:
        """Start a batch task and keep a strong reference to it.

        The loop only holds a weak reference, so a bare `create_task` can be
        garbage-collected mid-flight (RUF006) and its failure silently dropped.
        """
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def create(self, job: _Job) -> _Job:
        job.id = str(next(self._ids))
        self.jobs[job.id] = job
        self.order.append(job.id)
        self.running_id = job.id
        self._evict()
        return job

    def _evict(self, keep: int = _MAX_JOBS) -> None:
        """Drop the oldest FINISHED jobs beyond `keep` — a long-lived server must
        not grow without bound. The in-flight job is always the newest."""
        while len(self.order) > keep:
            oldest = self.jobs.get(self.order[0])
            if oldest is not None and not oldest.done:
                break
            self.jobs.pop(self.order.pop(0), None)

    def finish(self, job: _Job) -> None:
        if self.running_id == job.id:
            self.running_id = None
        job.done = True

    def summary(self, job: _Job) -> dict[str, Any]:
        return {
            "job_id": job.id,
            "status": job.status,
            "counts": _counts(job),
            "started_at": job.started,
            "inputs": len(job.inputs),
            "in_flight": self.running_id == job.id,
            "error": job.error,
        }

    def history(self, limit: int = 50) -> list[dict[str, Any]]:
        return [self.summary(self.jobs[j]) for j in self.order[-limit:][::-1]]

    def detail(self, job: _Job) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        if job.report is not None:
            for idx, result in enumerate(job.report.results):
                items.append({
                    "input": job.inputs[idx] if idx < len(job.inputs) else "",
                    "tweet_id": result.ref.tweet_id,
                    "author": result.ref.screen_name or "",
                    "canonical_url": result.ref.canonical_url,
                    "status": result.status,
                    "detail": result.detail,
                    "files": [str(f) for f in result.files],
                    "error": result.error.code.value if result.error else "",
                })
        return {**self.summary(job), "inputs": job.inputs, "items": items}


def _counts(job: _Job) -> dict[str, int]:
    counts = {
        "total": len(job.inputs), "ok": 0, "skipped": 0, "partial": 0, "failed": 0,
    }
    if job.report is not None:
        report = job.report
        counts["ok"], counts["skipped"] = report.ok, report.skipped
        counts["partial"], counts["failed"] = report.partial, report.failed
        return counts
    counts.update(job.counts)  # maintained incrementally in _Job.emit -> O(1)
    return counts


def parse_textarea(raw: str | list[str]) -> tuple[list[str], int]:
    """CLI-file-reader semantics: drop blanks + `#` comments, order-preserving
    dedupe. Returns (urls, duplicate_count)."""
    lines = raw.splitlines() if isinstance(raw, str) else raw
    urls: list[str] = []
    seen: set[str] = set()
    dupes = 0
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped in seen:
            dupes += 1
            continue
        seen.add(stripped)
        urls.append(stripped)
    return urls, dupes


# --------------------------------------------------------------------------- #
# Request models
# --------------------------------------------------------------------------- #

class _UrlsMixin(BaseModel):
    urls: str | list[str]
    model_config = ConfigDict(extra="forbid")


class JobOptions(BaseModel):
    quality: str | None = None  # 'best' | 'worst' | height | 'mp3' (audio-only)
    output: str | None = None
    concurrency: int | None = Field(default=None, ge=1, le=5)


def _batch_quality(raw: str | None) -> tuple[str | None, bool]:
    """The UI's 'MP3 (audio)' select option maps onto the CLI's --audio-only."""
    if raw == "mp3":
        return None, True
    return raw, False


class JobsPost(_UrlsMixin):
    options: JobOptions = JobOptions()


class AnalyzePost(_UrlsMixin):
    pass


# --------------------------------------------------------------------------- #
# App factory
# --------------------------------------------------------------------------- #

def create_app(
    settings: Settings,
    run_batch: RunBatchFn,
    *,
    analyze: AnalyzeFn | None = None,
    list_batches: ListBatchesFn | None = None,
    item_files: ItemFilesFn | None = None,
) -> FastAPI:
    """Assemble the web UI; orchestrator/history/reveal are injected callables."""
    hub = _Hub()
    app = FastAPI(title="xdl", docs_url=None, redoc_url=None, openapi_url=None)
    # Outermost user middleware: normalize proxy absolute-form targets first.
    app.add_middleware(_AbsoluteFormMiddleware)
    app.mount("/static", StaticFiles(directory=str(_UI_DIR / "static")), name="static")

    @app.middleware("http")
    async def _host_guard(
        request: Request, call_next: Callable[[Request], Awaitable[Any]]
    ) -> Any:
        host = (request.headers.get("host") or "").split(":", 1)[0].lower()
        if host not in _ALLOWED_HOSTS:
            return JSONResponse({"error": "host not allowed"}, status_code=403)
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = CSP
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.get("/")
    async def index(request: Request) -> Any:
        history = (
            list(await list_batches(50)) if list_batches is not None else hub.history()
        )
        running = hub.summary(hub.jobs[hub.running_id]) if hub.running_id else None
        return TEMPLATES.TemplateResponse(
            request,
            "index.html",
            {
                "history": history,
                "running": running,
                "max_urls": MAX_URLS,
            },
        )

    @app.post("/jobs")
    async def post_jobs(body: JobsPost) -> JSONResponse:
        urls, dupes = parse_textarea(body.urls)
        if not urls:
            raise HTTPException(400, detail="no URLs parsed from input")
        if len(urls) > MAX_URLS:
            raise HTTPException(
                413, detail=f"too many URLs: {len(urls)} (max {MAX_URLS} per batch)"
            )
        if hub.running_id is not None:
            raise HTTPException(409, detail="a batch is already running; open its page")
        req_settings = _apply_overrides(settings, body.options)
        job = hub.create(
            _Job(
                id="",
                inputs=urls,
                quality=body.options.quality,
                output=body.options.output,
                concurrency=body.options.concurrency,
            )
        )
        quality, audio_only = _batch_quality(body.options.quality)
        req = BatchRequest(
            inputs=urls, settings=req_settings, origin="web",
            quality=quality, audio_only=audio_only,
        )
        hub.spawn(_run(hub, run_batch, req, job))
        return JSONResponse(
            {"job_id": job.id, "accepted": len(urls), "duplicates": dupes},
            status_code=202,
        )

    @app.get("/jobs")
    async def get_jobs() -> dict[str, Any]:
        if list_batches is not None:
            return {"jobs": list(await list_batches(50)), "in_flight": hub.running_id}
        return {"jobs": hub.history(), "in_flight": hub.running_id}

    @app.get("/jobs/{job_id}")
    async def get_job(job_id: str) -> dict[str, Any]:
        try:
            job = hub.jobs[job_id]
        except KeyError as exc:
            raise HTTPException(404, detail="no such job") from exc
        return hub.detail(job)

    @app.get("/jobs/{job_id}/events")
    async def job_events(job_id: str, request: Request) -> StreamingResponse:
        try:
            job = hub.jobs[job_id]
        except KeyError as exc:
            raise HTTPException(404, detail="no such job") from exc

        async def stream() -> AsyncIterator[bytes]:
            queue: asyncio.Queue[tuple[int, dict[str, Any]]] = asyncio.Queue()
            # Register FIRST, snapshot backlog synchronously: every event is
            # delivered exactly once (backlog has seq <= base, queue > base).
            job.subscribers.append(queue)
            backlog = job.replay()
            # Everything emitted so far has seq <= last_seq; anything queued for
            # us afterwards has a strictly greater seq. (Using the last *buffered*
            # seq would be wrong now that the progress tail can be evicted.)
            base_seq = job.last_seq
            for payload in backlog:
                if await request.is_disconnected():
                    return
                yield _sse(payload)
            if job.finished:
                # Attaching to an already-finished job: replay then close. Waiting
                # on an empty queue here would idle for the full ping timeout.
                return
            while True:
                if await request.is_disconnected():
                    return
                try:
                    seq, payload = await asyncio.wait_for(queue.get(), timeout=15)
                except TimeoutError:
                    if job.done:
                        return
                    yield b": ping\n\n"
                    continue
                if seq <= base_seq:
                    continue
                yield _sse(payload)
                if payload["kind"] == EventKind.batch_done.value and job.done:
                    return

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    @app.post("/jobs/{job_id}/retry")
    async def retry_job(job_id: str) -> JSONResponse:
        try:
            job = hub.jobs[job_id]
        except KeyError as exc:
            raise HTTPException(404, detail="no such job") from exc
        if hub.running_id is not None:
            raise HTTPException(409, detail="a batch is already running")
        # report.results aligns 1:1 with job.inputs (both input-ordered; the
        # orchestrator preserves order and we pre-deduped at POST /jobs).
        failed = [
            job.inputs[i]
            for i, r in enumerate(job.report.results or [])
            if r.status == "failed"
        ] if job.report else []
        if not failed:
            return JSONResponse({"job_id": None, "retried": 0, "detail": "nothing failed"})
        quality, audio_only = _batch_quality(job.quality)
        req = BatchRequest(
            inputs=failed, settings=settings, origin="retry",
            quality=quality, audio_only=audio_only,
        )
        retry_job_view = hub.create(
            _Job(id="", inputs=failed, quality=job.quality, output=job.output,
                 concurrency=job.concurrency)
        )
        hub.spawn(_run(hub, run_batch, req, retry_job_view))
        return JSONResponse(
            {"job_id": retry_job_view.id, "retried": len(failed)}, status_code=202
        )

    @app.post("/analyze")
    async def post_analyze(body: AnalyzePost) -> JSONResponse:
        urls, _ = parse_textarea(body.urls)
        if not urls:
            raise HTTPException(400, detail="no URLs parsed from input")
        if len(urls) > MAX_URLS:
            raise HTTPException(413, detail=f"too many URLs (max {MAX_URLS})")
        if analyze is None:
            raise HTTPException(503, detail="analyze is not wired in this build")
        return JSONResponse({"items": await analyze(urls)})

    @app.post("/items/{tweet_id}/reveal")
    async def reveal_item(tweet_id: str) -> JSONResponse:
        if item_files is None:
            raise HTTPException(503, detail="reveal is not wired in this build")
        try:
            paths = [p for p in await item_files(tweet_id) if Path(p).exists()]
        except XdlError as err:
            raise HTTPException(502, detail=err.detail) from err
        if not paths:
            raise HTTPException(404, detail="no downloaded file on disk")
        for path in paths:
            await asyncio.to_thread(_reveal_one, Path(path))
        return JSONResponse({"revealed": paths})

    return app


# --------------------------------------------------------------------------- #
# plumbing
# --------------------------------------------------------------------------- #

def to_card(raw: str, result: ResolvedTweet | XdlError) -> dict[str, Any]:
    """One analyze card for the UI: an error line or resolvable post info.

    The single shaping point for /analyze responses (server wiring and tests
    both go through it, so the card shape lives in exactly one place).
    """
    if isinstance(result, XdlError):
        return {"input": raw, "error": f"{result.code}: {result.detail}"}
    return {
        "input": raw,
        "tweet_id": result.ref.tweet_id,
        "author": result.author.screen_name,
        "text": result.text[:120],
        "poster": next(
            (m.poster_url or m.url_best for m in result.media), None
        ),
        "canonical_url": result.ref.canonical_url,
    }


def _sse(payload: dict[str, Any]) -> bytes:
    return b"data: " + _json.dumps(payload, separators=(",", ":")).encode() + b"\n\n"


def progress_sink(hub: _Hub, job: _Job) -> Callable[[ProgressEvent], None]:
    """run_batch(on_event=...) adapter: feeds the hub's job + SSE queues."""
    def on_event(event: ProgressEvent) -> None:
        job.emit(event)
        if event.kind is EventKind.batch_done:
            hub.finish(job)
    return on_event


def _apply_overrides(base: Settings, options: JobOptions) -> Settings:
    """Options row maps 1:1 onto BatchRequest fields (spec): copy Settings."""
    new = base.model_copy(deep=True)
    if options.output:
        new.storage.root = Path(options.output)
    if options.concurrency:
        new.engine.concurrency = options.concurrency
    return new


async def _run(hub: _Hub, run_batch: RunBatchFn, req: BatchRequest, job: _Job) -> None:
    try:
        report = await run_batch(req, on_event=progress_sink(hub, job))
        job.report = report
        counts = _counts(job)
        if counts["failed"] or counts["partial"]:
            job.status = "partial" if counts["ok"] or counts["skipped"] else "failed"
        else:
            job.status = "ok"
    except Exception as err:
        job.error = f"{type(err).__name__}: {err}"
        job.status = "failed"
    finally:
        hub.finish(job)


def _reveal_one(path: Path) -> None:
    """OS file-reveal (never HTTP file serving): docs technical/10."""
    if sys.platform == "win32":
        subprocess.Popen(["explorer", f"/select,{path}"])
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-R", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path.parent)])
