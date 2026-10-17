# X Batch Video Downloader — Project Plan

A **private, local-only, batch downloader for X (Twitter) video posts**, inspired by
[x-downloader.com](https://x-downloader.com/en/) but built for personal use: no web service,
no third-party servers, no accounts, files saved straight to disk with metadata.

This folder contains the complete planning documentation for building the app from scratch.
No code exists yet — these docs are the blueprint.

## What the app is (one paragraph)

A command-line tool (`xdl`) where you paste one or many X post URLs (a list file, stdin, or
arguments). For each post it resolves the video metadata via X's public syndication API,
picks the highest-quality MP4 variant, downloads it concurrently with progress bars, saves
it under a clean, deterministic filename (e.g. `2026-10-01_1750123456789012352.mp4`) next to
a JSON metadata sidecar, records the download in a local SQLite database so re-runs skip
duplicates, and prints a final summary table. A local web UI (`xdl serve`, localhost
only) that mimics the x-downloader.com paste-and-grab UX is a **core deliverable
(milestone M3)** driving the same pipeline.

## Document map

| Doc | Contents |
|---|---|
| [01-goals-and-scope.md](01-goals-and-scope.md) | Goals, non-goals, user stories, feature mapping vs. x-downloader.com, MVP definition |
| [02-architecture.md](02-architecture.md) | Whole system architecture: pipeline, components, concurrency model, storage, data flow |
| [03-tech-stack.md](03-tech-stack.md) | Language & framework choice with rationale, dependency list, packaging |
| [04-project-structure.md](04-project-structure.md) | Repository layout, module responsibilities, key APIs |
| [05-implementation-plan.md](05-implementation-plan.md) | Milestones M0–M7 with tasks and acceptance criteria |
| [06-risks-and-legal.md](06-risks-and-legal.md) | Risk register, X ToS considerations, cookie handling policy |

### Technical deep-dives (`technical/`)

| Doc | Contents |
|---|---|
| [01-url-parsing.md](technical/01-url-parsing.md) | Every supported URL form, regex, `t.co` expansion, validation |
| [02-media-resolution.md](technical/02-media-resolution.md) | Syndication API (exact token algorithm), fxtwitter fallback, optional authenticated GraphQL |
| [03-download-engine.md](technical/03-download-engine.md) | Async worker pool, retries, resume via Range, integrity checks |
| [04-hls-and-ffmpeg.md](technical/04-hls-and-ffmpeg.md) | HLS/m3u8 handling, ffmpeg remux, GIF and audio extraction |
| [05-storage-naming-dedup.md](technical/05-storage-naming-dedup.md) | Folder layout, filename templates, Windows sanitization, SQLite state schema |
| [06-metadata-sidecar.md](technical/06-metadata-sidecar.md) | JSON sidecar schema |
| [07-cli-and-config.md](technical/07-cli-and-config.md) | Full CLI spec, config file, output UX, exit codes |
| [08-errors-and-rate-limits.md](technical/08-errors-and-rate-limits.md) | Error taxonomy, 429 strategy, circuit breaker |
| [09-testing.md](technical/09-testing.md) | Test pyramid, recorded fixtures, manual test matrix |
| [10-local-web-ui.md](technical/10-local-web-ui.md) | Local web UI spec (core milestone M3) |

## Reading order

1. **01 → 02 → 03**: what we're building, how it's shaped, what it's built with.
2. **05**: the build order (start here when implementing).
3. **technical/**: open the relevant spec when implementing its milestone.

## Key decisions at a glance

- **Python 3.12+**, asyncio-based, single process, no server components.
- **Two first-class frontends, one core**: the `xdl` CLI lands first (M1), and the
  localhost web UI (`xdl serve`) is a core M3 deliverable — both drive the identical
  pipeline (audit round 2 promotion; the brief said "inspired by x-downloader.com").
- **No X account by default**: uses the public syndication CDN endpoint (same one X's own
  embed widgets and Vercel's `react-tweet` use). Authenticated GraphQL resolution
  (own cookies, enables followed/protected posts) is an opt-in later phase.
- **Resolver is a plug-in interface** — the parent repo theme (`tiktok-repost`) suggests a
  TikTok resolver can be added later without touching the pipeline.
- **yt-dlp is the maintenance safety net, not the engine**: resolver chain
  `syndication → yt-dlp` with `fallback = "auto"` — syndication 404/tombstone means
  "not embeddable" (sensitive/age-gated/fresh posts), **not** "post gone", so the chain
  falls through automatically; yt-dlp ships as a standard dependency, so `auto` works
  out of the box.
- **Everything local**: files, SQLite state, config. Nothing phone-home.
- **Windows-first** (primary machine is Windows 11), but cross-platform-clean Python.

## Decision log

- **2026-10-17** — plan created; syndication endpoint live-probed healthy (guest access,
  computed token, default curl UA, HTTP 200 with full JSON).
- **2026-10-17** — external audit reviewed. Its citations matched no file in this repo
  (it described a different/hallucinated doc set: Electron, Turborepo, better-sqlite3,
  code signing — none of which exist here). Its surviving points were adopted: yt-dlp
  promoted to first-class opt-in fallback adapter, fxtwitter privacy warning, Chrome
  ≥127 app-bound-encryption cookie warning, richer phase-2 web-UI queue cards. The
  from-scratch core and CLI-first order are unchanged, per the original brief.
- **2026-10-17 (round 2)** — second audit (this one read the actual docs) returned 8
  findings; all accepted, two with modified framing. Syndication 404/tombstone
  re-mapped to `NOT_EMBEDDABLE` with automatic adapter fallback (it refuses
  sensitive/age-gated/fresh posts, not just deleted ones). Web UI promoted from
  optional M6 to core M3. `batches`/`tweets` tables, `batch_id`, and `media_count`
  added to the state schema (fixes the multi-video partial-failure skip bug).
  Range-resume now parses `Content-Range`, tolerates unknown length, and re-resolves
  on expired-URL 403 (`LINK_EXPIRED`) instead of failing. `asyncio.to_thread` for
  fsync/DB commits on Windows. Directory segments explicitly under sanitization.
  HLS selection is progressive-first (payloads publish no HLS bitrates).
- **2026-10-17 (round 3)** — verification audit confirmed the round-2 fixes; 4 sync
  gaps closed: yt-dlp + fastapi/uvicorn/jinja2 promoted from extras to standard
  runtime deps (a default install must contain the plan's own mitigations);
  `src/xdl/ui/` added to the project tree; `LINK_EXPIRED` recovery explicitly owned by
  the orchestrator (engine raises, never re-resolves — layering rule 6);
  `ItemResult` gains `partial` status + per-media outcomes; web UI documents both
  flows (auto-batch default, Analyze-then-pick). The "(phase 2)" serve label the
  audit flagged was already fixed in round 2.
