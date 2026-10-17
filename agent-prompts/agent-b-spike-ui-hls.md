# AGENT B — Spike, HLS & Web UI (parallel lane)

You are building the three deliverables of a private X (Twitter) video downloader
that do **not** depend on the core pipeline existing: the **M1 live spike**
(fixtures + token goldens — this unblocks the other agent, do it FIRST), the
**HLS/ffmpeg module**, and the **M3 local web UI** (built against the frozen,
documented interfaces with a stub orchestrator). The complete plan is in `docs/`.

## Parallel-lane context (read this carefully)

Another agent (**Agent A**) works simultaneously on branch `feat/core` implementing
the core pipeline (M0 scaffold, syndication resolver, download engine, storage,
batch orchestration) in the main checkout. You will not collide because file
ownership is a hard boundary:

**You own (only you create/edit these):**
- `scripts/**` (fixture recorder + spike utilities)
- `tests/fixtures/**` (recorded JSON, `golden_tokens.json`, README)
- `src/xdl/ui/**`, `tests/ui/**`
- `src/xdl/media/hls.py`, `tests/unit/test_hls.py`
- `COORDINATION.md` — your append-only section only

**Never create or edit (Agent A's):** `pyproject.toml`, `src/xdl/**` except your two
spots above (`ui/**`, `media/hls.py`), `tests/**` except your spots, `README.md`,
`agent-prompts/**`.

If your UI tests need a dependency that `pyproject.toml` doesn't declare yet (A owns
that file), install it into your own venv for development and record the need in
`COORDINATION.md` — do not edit the file.

## Setup

- Work in a **separate directory** from Agent A: clone fresh, e.g.
  `git clone https://github.com/ocim128/downloader ../downloader-agent-b`
  (Windows, Git Bash; Python 3.12+).
- Branch: `git checkout -b feat/ui-spike` and stay on it.
- Commits prefixed `[ui]`. Push periodically and at the end:
  `git push -u origin feat/ui-spike`.
- **Never** merge, rebase onto, or push to `main` or `feat/core`.

## Read before writing code

1. `docs/README.md` (orientation + decision log)
2. `docs/05-implementation-plan.md` — M1 spike section, M3 milestone
3. For the spike: `docs/technical/02-media-resolution.md` (token algorithm,
   response shapes, `NOT_EMBEDDABLE` semantics)
4. For HLS: `docs/technical/04-hls-and-ffmpeg.md` (complete spec)
5. For the UI: `docs/technical/10-local-web-ui.md` (endpoints, flows A/B, cards,
   security posture) + `docs/04-project-structure.md` (models,
   `run_batch`/`ProgressEvent` shapes, **Layering rules 1–6**)

## Your priority order

### Priority 1 — M1 live spike (do this FIRST; it unblocks Agent A)

Network access is ALLOWED here (standalone scripts only — never inside `pytest`).

1. `scripts/record_fixture.py`: a **standalone** script (its own token
   implementation per `docs/technical/02` — do not import from `src/`, which may not
   exist yet) that, given a post URL/id, computes the token, calls
   `https://cdn.syndication.twimg.com/tweet-result?id=<id>&lang=en&token=<token>`,
   and saves the JSON response to `tests/fixtures/syndication/<name>.json` with a
   small header (recorded-at, id, source URL).
2. Record fixtures for: normal video post, GIF post, photo-only post, multi-video
   post, tombstone (`TweetTombstone`), **sensitive/age-gated video post** (expect
   404/empty — that IS the fixture: save the failure shape), deleted post (404),
   HLS-only post if you can find one. Find candidate posts via web search of public
   accounts; if a category is genuinely unfindable (e.g. no fresh upload at spike
   time), note it in `tests/fixtures/README.md` and move on.
3. Token goldens: for ~10 real tweet ids, compute the token and **verify each with a
   live GET (expect HTTP 200)**. Compute via Node if available:
   `node -e "console.log(((Number(process.argv[1])/1e15)*Math.PI).toString(36).replace(/(0+|\.)/g,''))" <id>`
   otherwise port the exact f64→base-36 algorithm from
   `docs/technical/02-media-resolution.md`. Known-good anchor: id `"20"` →
   `"6dq1a2xwd9"` (verified 2026-10-17). Save
   `tests/fixtures/golden_tokens.json` (`[{"id": "...", "token": "..."}]`) —
   Agent A's `test_token.py` will consume it.

**Acceptance:** fixtures + goldens committed on your branch, `README.md` in
`tests/fixtures/` documents each file and how it was obtained, and a note is in
`COORDINATION.md` telling Agent A they're ready.

### Priority 2 — `src/xdl/media/hls.py`

Per `docs/technical/04`: master-playlist parsing (`#EXT-X-STREAM-INF`
BANDWIDTH/RESOLUTION), child-playlist selection, the progressive-first selection
helper (HLS only when no MP4 or best MP4 < 720p), DRM detection (`EXT-X-KEY` method
≠ AES-128 → `DRM_UNSUPPORTED`), and the ffmpeg argv builder (`-c copy`,
`+faststart`, `-nostdin`, UA header, `.part` output path). Unit tests in
`tests/unit/test_hls.py` using fixture `.m3u8` text files; **assert on argv arrays —
no live ffmpeg runs** (one opt-in `@pytest.mark.live` smoke test is fine).

### Priority 3 — M3 web UI (against the frozen contract)

`src/xdl/ui/` per `docs/technical/10-local-web-ui.md`:

- `app.py`: `create_app(settings, run_batch) -> FastAPI` with all documented
  endpoints (`GET /`, `POST /jobs`, `POST /analyze`, `GET /jobs`,
  `GET /jobs/{id}`, `GET /jobs/{id}/events` SSE, `POST /jobs/{id}/retry`,
  `POST /items/{tweet_id}/reveal`), Flow A (auto-batch) and Flow B
  (Analyze-then-pick), queue cards (thumbnail/@handle/snippet/per-item status) and
  toolbar.
- Security posture exactly as specced: binds `127.0.0.1` only and **hard-fails**
  otherwise; `Host`-header check (403 otherwise); CSP `default-src 'self'`;
  500-URL cap per `POST /jobs`; no directory listing of the downloads tree.
- `templates/index.html` (Jinja2) + `static/` minimal CSS/JS with htmx-style SSE
  consumption. No build step, no npm.
- Tests in `tests/ui/test_serve.py` using FastAPI's `TestClient` and a
  **`StubOrchestrator`** (`tests/ui/stub_orchestrator.py`) that implements the
  documented `run_batch(BatchRequest) -> BatchReport` + `ProgressEvent` shapes from
  `docs/04-project-structure.md`. This is the key to parallelism: you code against
  the documented contract, not Agent A's branch. Leave a marked TODO: swap stub →
  real core after integration.

**Acceptance:** the plan's M3 acceptance criteria, with "paste 5 URLs in the
browser" demonstrated against the stub; all UI tests hermetic and green.

## Hard rules

- **Tests are hermetic:** no network and no live ffmpeg inside `pytest`. Network is
  allowed only in `scripts/**` (the spike).
- `ruff` + `mypy` clean on the files you own.
- Layering rules 1–6 from `docs/04`: `ui/` is a frontend — it imports
  `app`/`config`/`report` shapes only (via the injected `run_batch` callable; the
  stub exists precisely so you don't need A's code).
- Do not silently deviate from specs: smallest sensible choice + note in
  `COORDINATION.md` under `## Agent B notes`.

## Finish line for this session

1. Priority 1 fully complete (it unblocks the other lane).
2. Priorities 2–3 as far as you get, each stopping point green.
3. Handoff note in `COORDINATION.md` under `## Agent B notes`; push `feat/ui-spike`;
   print a final summary (done / tested / deferred).
