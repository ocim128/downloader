# 04 — Project Structure

```
downloader/
├── pyproject.toml              # metadata, deps, entry point: xdl
├── README.md                   # user-facing quickstart
├── CHANGELOG.md                # shipped-slice summary per round
├── .gitignore                  # downloads/, state.db, .venv, __pycache__
├── docs/                       # this planning set + technical deep-dives
│   ├── ...00–06 planning docs...
│   ├── technical/              # deep-dive specs
│   └── config-template.toml    # documented config.toml (M6)
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
│       │   ├── syndication.py  # cdn.syndication.twimg.com adapter (+ token fn, V8-exact)
│       │   └── fxtwitter.py    # api.fxtwitter.com adapter (opt-in fallback)
│       ├── engine/
│       │   ├── downloader.py   # streaming GET, Range resume, .part, verify, rename
│       │   └── throttle.py     # ThrottleState: pacing jitter, 429 cooldown, backoff
│       ├── media/
│       │   ├── selector.py     # variant filtering per --quality policy
│       │   └── hls.py          # m3u8 detect/parse, rendition choose, ffmpeg remux
│       ├── storage/
│       │   ├── naming.py       # template render + Windows-safe sanitize + collisions
│       │   ├── state.py        # SQLite: schema, upsert, skip lookups, retry-failed
│       │   └── sidecar.py      # meta.json writer (atomic)
│       ├── ui/
│       │   ├── __main__.py     # python -m xdl.ui: real-core wiring for xdl serve
│       │   ├── app.py          # FastAPI app: page, /jobs, /analyze, SSE stream,
│       │   │                   #   localhost-only + Host-check guards (core M3)
│       │   ├── templates/      # Jinja2: index.html (paste box, cards, toolbar)
│       │   └── static/         # CSS + vanilla JS (app.js, style.css, no-thumb.svg)
│       └── report/
│           ├── events.py       # ProgressEvent neutral events (CLI + web UI share)
│           ├── render.py       # rich progress bars + summary table
│           └── logging.py      # setup_logging: rich console + rotating file (M6)
├── tests/
│   ├── conftest.py             # fixtures: Settings, httpx MockTransport, tmp dirs
│   ├── unit/
│   │   ├── test_urls.py        # parametrized parse table (~30 cases)
│   │   ├── test_token.py       # golden token values + V8-exact lock
│   │   ├── test_selector.py    # quality policy matrix
│   │   ├── test_naming.py      # Windows reserved names, sanitization, collisions
│   │   ├── test_throttle.py    # backoff math, Retry-After, global pause
│   │   ├── test_downloader.py  # resume/200-restart/integrity mechanics
│   │   ├── test_hls.py         # fixture playlists + sim-ffmpeg subprocess cases
│   │   ├── test_state.py       # skip logic, retry-failed
│   │   ├── test_sidecar.py     # meta.json schema
│   │   └── test_scaffold.py    # settings/env sanities
│   ├── adapters/
│   │   ├── test_syndication.py # mapping from recorded JSON fixtures
│   │   └── test_fxtwitter.py
│   ├── integration/
│   │   └── test_pipeline.py    # full run_batch via MockTransport, incl. resume & 429
│   ├── ui/
│   │   ├── test_serve.py       # real-core section + stub fault-injection section
│   │   ├── real_core.py        # fixture-backed MockTransport world (round 2)
│   │   ├── stub_orchestrator.py# fault-injection stub (429 storm, crash, 409, SSE)
│   │   └── README.md
│   ├── config/                 # config precedence (M6, Agent B lane)
│   ├── report/                 # logging setup (M6, Agent B lane)
│   └── fixtures/
│       ├── syndication/        # *.json recorded responses (envelope format, see
│       │                       #   fixtures/README): video, gif, photo grids, mixed
│       │                       #   media, tombstone, 404 page, hls-only (synthesized)
│       ├── hls/                # master/child m3u8 fixtures
│       └── golden_tokens.json  # Node-22 exact token goldens
├── scripts/                    # fixture recording + live probing + UI smoke
│   ├── record_fixture.py
│   ├── find_candidates.py
│   ├── probe_batch.py
│   ├── golden_tokens.mjs
│   ├── timeline_probe.py
│   └── ui_smoke.py             # localhost serve smoke vs real core (hermetic)
└── downloads/                  # default output root (gitignored)
```

The engine's worker-pool module that the original sketch named `pool.py` is
implemented as semaphore-bounded item tasks inside `app.py` (deviation 3 in
COORDINATION.md — semantics identical); the phase-7 authenticated adapter
(`graphql.py`) is not started.

## Module responsibilities & key APIs

| Module | Responsibility | Key public surface |
|---|---|---|
| `cli.py` | argument parsing, output rendering, exit codes. **No business logic.** | `main()` |
| `app.py` | batch lifecycle, stage wiring, skip decisions, report assembly | `run_batch(req: BatchRequest, *, client=None, on_event=None) -> BatchReport` (deviation 2: injectable httpx client for hermetic tests + event subscriber) |
| `config.py` | single `Settings` object; precedence CLI>env>TOML>defaults | `Settings.load(overrides, *, config_path=None, read_env=True)` |
| `models.py` | every cross-stage data type; frozen dataclasses/pydantic models | see below |
| `urls.py` | raw string → `TweetRef`; `t.co` expansion helper (async, one hop) | `parse(raw: str) -> TweetRef`, `normalize(client, raw, *, id_mode=False)` |
| `resolve/*` | `TweetRef -> ResolvedTweet` via adapters | `Resolver` protocol, `SyndicationResolver`, `FallbackChain`, `build_chain(settings, client)` |
| `engine/downloader.py` | one file's bytes, resumable, verified | `async fetch(job: DownloadJob, client, *, cb, throttle) -> Path` |
| `engine/throttle.py` | pacing + global cooldown shared by resolve & download | `ThrottleState` (`await gate()`, `report_429(ra)`) |
| `media/selector.py` | variants → one chosen variant per media asset | `select(media, policy) -> list[Chosen]` (deviation 1: one choice per video/gif asset, not a single) |
| `media/hls.py` | m3u8 → mp4 (pure module; ffmpeg argv/remux, no network) | `parse_master`, `choose_rendition`, `detect_drm`, `download_hls`, `ffmpeg_argv` |
| `storage/naming.py` | deterministic paths | `render_path(root, tweet: ResolvedTweet, index: int, *, layout, template) -> Path` |
| `storage/state.py` | history DB | `seen(tweet_id) -> bool`, `record(row)`, `failed_items()` |
| `ui/app.py` | `xdl serve` FastAPI app: page, `/jobs`, `/analyze`, SSE stream, localhost guards | `create_app(settings, run_batch, *, analyze, list_batches, item_files) -> FastAPI` |
| `report/events.py` | neutral progress events | `ProgressEvent` dataclass stream |
| `report/logging.py` | logging surface (M6) | `setup_logging(settings, *, verbose=0) -> Logger` |

## Core models (as shipped; deviations 4-5 noted inline)

```python
class TweetRef(BaseModel):
    tweet_id: str            # numeric string, ≤19 digits
    screen_name: str | None  # from URL if present; not authoritative
    canonical_url: str       # https://x.com/<screen_name or i>/status/<id>

class MediaAsset(BaseModel):
    kind: Literal["video", "gif", "photo"]
    index: int               # position in post (multi-video posts)
    variants: list[Variant]  # video: (url, bitrate, content_type, w, h)
    url_best: str = ""       # convenience for photos (media_url_https)
    poster_url: str | None   # video poster thumb (web UI / Flow B)

class ResolvedTweet(BaseModel):
    ref: TweetRef
    text: str
    created_at: datetime
    author: Author           # screen_name, display_name, verified (deviation 4)
    media: list[MediaAsset]
    source: Literal["syndication", "fxtwitter", "graphql", "ytdlp"]

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
    files: list[Path]           # convenience: the ok media files (skips stay in
                                # `media` with status "skipped" — deviation in
                                # COORDINATION.md round-1 notes)
    detail: str                 # human-readable, shown in summary

class BatchRequest(BaseModel):   # deviation 5: per-run flags live here, not Settings
    inputs: list[str]            # raw (unnormalized); normalize+dedup inside run_batch
    settings: Settings
    origin: Literal["cli", "web", "retry"] = "cli"
    id_mode: bool = False        # bare numeric arguments are tweet ids (--id)
    force: bool = False          # re-download even if state says complete
    no_meta: bool = False        # skip sidecar JSON
    quality: str | None = None   # 'best' | 'worst' | max height number
    dry_run: bool = False        # resolve + print planned paths, download nothing
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
