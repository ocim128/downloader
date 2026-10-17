# AGENT A — Core Pipeline (critical path)

You are implementing the **core** of a private, local batch downloader for X (Twitter)
video posts. The complete, three-times-audited plan already exists in this repo under
`docs/` — your job is Milestones **M0 → M1 → M2** of
[`docs/05-implementation-plan.md`](../docs/05-implementation-plan.md), in that order.
Partial completion is acceptable, but you must deliver in priority order and leave
every touched milestone green and tested.

## Parallel-lane context (read this carefully)

Another agent (**Agent B**) works simultaneously on a **separate branch and separate
directory** on: the M1 live spike (fixtures + token goldens), `media/hls.py`, and the
M3 web UI. You will not collide because file ownership is a hard boundary:

**You own (only you create/edit these):**
- `pyproject.toml`, `README.md` (quickstart section)
- `src/xdl/**` — **except** `src/xdl/ui/**` and `src/xdl/media/hls.py`
- `tests/**` — **except** `tests/ui/**`, `tests/fixtures/**`, `tests/unit/test_hls.py`
- `COORDINATION.md` — your append-only section only

**Never create or edit (Agent B's):**
`src/xdl/ui/**`, `src/xdl/media/hls.py`, `tests/ui/**`, `tests/fixtures/**`,
`tests/unit/test_hls.py`, `scripts/**`, `agent-prompts/**`.

You may **read** B's fixtures (`tests/fixtures/syndication/*.json`,
`tests/fixtures/golden_tokens.json`) once they appear; until then, embed minimal
inline JSON payloads in your adapter tests (tests are hermetic either way — see Hard
rules).

## Setup

- Work in the existing checkout of https://github.com/ocim128/downloader
  (Windows, Git Bash; Python 3.12+; use `uv` or `python -m venv`).
- Create your branch from `main` and stay on it: `git checkout -b feat/core`.
- Commit early and often, message prefix `[core]`. Push the branch periodically and
  at the end: `git push -u origin feat/core`.
- **Never** merge, rebase onto, or push to `main` or `feat/ui-spike`. Integration is
  done by the owner afterwards.

## Read before writing code

1. `docs/README.md` (orientation + decision log)
2. `docs/05-implementation-plan.md` — M0/M1/M2 checklists and acceptance criteria
3. `docs/04-project-structure.md` — repo tree, module APIs, **Layering rules 1–6**
4. Technical specs as you reach them: `docs/technical/01-url-parsing.md`,
   `02-media-resolution.md` (exact token algorithm + `NOT_EMBEDDABLE` chain
   semantics), `03-download-engine.md`, `05-storage-naming-dedup.md` (full SQLite
   schema), `06-metadata-sidecar.md`, `07-cli-and-config.md`, `08-errors-and-rate-limits.md`,
   `09-testing.md`.

## Your priority order

### Priority 1 — M0 scaffold (must be done first, ~half a day)
Per the plan's M0: `pyproject.toml` (`src/` layout, entry point `xdl = xdl.cli:main`,
`requires-python = ">=3.12"`), `ruff` + `mypy` config, `pytest` wiring, `models.py`
with ALL types from `docs/04-project-structure.md`, `cli.py` with `--version` only.
**Acceptance:** `pip install -e ".[dev]"`, `ruff check .`, `mypy src`, `pytest` all
green.

**Critical packaging rule (round-3 decision):** declare ALL standard runtime
dependencies from `docs/03-tech-stack.md` in `pyproject.toml` from the start —
including `yt-dlp`, `fastapi`, `uvicorn`, `jinja2`, which you won't use yet. No
extras may exist. Agent B's lane depends on these being declared.

**Contract rule:** `models.py` and `report/events.py` must match the shapes in
`docs/04-project-structure.md` exactly — Agent B codes UI against those documented
shapes, not against your branch. If you must deviate, record it in
`COORDINATION.md` under `## Agent A notes` immediately, and keep the deviation
minimal.

### Priority 2 — M1 single-URL happy path
Per the plan's M1 (skip the spike items marked as B's): `urls.py` parser + `t.co`
expansion; `resolve/base.py` (Resolver protocol + FallbackChain with
`NOT_EMBEDDABLE` fall-through semantics) and `resolve/syndication.py`; token function
implemented EXACTLY per `docs/technical/02-media-resolution.md` (f64 → base-36
shortest-round-trip, strip `0+` runs and the dot). Known-good golden to lock in
`tests/unit/test_token.py` immediately: tweet id `"20"` → token `"6dq1a2xwd9"`
(live-verified 2026-10-17). More goldens will arrive from B at
`tests/fixtures/golden_tokens.json` — make the test load that file if present.
Also: `media/selector.py` (best-bitrate progressive-first policy),
`engine/downloader.py` (correct-but-basic streaming download, `.part` + atomic
rename, `Content-Range`-aware total per the round-2 spec), `storage/naming.py`
(Windows sanitization rules 1–7 incl. directory segments),
`storage/sidecar.py`, minimal `report/render.py` progress bar.
**Acceptance:** `xdl <url>` downloads a public video post's best MP4 + sidecar on
Windows; parser/token/mapper/selector/naming unit tests pass.

### Priority 3 — M2 batch + state + dedup
Per the plan's M2: `app.py` `run_batch` (queue + semaphores per
`docs/02-architecture.md`), `storage/state.py` with the **full three-table schema**
(`batches`, `tweets` with `media_count`, `downloads` with `batch_id`) and the
`count(ok) == media_count` skip semantics (NOT an any-row check), input from
args/`-f`/stdin, `ItemResult` collection with `partial` status, summary table, exit
codes 0/1/2, `xdl retry-failed`.
**Acceptance:** the plan's M2 acceptance block, including the 3-video-post
partial-completion regression test.

## Hard rules

- **Layering rules 1–6 in `docs/04` are non-negotiable.** In particular: the engine
  never imports a resolver (it raises `LinkExpiredError`; the orchestrator recovers).
- **Tests are hermetic:** no network in `pytest`, ever. Use `httpx.MockTransport` +
  inline JSON or B's fixtures. Live verification is B's spike lane.
- `ruff check .` and `mypy src` clean; `pytest` green at every commit.
- Windows-first: `pathlib` everywhere (ruff `PTH`), no hardcoded `/` paths, no ANSI
  assumptions.
- Error semantics follow `docs/technical/08` exactly (codes, retryability, exit-code
  folding).
- Do not silently deviate from any spec: smallest sensible choice + note in
  `COORDINATION.md`.

## Finish line for this session

1. Priority 1 complete; Priorities 2–3 as far as you get, each stopping point green.
2. Append a handoff note to `COORDINATION.md` under `## Agent A notes`: what's done,
   test status, deviations, what's next for your lane.
3. Push `feat/core`. Print a final summary (done / tested / deferred).
