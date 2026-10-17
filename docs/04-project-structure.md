# 04 — Project Structure

```
downloader/
├── pyproject.toml              # metadata, deps, entry point: xdl
├── README.md                   # user-facing quickstart (written in M1)
├── CHANGELOG.md
├── .gitignore                  # downloads/, state.db, .venv, __pycache__
├── docs/                       # this planning set
│   ├── ...00–06 planning docs...
│   └── technical/              # deep-dive specs
├── src/
│   └── xdl/
│       ├── __init__.py         # __version__
│       ├── cli.py              # typer app; thin — parses, calls app.run_batch, renders
│       ├── app.py              # Orchestrator: run_batch(BatchRequest) -> BatchReport
│       ├── config.py           # Settings (pydantic) + TOML/env loading + precedence
│       ├── models.py           # TweetRef, ResolvedTweet, MediaAsset, DownloadJob,
│       │                       #   ItemResult, BatchReport, error enums
│       ├── urls.py             # parsing, normalization, t.co expansion (sync part)
│       ├── resolve/
│       │   ├── base.py         # Resolver protocol + FallbackChain + error mapping
│       │   ├── syndication.py  # cdn.syndication.twimg.com adapter (+ token fn)
│       │   ├── fxtwitter.py    # api.fxtwitter.com adapter (opt-in fallback)
│       │   └── graphql.py      # authenticated adapter (phase 7; stub until then)
│       ├── engine/
│       │   ├── downloader.py   # streaming GET, Range resume, .part, verify, rename
│       │   ├── pool.py         # worker pool over asyncio.Queue + semaphores
│       │   └── throttle.py     # ThrottleState: pacing jitter, 429 cooldown, backoff
│       ├── media/
│       │   ├── selector.py     # variant filtering per --quality policy
│       │   └── hls.py          # m3u8 detect/parse, ffmpeg remux, segment fallback
│       ├── storage/
│       │   ├── naming.py       # template render + Windows-safe sanitize + collisions
│       │   ├── state.py        # SQLite: schema, upsert, skip lookups, retry-failed
│       │   └── sidecar.py      # meta.json writer (atomic)
│       ├── ui/
│       │   ├── app.py          # FastAPI app: page, /jobs, /analyze, SSE stream,
│       │   │                   #   localhost-only + Host-check guards (core M3)
│       │   ├── templates/      # Jinja2: index.html (paste box, cards, toolbar)
│       │   └── static/         # minimal CSS + vanilla JS / htmx attributes
│       └── report/
│           ├── events.py       # ProgressEvent neutral events (CLI + web UI share)
│           └── render.py       # rich progress bars + summary table
├── tests/
│   ├── conftest.py             # fixtures: Settings, httpx MockTransport, tmp dirs
│   ├── unit/
│   │   ├── test_urls.py        # parametrized parse table (~30 cases)
│   │   ├── test_token.py       # golden token values (captured in M1 spike)
│   │   ├── test_selector.py    # quality policy matrix
│   │   ├── test_naming.py      # Windows reserved names, sanitization, collisions
│   │   ├── test_throttle.py    # backoff math, Retry-After, global pause
│   │   └── test_state.py       # skip logic, retry-failed
│   ├── adapters/
│   │   ├── test_syndication.py # mapping from recorded JSON fixtures
│   │   └── test_fxtwitter.py
│   ├── integration/
│   │   └── test_pipeline.py    # full batch via MockTransport, incl. resume & 429
│   ├── ui/
│   │   └── test_serve.py       # FastAPI TestClient: /jobs, /analyze, SSE, Host guard
│   └── fixtures/
│       ├── syndication/        # *.json recorded responses (video, gif, photo-only,
│       │                       #   tombstone, empty, multi-video)
│       └── fxtwitter/
├── scripts/
│   └── record_fixture.py       # hit live endpoint, save redacted JSON fixture
└── downloads/                  # default output root (gitignored)
```

## Module responsibilities & key APIs

| Module | Responsibility | Key public surface |
|---|---|---|
| `cli.py` | argument parsing, output rendering, exit codes. **No business logic.** | `main()` |
| `app.py` | batch lifecycle, stage wiring, skip decisions, report assembly | `async run_batch(req: BatchRequest) -> BatchReport` |
| `config.py` | single `Settings` object; precedence CLI>env>TOML>defaults | `Settings.load()` |
| `models.py` | every cross-stage data type; frozen dataclasses/pydantic models | see below |
| `urls.py` | raw string → `TweetRef`; `t.co` expansion helper (async, one hop) | `parse(raw: str) -> TweetRef`, `expand_tco(url) -> str` |
| `resolve/*` | `TweetRef -> ResolvedTweet` via adapters | `Resolver` protocol, `SyndicationResolver`, `FallbackChain` |
| `engine/downloader.py` | one file's bytes, resumable, verified | `async fetch(job: DownloadJob, cb: ProgressFn) -> Path` |
| `engine/throttle.py` | pacing + global cooldown shared by resolve & download | `ThrottleState` (`await gate()`, `report_429(ra)`) |
| `media/selector.py` | variants → chosen variant per policy | `select(media: list[MediaAsset], policy: QualityPolicy) -> Chosen` |
| `media/hls.py` | m3u8 → mp4 | `remux(url, dest)`, `needs_ffmpeg(assets) -> bool` |
| `storage/naming.py` | deterministic paths | `render_path(root, tweet: ResolvedTweet, index: int) -> Path` |
| `storage/state.py` | history DB | `seen(tweet_id) -> bool`, `record(row)`, `failed_items()` |
| `ui/app.py` | `xdl serve` FastAPI app: page, `/jobs`, `/analyze`, SSE stream, localhost guards | `create_app(settings, run_batch) -> FastAPI` |
| `report/events.py` | neutral progress events | `ProgressEvent` dataclass stream |

## Core models (illustrative)

```python
class TweetRef(BaseModel):
    tweet_id: str            # numeric string, ≤19 digits
    screen_name: str | None  # from URL if present; not authoritative
    canonical_url: str       # https://x.com/<screen_name or i>/status/<id>

class MediaAsset(BaseModel):
    kind: Literal["video", "gif", "photo"]
    index: int               # position in post (multi-video posts)
    variants: list[Variant]  # video: (url, bitrate, content_type, w, h)
    url_best: str            # convenience for photos

class ResolvedTweet(BaseModel):
    ref: TweetRef
    text: str
    created_at: datetime
    author: Author           # screen_name, display_name
    media: list[MediaAsset]
    source: Literal["syndication", "fxtwitter", "graphql"]

class MediaOutcome(BaseModel):
    index: int               # media position in the post
    status: Literal["ok", "skipped", "failed"]
    file: Path | None
    error: XdlError | None   # per-media failure reason

class ItemResult(BaseModel):
    ref: TweetRef
    status: Literal["ok", "skipped", "partial", "failed"]
    media: list[MediaOutcome]   # one per media asset (round 3: multi-video posts can
                                # finish part-way; "partial" = ≥1 ok AND ≥1 failed)
    error: XdlError | None      # headline error for display (first failure)
    files: list[Path]           # convenience: the ok media files
    detail: str                 # human-readable, shown in summary
```

## Layering rules (enforced in review)

1. Frontends (`cli.py`, `ui/`) import `app`, `config`, `report`; nothing imports them.
2. `app.py` may import everything except `report/render` specifics (it emits
   `ProgressEvent`s; rendering subscribes).
3. Adapters (`resolve/*`) import `models` + `httpx` only — never storage or engine.
4. Storage never performs network I/O; engine never touches SQLite.
5. Everything path-related goes through `pathlib.Path`; string paths are banned in
   `src/` (ruff rule `PTH`).
6. Cross-stage recovery belongs to the orchestrator: the engine raises
   `LinkExpiredError` and never calls a resolver; `app.py` catches it, re-resolves,
   and re-dispatches the `DownloadJob` (round 3 layering fix).
