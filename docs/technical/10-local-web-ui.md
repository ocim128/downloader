# Technical 10 — Local Web UI (Core Milestone M3)

A localhost-only page reproducing the x-downloader.com convenience (paste box →
progress → file) privately. Promoted to a **core milestone** by audit round 2 — the
brief was "inspired by x-downloader.com", so the paste-and-grab UI is a first-class
deliverable, not an afterthought. The core still needs zero changes (that's the
architecture's promise); `xdl serve` is just another frontend over `run_batch`.

## Stack

- **FastAPI** + **uvicorn**, `xdl serve` → binds `127.0.0.1:7788` (port flag).
- Server-rendered **Jinja2** + **htmx** + a sprinkle of vanilla JS. No build step, no
  npm, no SPA framework — it's one page.
- Open browser on start (`webbrowser.open`) unless `--no-open`.

## Why SSE not websockets

Progress is one-directional (server → page). Server-Sent Events over htmx (`hx-sse`)
gives live per-item progress with zero client state machinery, and FastAPI's
`StreamingResponse` makes it trivial.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/` | main page: paste box, options row (quality, output dir override), history table |
| POST | `/jobs` | body: `{urls: string, options?}` → creates batch, returns `{job_id}`; textarea parsed exactly like the CLI file reader (comments/blanks/dedup); optional `items: [{url, quality, format}]` per-item overrides (power the cards' format buttons) |
| POST | `/analyze` | body `{urls}` → **resolve-only, no downloads** (Flow B): returns card data — thumbnail, author, text, and the *actually available* formats per item; in-memory session cache, never persisted (signed URLs expire) |
| GET | `/jobs` | list recent batches from state.db (status counts) |
| GET | `/jobs/{id}` | batch detail: per-item table |
| GET | `/jobs/{id}/events` | **SSE**: `ProgressEvent`s from `run_batch` (same events the CLI renders) |
| POST | `/jobs/{id}/retry` | re-run failed items (maps to retry-failed semantics) |
| POST | `/items/{tweet_id}/reveal` | OS file-reveal of downloaded item (`explorer /select` on Win, `open -R` on mac, `xdg-open` dir on Linux) |

## Page sketch

```
┌─ xdl · local ──────────────────────────────────────────────┐
│ ┌───────────────────────────────────────────┐  [quality ▾] │
│ │ paste URLs, one per line…                 │  [output  ▾] │
│ │                                           │              │
│ └───────────────────────────────────────────┘  [Download]  │
│                                                            │
│ Batch #12 — running            ok 3 · skip 1 · fail 0      │
│  ▸ 2026-10-01_17501…352.mp4   someuser   [██████░░ 74%]   │
│  ▸ 2026-10-01_17501…353.mp4   other      [✓ 12.4 MB]      │
│  ▸ 2026-09-30_17501…349.mp4   third      [✓ skipped]      │
│                                                            │
│ Recent ─────────────────────────────────────────────────── │
│ #11  yesterday   28 ok · 2 fail            [retry fails]   │
└────────────────────────────────────────────────────────────┘
```

## Queue cards & toolbar (the x-downloader.com feel)

Each queue item renders as a **card**, not a table row:

- thumbnail (the resolver's poster URL), author `@handle`, one-line text snippet;
- per-item format buttons: `Best MP4` · `720p` · `480p` · `MP3` — per-item overrides
  applied at enqueue time via the `items` field of `POST /jobs`;
- state chip per card: queued / resolving / downloading (%·speed·ETA) / done (size) /
  failed (error + per-item retry).

Batch toolbar above the list: **Download all — best MP4**, **Download all — MP3**,
concurrency slider (1–5, maps to `--concurrency`), output-directory picker.

## Two interaction flows (round 3)

Format pills cannot be honest before resolution — available qualities are unknown
until the post is resolved. So both flows exist:

- **Flow A — Auto-batch (default)**: paste URLs → click **Download all** → the
  orchestrator resolves and downloads each post at its best quality, streaming live
  progress cards. The toolbar's quality/MP3 choices act as *policies/caps* (e.g.
  "720p" means "at most 720p"), not claims about what exists.
- **Flow B — Inspect, then pick**: paste URLs → click **Analyze** → `POST /analyze`
  resolves metadata without downloading and renders cards with thumbnail, author,
  text, and the **actually available** formats as buttons → clicking one enqueues
  `POST /jobs` with that per-item choice (the `items` field).

## Wiring to the core

- `run_batch` already emits neutral `ProgressEvent`s (report/events.py) — the UI
  subscribes the SSE generator to the same event bus the CLI's rich renderer uses.
- One in-flight batch at a time (personal tool; a second POST while running returns
  409 with a friendly message). No job persistence beyond state.db rows.
- Options mapping: the options row maps 1:1 onto `BatchRequest` fields — nothing new to
  invent server-side.

## Security posture

- **Binds 127.0.0.1 only.** Startup hard-fails if asked to bind 0.0.0.0 (private tool;
  if remote access is ever wanted, that's an explicit re-decision with auth).
- No auth on localhost (browser same-origin is the boundary), but `POST /jobs` caps
  input at 500 URLs/request and the page is `Content-Security-Policy: default-src 'self'`
  so a drive-by page can't script it.
- DNS-rebinding: check `Host` header is `127.0.0.1:7788`/`localhost:7788`, else 403.
- Proxy absolute-form targets: a forward proxy forwards `POST http://127.0.0.1:7788/jobs`.
  RFC 9112 requires servers to accept that form, and uvicorn's h11 parser does not
  normalize it — it lands in `scope["path"]` and every route 404s. A middleware
  rewrites loopback absolute-form targets before routing (foreign authorities are left
  alone and still hit the Host guard).
- No file serving of the downloads tree through HTTP — `reveal` shells out to the OS
  file manager instead (never expose a directory-listing endpoint by accident).

## Dependencies

`fastapi`, `uvicorn`, `jinja2` — **standard runtime dependencies** (round 3: the web
UI is core M3, so a plain `pipx install .` must yield a working `xdl serve`; an
`[ui]` extra would ship a default install missing a core feature). SSE is hand-rolled
on `StreamingResponse` — no `sse-starlet`.
