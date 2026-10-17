# 05 — Implementation Plan

Milestones are vertical slices: each ends with something runnable and manually testable.
Effort estimates assume solo evening/weekend sessions.

> **Status key (round 2, 2026-10-17):** `[x]` done and merged to main · `[~]` partial ·
> `[ ]` not started. Shipped-slice summaries live in `CHANGELOG.md`; cross-lane
> deviations live in `COORDINATION.md` (they supersede this doc where they disagree).

## M0 — Scaffold (0.5 day) — DONE

**Goal**: repo skeleton that lints, types, and tests green with zero features.

- [x] `pyproject.toml` (deps per [03-tech-stack.md](03-tech-stack.md)), `src/` layout,
      entry point `xdl`, `ruff` + `mypy` config, `pytest` wiring.
- [x] `models.py` with the types from [04-project-structure.md](04-project-structure.md).
- [x] `cli.py` with `xdl --version` only (grew the full surface at M2).
- [x] `.gitignore` (downloads/, state.db, .venv), init git.

**Accept**: ✅ `pip install -e ".[dev]"`, `ruff check .`, `mypy`, `pytest` all pass;
`xdl --version` prints. (230 hermetic tests + logging/config suites green at round 2.)

## M1 — Spike + single-URL happy path (1–2 days) ⚠️ highest-uncertainty milestone — DONE

**Goal**: `xdl <url>` writes one best-quality MP4 for a normal public video post.

The **spike** came first (Agent B's M1 spike, recorded 2026-10-17 in
COORDINATION.md; fixtures in `tests/fixtures/`, goldens in
`tests/fixtures/golden_tokens.json`):

- [x] Live-verify `cdn.syndication.twimg.com/tweet-result` — endpoint alive; **token is
      effectively a cache-buster** (any non-empty token → 200; empty → 200 `{}`), but
      shipped V8-exact anyway to stay byte-identical with react-tweet.
- [x] Golden fixtures recorded: normal video (4 URL forms), GIF, photo grids
      (3/4) + single photo, mixed media, tombstone, deleted-404 HTML, media-stripped
      200, text-only, HLS-only (**synthesized** — m3u8-only variants never occur live).
      *Findings worth re-reading*: deleted vs never-existed are indistinguishable;
      sensitive posts are NOT gated at this endpoint; real resolution data lives only
      in HLS RESOLUTION lines, not `video_info.variants`.
- [x] Golden token values (`node scripts/golden_tokens.mjs`, 13 ids incl. `20`, β=
      `6dq1a2xwd93` — the doc's `6dq1a2xwd9` anchor was a stale JS-engine output; locked
      13/13 live-verified, then byte-exact-locked by the V8 `DoubleToRadixStringView`
      port in `resolve/syndication.py`, 3000-id sample 0 mismatches vs Node).
- [x] Endpoint alive — no re-plan needed. (Mismatch watchouts: Python stdlib `urllib`
      gets instant 404s — client fingerprinting exists; use httpx with browser-lib UA,
      never urllib. `timeline-profile/*` 429s all day — don't build on it.)

Then built:

- [x] `urls.py` parser + `t.co` expansion ([technical/01-url-parsing.md](technical/01-url-parsing.md)).
- [x] `resolve/syndication.py` (token fn + mapping per fixtures).
- [x] `media/selector.py` best-bitrate policy (progressive-first; deviation noted).
- [x] `engine/downloader.py` streaming download incl. Range resume (arrived with M2).
- [x] `storage/naming.py` `{date}_{id}.mp4` + `storage/sidecar.py`.
- [x] `report/render.py` progress bars.

**Accept**: ✅ MVP story #2 works on Windows for a fresh public video post (live-verified
during M1/M2); unit tests for parser/token/mapper/selector/naming pass against fixtures.

## M2 — Batch + state + dedup (1 day) — DONE

- [x] `app.run_batch` concurrency wiring — shipped as **semaphore-bounded item tasks**
      (deviation 3); every run records a `batches` row.
- [x] Input from args, `-f file`, stdin; `#` comments, blank-line tolerant.
- [x] `storage/state.py` SQLite schema — skip check `count(ok rows) == media_count` +
      file-exists verification.
- [x] Per-item `ItemResult` collection; summary; exit codes 0/1/2
      ([technical/07-cli-and-config.md](technical/07-cli-and-config.md)).
- [x] `xdl retry-failed`.

**Accept**: ✅ mixed batches (fixtures) yield correct summaries and exit 1; partial
multi-video completion tested in `tests/integration/test_pipeline.py`; live-verified on
Windows: single post + sidecar + state row, second run skips, batch file + t.co +
mirror-host dedup.

## M3 — Local web UI MVP (2 days) — DONE

The x-downloader.com experience, local. Spec:
[technical/10-local-web-ui.md](technical/10-local-web-ui.md).

- [~] `xdl serve` — FastAPI on 127.0.0.1: shipped as `python -m xdl.ui` (CLI alias
      pending, see [technical/07](technical/07-cli-and-config.md) shipped note).
- [x] Paste box → `POST /jobs` → batch row → SSE progress stream.
- [x] Queue cards: thumbnail, `@handle`, text snippet, per-item status/progress.
- [x] Batch toolbar: quality choice, concurrency, output dir, per-item format
      (MP3 gated on M5 ffmpeg paths; per-item quality collection wired, consumption
      pending a `BatchRequest` per-item surface — post-merge).
- [x] History list + per-job detail + per-item retry (real StateStore in the runner).

**Accept**: ✅ round-1 stub-held tests; round 2 re-drives the happy path end-to-end
against the real orchestrator (fixture-backed MockTransport world in
`tests/ui/real_core.py`), plus `scripts/ui_smoke.py` localhost boot smoke over the real
core. Host guard 403s anything non-loopback; core library untouched.

## M4 — Resilience (1–2 days) — in progress (Agent A, `feat/core-2`)

- [ ] Retry with exponential backoff + jitter; `Retry-After` honored
      ([technical/08-errors-and-rate-limits.md](technical/08-errors-and-rate-limits.md)).
- [ ] Global 429 cooldown via `ThrottleState`; resolve pacing jitter.
- [ ] Range-resume on `.part` files — total parsed from `Content-Range`, unknown
      length tolerated (no false `INTEGRITY_MISMATCH`), expired-URL 403 triggers one
      re-resolve (`LINK_EXPIRED`); atomic rename
      ([technical/03-download-engine.md](technical/03-download-engine.md)).
- [ ] Graceful Ctrl+C (finish chunk, flush state, exit 130).
- [ ] Circuit breaker: N consecutive upstream failures → abort remaining, keep summary.
- [ ] `YtdlpResolver` fallback adapter — resolve-only mode, bundled runtime dep
      (round 3), default `fallback = "auto"` (used automatically for
      `NOT_EMBEDDABLE` posts and endpoint breakage)
      (spec: [technical/02-media-resolution.md](technical/02-media-resolution.md)
      Adapter D).

**Accept**: MVP story #4 (simulated 429 via MockTransport); kill mid-download and re-run
resumes (integration test asserts single Range request with correct offset); a mocked
expired-URL 403 recovers via re-resolve without losing the item.

## M5 — Media completeness (1–2 days) — in progress (Agent A, `feat/core-2`; `media/hls.py` + tests + fixtures shipped early, commit c4f164e)

- [ ] HLS: detect m3u8-best, ffmpeg remux, `HLS_REQUIRES_FFMPEG` error with install hint
      ([technical/04-hls-and-ffmpeg.md](technical/04-hls-and-ffmpeg.md)).
- [ ] Multi-video posts → `_v2`, `_v3` outputs.
- [ ] GIF posts → mp4 (X serves gifs as mp4); optional `--gif` real-gif conversion.
- [ ] `--audio-only` MP3 extraction (ffmpeg).
- [ ] `--quality` policy (`best|worst|<=Np`), `--no-meta`, `--force`.

**Accept**: each media kind in the manual matrix
([technical/09-testing.md](technical/09-testing.md)) downloads correctly; ffmpeg-absent
run degrades with clear messages, exit 1.

## M6 — Config & polish (0.5–1 day) — config + logging shipped (Agent B, round 2)

- [x] `config.toml` + env + precedence; `platformdirs` locations
      ([technical/07-cli-and-config.md](technical/07-cli-and-config.md)).
- [x] Filename template options; by-author/flat layouts (shipped at M2).
- [x] Logging to rotating file (`report/logging.py`); console level from `-v/-vv`
      (file always-on INFO + cookie redaction). CLI `main()` wiring: call
      `setup_logging(settings, verbose=...)` — **pending on feat/core-2** (noted in
      COORDINATION.md).
- [x] README refresh (every round).
- [~] CHANGELOG — round-2 initial entry done.
- [ ] `--dry-run` exists end-to-end; needs a live matrix tick once CLI wiring lands.

**Accept**: flags configurable via file+env (tested in `tests/config/`); `--dry-run` is
BatchRequest-native (tests/integration); live matrix tick pending M4/M5 merge.

## M7 — Optional: authenticated GraphQL resolver (2 days, separate decision)

Opt-in own-cookies resolver for protected/followed-only posts, mirroring x-downloader's
"extension reads your session" idea. Highest maintenance burden; see risks. Requires the
cookie-safety policy in [06-risks-and-legal.md](06-risks-and-legal.md) implemented first.

## Later backlog (unordered)

- TikTok resolver adapter (repo theme!) — plug into the same pipeline.
- Watch-folder mode: drop `.url`/`.txt` files, auto-process.
- Playlist/author-page expansion ("all videos from @user") — **needs care** to stay a
  downloader, not a crawler (see non-goals).
- Scheduled re-check of a user's pinned post, etc. — same caution.
- Screenshot rendering (stretch).

## Definition of Done (project-level)

- All milestone acceptance criteria green.
- `ruff` + `mypy --strict` clean; unit+adapter tests hermetic (no network in `pytest`).
- README covers install, quickstart, config, troubleshooting (429/ffmpeg/protected).
- One full manual pass of the test matrix on Windows.
