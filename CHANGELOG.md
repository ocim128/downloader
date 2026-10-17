# Changelog

Private, local batch downloader for X (Twitter) video posts. Dates are
2026-10-17 (two parallel implementation rounds merged into `main`).

## [Unreleased] — 2026-10-17

### Changed

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

### Known filings (against core lane, see COORDINATION.md)

- Retrying a failed `NO_VIDEO` item reports `skipped` (`state.upsert_tweet`
  records `media_count=0` → `seen_complete()` false-complete).
- Per-item quality/format choices collected by the UI still pass through
  batch-level only (needs a `BatchRequest` per-item surface).
