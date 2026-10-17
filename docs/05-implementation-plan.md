# 05 — Implementation Plan

Milestones are vertical slices: each ends with something runnable and manually testable.
Effort estimates assume solo evening/weekend sessions.

## M0 — Scaffold (0.5 day)

**Goal**: repo skeleton that lints, types, and tests green with zero features.

- [ ] `pyproject.toml` (deps per [03-tech-stack.md](03-tech-stack.md)), `src/` layout,
      entry point `xdl`, `ruff` + `mypy` config, `pytest` wiring.
- [ ] `models.py` with the types from [04-project-structure.md](04-project-structure.md).
- [ ] `cli.py` with `xdl --version` only.
- [ ] `.gitignore` (downloads/, state.db, .venv), init git.

**Accept**: `uv pip install -e ".[dev]"`, `ruff check .`, `mypy src`, `pytest` all pass;
`xdl --version` prints.

## M1 — Spike + single-URL happy path (1–2 days) ⚠️ highest-uncertainty milestone

**Goal**: `xdl <url>` writes one best-quality MP4 for a normal public video post.

The **spike** comes first, because everything downstream depends on the syndication
endpoint behaving as documented (see
[technical/02-media-resolution.md](technical/02-media-resolution.md)):

- [ ] Live-verify `cdn.syndication.twimg.com/tweet-result` with the token algorithm;
      capture the current `features` parameter list from the react-tweet source.
      (Pre-verified 2026-10-17: guest HTTP 200 on id 20 with the computed token and a
      default curl UA — still confirm video posts, sensitive posts, batch behavior.)
- [ ] Record golden fixtures for: normal video post, GIF post, photo-only post,
      multi-video post, tombstone, deleted (404), **sensitive/age-gated video post
      (expect syndication `NOT_EMBEDDABLE`; confirm the yt-dlp adapter resolves it)**,
      fresh (< 1 h) upload — via `scripts/record_fixture.py`.
- [ ] Capture golden token values (JS `getToken` output for ~10 ids) to lock the Python
      port with exact-match tests.
- [ ] If the endpoint is dead/changed: stop, re-plan resolver (fxtwitter primary,
      graphql). This is the plan's one deliberate checkpoint gate.

Then build:

- [ ] `urls.py` parser + `t.co` expansion (spec:
      [technical/01-url-parsing.md](technical/01-url-parsing.md)).
- [ ] `resolve/syndication.py` (token fn + mapping per fixtures).
- [ ] `media/selector.py` best-bitrate policy.
- [ ] `engine/downloader.py` naive-but-correct streaming download (no resume yet).
- [ ] `storage/naming.py` basic `{date}_{id}.mp4` + sidecar JSON.
- [ ] Minimal `report/render.py` progress bar.

**Accept**: MVP story #2 works on Windows for a fresh public video post; unit tests for
parser/token/mapper/selector/naming pass against fixtures.

## M2 — Batch + state + dedup (1 day)

- [ ] `app.run_batch` queue/semaphore wiring (concurrency model per
      [02-architecture.md](02-architecture.md)); every run records a `batches` row.
- [ ] Input from args, `-f file`, stdin; `#` comments, blank-line tolerant.
- [ ] `storage/state.py` SQLite schema — `downloads` + `tweets` (with `media_count`) +
      `batches` (with `batch_id`) — skip check `count(ok rows) == media_count` +
      file-exists verification.
- [ ] Per-item `ItemResult` collection; summary table; exit codes (0/1/2 per
      [technical/07-cli-and-config.md](technical/07-cli-and-config.md)).
- [ ] `xdl retry-failed`.

**Accept**: MVP stories #1 and #3 pass; a 10-URL mixed batch (fixtures) yields correct
summary and exit code 1 with one poisoned item; a 3-video post with 1 file already `ok`
downloads exactly the missing 2.

## M3 — Local web UI MVP (2 days)

The x-downloader.com experience, local. Spec:
[technical/10-local-web-ui.md](technical/10-local-web-ui.md).

- [ ] `xdl serve` — FastAPI on 127.0.0.1, Jinja2 + htmx, opens browser.
- [ ] Paste box (multi-URL textarea) → `POST /jobs` → batch row → SSE progress stream.
- [ ] Queue cards: thumbnail, `@handle`, text snippet, per-item status/progress/ETA.
- [ ] Batch toolbar: "Download all — best MP4", concurrency slider, output dir
      (MP3 button visible but gated on M5 ffmpeg, with a clear tooltip).
- [ ] History list from `batches` + per-batch detail + per-item retry.

**Accept**: MVP story #6 passes — paste 5 URLs in the browser, watch 5 progress bars,
files land with sidecars; failed items show error + retry; refuses any non-localhost
bind; core library unchanged from M2.

## M4 — Resilience (1–2 days)

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

## M5 — Media completeness (1–2 days)

- [ ] HLS: detect m3u8-best, ffmpeg remux, `HLS_REQUIRES_FFMPEG` error with install hint
      ([technical/04-hls-and-ffmpeg.md](technical/04-hls-and-ffmpeg.md)).
- [ ] Multi-video posts → `_v2`, `_v3` outputs.
- [ ] GIF posts → mp4 (X serves gifs as mp4); optional `--gif` real-gif conversion.
- [ ] `--audio-only` MP3 extraction (ffmpeg).
- [ ] `--quality` policy (`best|worst|<=Np`), `--no-meta`, `--force`.

**Accept**: each media kind in the manual matrix
([technical/09-testing.md](technical/09-testing.md)) downloads correctly; ffmpeg-absent
run degrades with clear messages, exit 1.

## M6 — Config & polish (0.5–1 day)

- [ ] `config.toml` + env + precedence; `platformdirs` locations
      ([technical/07-cli-and-config.md](technical/07-cli-and-config.md)).
- [ ] Filename template options; by-author/flat layouts.
- [ ] Logging to rotating file; `--verbose/-vv`; `--dry-run`.
- [ ] README (user-facing), CHANGELOG.

**Accept**: all flags configurable via file; `--dry-run` prints planned paths and touches
nothing.

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
