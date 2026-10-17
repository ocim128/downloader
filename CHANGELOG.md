# Changelog

Private, local batch downloader for X (Twitter) video posts. Dates are
2026-10-17 (three implementation rounds merged into `main`).

## [Unreleased] — 2026-10-17

### Fixed

- **The web UI 404s behind a forward proxy.** A proxy forwards loopback requests in
  RFC 9112 absolute-form (`POST http://127.0.0.1:7788/jobs`). uvicorn's h11 parser
  leaves the whole URI in `scope["path"]`, so the first request on a connection works
  and every subsequent one 404s. `_AbsoluteFormMiddleware` now rewrites loopback
  absolute-form targets before routing; foreign authorities are left untouched and
  still hit the Host guard. Found via `scripts/ui_smoke.py` on a machine with
  `HTTP_PROXY` set. Regression tests: 5 in `tests/ui/test_serve.py`.
- **Request pacing did nothing under concurrency.** `ThrottleState.gate()` read
  `_last_request`, awaited, then wrote it — so every waiter read the same stale
  timestamp, slept the same duration and fired in a burst (measured: 3 of 4
  concurrent callers fired in the same millisecond). With `resolve_concurrency=4`
  and `concurrency=3` that was the default path, leaving only the absolute 429
  cooldown effective. The slot is now reserved *before* the await. New regression
  test asserts the calls actually space out.
- **SSE streams idled 15 s on an already-finished job.** The backlog was replayed
  and then the handler blocked on an empty queue until the ping timeout, so opening
  a completed job (or any test that POSTs then connects) waited the full 15 s. Now
  closes right after the replay. This alone took the suite from **30.8 s → 13 s**
  (`test_stub_sse_exact_tick_order` was 15.02 s = 48% of the run).
- **`analyze()` and `_normalize_inputs()` resolved strictly serially.** `POST /analyze`
  accepts up to 500 lines, so a large paste held one HTTP request open for minutes
  (measured: 1.21 s for 20 URLs at 50 ms/request — perfectly linear). Both now fan
  out over `resolve_concurrency`; `gather` keeps the 1:1 input ordering.
- **t.co expansion was the one unpaced request path.** `_normalize_inputs` called
  `normalize_url` without consulting the throttle. `normalize()` now takes a `gate`
  callable and awaits it around the only network hop.
- **The transfer stage held a resolve slot.** `resolve_sem` wrapped the whole item,
  including downloads, so effective resolve parallelism collapsed to
  `resolve_concurrency - concurrency` (1 with defaults) and a transfer stalled in a
  429 cooldown blocked every pending resolve. `_prepare_item` (resolve+select) and
  `_download_item` (transfer) now hold only their own semaphore. Verified by a test
  that fails under the old nesting.
- **`ok_rows()` was re-queried per media file** although the caller had already built
  the index map (4 redundant queries on a 4-video post); `seen_complete()` cost two
  statements and a third for the count. Now one statement, and the row is threaded
  through.
- **Retries were invisible.** A clean transfer and one that needed three attempts
  produced identical reports. `_with_retries` gained `on_attempt`; `MediaOutcome`
  gained `attempts`/`elapsed_ms` and `ItemResult` gained `elapsed_ms`. The summary
  table shows per-item time, and `detail` appends `(N attempts)` when N > 1.
- **One bad item could abort the whole batch.** Two ways a non-`XdlError` escaped an
  item and unwound through `asyncio.gather`:
  1. `_acquire_hls.fetch_text` calls the shared client directly; an `httpx` transport
     error while fetching an m3u8 escaped every `XdlError` handler and killed the run.
     Now mapped to `NETWORK`.
  2. Any *unmapped* exception type (a bug, an unexpected `OSError`) did the same.
     `run_batch` now wraps each item in a containment boundary: escaping `XdlError`s
     become that item's failure, anything else becomes `UNEXPECTED` (exception type in
     the detail), the breaker records it, and the batch continues.
  Regression tests: `test_hls_manifest_transport_error_fails_item_not_batch`,
  `test_unexpected_item_exception_is_contained`.
- **HLS master playlist was fetched twice** per upgraded item (once by the upgrade
  probe, again by the remux). `download_hls(master_text=...)` reuses the probe's
  fetch.

### Changed

- **SQLite runs `synchronous=NORMAL`** (was the `FULL` default): ~1.05 ms → ~0.02 ms
  per commit, measured, and every write happens on the event-loop thread. Safe under
  WAL — a crash can lose the last transaction but never corrupts; the idempotent
  replay path already covers a missing state row. `busy_timeout` is now explicit.
- **Web-UI job retention is bounded.** Progress ticks live in a 200-entry ring
  buffer instead of the replay backlog (a 500-item batch buffered 41k events / 6.8 MB
  and replayed all of it on every reconnect), item counters are maintained
  incrementally, and the hub keeps the last 50 finished jobs.
- **Batch tasks keep a strong reference** (`_Hub.spawn`) instead of a bare
  `create_task` with `# noqa: RUF006`.
- **`pytest` is hermetic by default**: `addopts = "-m 'not live'"`. A bare `pytest`
  no longer hits `cdn.syndication.twimg.com`; `pytest -m live` opts in.
- **CI added** (`.github/workflows/ci.yml`): ruff + mypy on Linux, pytest on
  Linux **and Windows**, nightly live-endpoint job (non-blocking).
- `httpx[http2]` → `httpx`: the `h2` extra was installed while `build_client()`
  hard-codes `http2=False`.
- `Settings.app_dir` moved to a module-level `config.app_dir()` so loading
  `config.toml` no longer constructs a `Settings` model first.

### Removed (complexity audit, round 3)

- **Logging subsystem deleted**: `report/logging.py`, `LogCfg`, the `-v/-vv`
  flags, `tests/report/test_logging.py`, and the README/config-template log
  sections. The handlers were installed but no module ever emitted a log
  record, so the flags and the "always-on rotating file log" did nothing
  (DECISIONS.md "Deliberately not built").
- **Phantom per-item MP3**: `JobItemOverride`, `JobsPost.items`,
  `_Job.item_formats`, and the analyze-card MP3 pill are gone. The UI parsed
  and stored per-item formats, then built a batch-level `BatchRequest` — the
  pill silently downloaded video. `quality: "mp3"` remains as the batch-level
  audio-only choice.
- **`rebuild_from_disk`** and its test — a state-recovery method with no CLI or
  UI surface.
- **Dead symbols**: `record_many`, `next_available`, `snowflake_plausible`,
  `parse_progress_frames`, `target_duration`, `LinkExpiredError`,
  `expected_size`, the sha256 sidecar/schema plumbing, `DEFAULT_UA` /
  `GOOGLEBOT_UA`, `downloader._retry_after`, `ConsoleReporter(quiet=...)`.
- **Dead config knobs**: `resolver.ytdlp_mode`, `resolver.googlebot_retry`,
  `engine.chunk_kib`, the whole `[log]` section. The template now documents 14
  honest keys instead of ~20.
- `.tmp_candidates.json` untracked and gitignored.

### Changed

- **One analyze implementation**: `ui/__main__` and `tests/ui/real_core.py`
  both call `xdl.app.analyze` + the single `ui.app.to_card` shaper. The UI
  analyze path gains the shared throttle and typed error mapping it had
  bypassed.
- **`DownloadJob` → `fetch(url, dest, client, ...)`**: the engine read two of
  the model's eight fields and imported resolver types for the rest.
- **`Settings.load` runs once per CLI invocation**; all flags go through the
  one overrides dict instead of mutating `Settings` after load.
- **`POST /jobs/<id>/retry` returns 202 immediately** (`create_task`), matching
  `POST /jobs`; the HTTP response no longer blocks on the retry batch.
- **One host allowlist** in `urls.py` (`_HOSTS` interpolated into every regex);
  `syndication` reuses `throttle.parse_retry_after` (HTTP-date support
  included) instead of its numeric-only stub.
- **Docs**: `COORDINATION.md` and `agent-prompts/` archived under
  `docs/archive/`; durable decisions distilled into `DECISIONS.md`. Technical
  docs no longer advertise logging, `--checksum`, `rebuild_from_disk`,
  `ytdlp_mode`, `googlebot_retry`, or `chunk_kib`.
- **Tests flattened**: `tests/adapters/`, `tests/config/`, `tests/report/`
  folded into `tests/unit/`.
- **Default output filename is now the bare tweet id** (`feat/naming-by-id`):
  `filename_template` default `{date}_{id}` → `{id}` in both `StorageCfg` and
  `render_path`. `xdl <url>` now writes `downloads/x/<author>/<id>.mp4`
  (e.g. `.../Rainmaker1973/2111083867478368447.mp4`) — an id-named file maps
  1:1 to its source URL. Multi-video `_v{index}` suffix unchanged, now on the
  bare id; sidecar follows the media name automatically. `{date}_{id}` and
  the other template variables remain available via `config.toml`. **No
  migration**: skip logic reuses the `file_path` stored in `state.db`, so
  posts downloaded under the old names keep skipping; old- and new-named
  files coexist by design.

## [0.1.0] — round 2, 2026-10-17

### Core (rounds 1-2, `feat/core` → main)

- **Orchestrator** (`xdl.app`): `run_batch(req, *, client=None, on_event=None)
  -> BatchReport` — normalize → dedupe → resolve → select → download →
  finalize with state-based skip, idempotent replay of crash-interrupted
  items, and one-shot `LINK_EXPIRED` re-resolve recovery. Semaphore-bounded
  item-task concurrency (resolve 4 / download 3) — deviation from the queue+
  workers letter, semantics identical (COORDINATION.md deviation 3).
- **Resolvers**: syndication fast-path with a **byte-exact V8 port of the
  token algorithm** (`DoubleToRadixStringView`; 3000-id sample verified vs
  Node 22, 13/13 golden tokens), request-shape assertions; optional
  fxtwitter fallback. `NOT_EMBEDDABLE`→fall-through chain semantics.
- **Engine**: streaming GET, Range-resume from `.part`, 200-to-ranged-restart,
  unknown-length tolerance (no false `INTEGRITY_MISMATCH`), 403→
  `LinkExpiredError`, 429→`ThrottleState.report_429`, fsync-before-rename.
- **Storage**: deterministic Windows-safe naming (`by-author|flat|by-date`
  layouts), `<name>.meta.json` sidecars (atomic), SQLite state store with
  per-media rows + batches; completeness = ok-rows == downloadable media
  count.
- **CLI**: `xdl [OPTIONS] URLS...` default command (single-command typer app +
  group dispatch — deviation 11), `-f/--file`/stdin, batching flags, exit
  codes 0/1/2, rich runner progress + summary table, `xdl retry-failed`.

### Web UI (round 1 + round 2)

- `python -m xdl.ui` — localhost-only FastAPI app (hard non-loopback bind
  guard, Host-header 403, CSP, 500-URL cap, one in-flight batch slot),
  paste-box → `POST /jobs` → SSE event stream (exact-once via backlog+seq),
  Flow-B `POST /analyze` cards, `POST /items/<id>/reveal`, per-item retry,
  history list. Round 2: happy-path tests re-driven **against the real
  orchestrator** (fixture-backed `httpx.MockTransport` world), localhost boot
  smoke (`scripts/ui_smoke.py`); stub kept only for fault injection (409,
  crash, 429 storm).

### Config & logging (round 2)

- `config.py` precedence completed: CLI overrides > `XDL_*` env >
  `config.toml` > defaults; mechanical `XDL_<SECTION>__<FIELD>` mapping +
  documented conveniences; absent file tolerated; unknown keys warn and drop
  (stale templates never brick the CLI). App dir is
  `%LOCALAPPDATA%\xdl` via `platformdirs(..., appauthor=False)` (deviation 7).
- `report/logging.py::setup_logging`: rich console handler
  (`-v` info / `-vv` debug), rotating file handler (5 MB × 3) at
  `%LOCALAPPDATA%\xdl\logs\xdl.log` always-on INFO with a cookie-value
  redaction filter. CLI call-site wiring pending on `feat/core-2`.
- Documented template: `docs/config-template.toml`.

### Test surface

- 248 hermetic tests (round-1: 230): UI tier drives the real core via
  injected mock clients; syndication/HLS/URL/token/engine/state suites;
  integration pipeline incl. resume/429/multi-video partial.

### Known filings (round 2, historical — both resolved; see DECISIONS.md)

- Retrying a failed `NO_VIDEO` item reports `skipped` (`state.upsert_tweet`
  records `media_count=0` → `seen_complete()` false-complete).
- Per-item quality/format choices collected by the UI still pass through
  batch-level only (needs a `BatchRequest` per-item surface).
