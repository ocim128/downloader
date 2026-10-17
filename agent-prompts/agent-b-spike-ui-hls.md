# AGENT B — Round 2: Real-Core Integration, Config & Docs (M6)

You are continuing a **working project** — not starting one. `main` already contains
M0–M3 (core CLI, resolvers with V8-exact token, engine, your `media/hls.py`, and the
web UI you built last round). 230 hermetic tests green; ruff + mypy strict clean.

**Your lane: swap the UI onto the real core, finish M6 (config precedence +
logging), reconcile the docs with shipped reality, and run the manual test matrix.**
Partial completion is fine; deliver in priority order, green at each stop.

## Before anything else

1. Read `COORDINATION.md` end to end — both round-1 handoffs (your own notes
   included: hls.py API, live findings, UI TODOs; and Agent A's 12 deviations +
   frozen `run_batch`/`ProgressEvent` contract you already coded against).
2. Skim `docs/technical/07-cli-and-config.md` (config precedence + logging spec),
   `docs/07`-era deviations in A's notes (esp. #7: app dir is
   `%LOCALAPPDATA%\xdl` via `appauthor=False`), `docs/06-risks-and-legal.md` (cookie
   policy — for context only), and `docs/09-testing.md` (your matrix).
3. **Doc-supersession rule:** code + `COORDINATION.md` beat `docs/` where they
   disagree (token algorithm, `select()` shape, `run_batch` kwargs, etc.).

## Parallel-lane context

Agent A works simultaneously on branch `feat/core-2` (main checkout) doing M4
(retries, 429 throttle, circuit breaker, Ctrl+C, LINK_EXPIRED recovery,
YtdlpResolver) and M5 (HLS wiring, MP3/GIF, `--force`/`--no-meta`) plus small
surface items (`xdl.app.analyze`, `xdl serve`, `xdl config` subcommands).
**File ownership is a hard boundary:**

- **You own:** `src/xdl/ui/**`, `tests/ui/**`, `src/xdl/config.py`,
  `src/xdl/report/logging.py` (new file), `README.md`, `CHANGELOG.md` (new),
  `docs/**` (reconciliation edits only — see Priority 3), `scripts/**`,
  `tests/fixtures/**`, your `COORDINATION.md` section.
- **Never edit (A's):** `pyproject.toml`, `src/xdl/{__init__,app,cli,models,urls}.py`,
  `engine/**`, `resolve/**`, `media/**`, `storage/**`, `report/{events,render}.py`,
  `tests/{unit,adapters,integration}/**`, `tests/conftest.py`.
- Bugs found in A's files: record under `## Agent B notes` with a reproducing
  command; fix only within your own files.

## Your priority order

### Priority 1 — UI on the real core

- [ ] In `tests/ui/test_serve.py`, drive `create_app` with the **real**
      `xdl.app.run_batch` (+ real `urls`/`build_chain` where injected) instead of
      `stub_orchestrator` for the happy-path/endpoint tests. Keep the stub **only**
      for fault-injection cases (429 storm, mid-batch crash), clearly marked.
      Real-core tests must stay hermetic — inject an `httpx.MockTransport` client
      via `run_batch(..., client=...)` (A's round-1 note documents the kwarg).
- [ ] Boot smoke: `python -m xdl.ui --no-open` against real core, drive one
      localhost batch with fixture-backed responses, verify SSE order + summary.
      Record the recipe in `tests/ui/README.md` (new, yours).
- [ ] Close your round-1 UI TODOs that live in your lane: per-item
      quality/format collection already feeds `POST /jobs` — keep collecting;
      consumption lands when A/Batch surface supports it (post-merge note).

### Priority 2 — M6 config & logging (your files only)

- [ ] `config.py`: full precedence **CLI overrides > `XDL_*` env > `config.toml` >
      defaults** per `docs/technical/07` (mechanical `XDL_` + `__` nesting mapping);
      TOML at `platformdirs.user_config_dir("xdl", appauthor=False)`; tolerate
      absent file; unknown-key warning. Keep `Settings.load(overrides)` signature —
      A's CLI depends on it.
- [ ] `src/xdl/report/logging.py`: `setup_logging(settings)` — rich console handler
      (level from `-v/-vv`), rotating file handler (5 MB × 3) at
      `<appdir>/logs/xdl.log` always-on INFO, cookie-value redaction guard.
      A wires the call into `cli.main()` from their lane; if their branch hasn't
      landed when you finish, note it in `COORDINATION.md` (one-liner).
- [ ] `config.toml` documented template (comments, matching `docs/technical/07`)
      generated/written to `docs/` or README — you own both.

### Priority 3 — docs reconciliation (make docs match shipped reality)

- [ ] `docs/04-project-structure.md`: sync model sketches to shipped code (A's
      deviations 1–5: `select() -> list[Chosen]`, `run_batch` kwargs,
      `Author.verified`, `BatchRequest.quality/dry_run`, semaphore model).
- [ ] `docs/02-architecture.md`: semaphore-bounded item tasks (A's deviation 3);
      `docs/technical/07`: shipped CLI surface (`xdl [OPTIONS] URLS...` default
      command, subcommands).
- [ ] `docs/05-implementation-plan.md`: tick M0–M3 checkboxes, annotate M1 spike
      outcomes (HLS-only synthesized, token goldens 13/13, sensitive-served
      finding).
- [ ] `README.md`: refresh status + quickstart for what exists now (you own it this
      round); `CHANGELOG.md`: initial entry (M0–M3 summary + field findings).
- [ ] Do NOT touch the superseded banners in `docs/technical/02` and `04` — they
      are correct as-is.

### Priority 4 — manual test matrix (`docs/technical/09`)

Run every runnable row on Windows against the real CLI (use
`scripts/find_candidates.py --check <id>` to source fresh public posts; network use
allowed here — never in pytest). Record results per row in `COORDINATION.md`
(pass/fail/blocked + why). Known-live constraints from your round-1 spike still
apply: httpx only (never urllib), ~1 req/s pacing, `timeline-profile` endpoint is
dead (429) — don't build on it.

## Hard rules (unchanged from round 1)

- `pytest` hermetic — no network, no live ffmpeg, no live browser inside tests.
- `ruff` + `mypy` clean on files you own; full `pytest` green at every commit.
- Layering rules 1–6 (`docs/04`): `ui/` and `config.py` import `app`/`models`/
  `report` shapes only; no engine/resolver internals in the UI layer.
- Deviations from specs: smallest sensible choice + note under `## Agent B notes`.

## Setup & git

- Work in your separate clone `../downloader-agent-b` (Windows, Git Bash):
  `git fetch origin && git checkout main && git pull && git checkout -b feat/ui-2`
  and stay on it. Fresh `pip install -e ".[dev]"`.
- Commits prefixed `[ui2]`; push `-u origin feat/ui-2` at the end (and
  periodically). **Never** merge/rebase onto `main` or `feat/core-2`.

## Finish line

1. Priority 1 fully done; 2–4 as far as you get, green at each stop.
2. Handoff under `## Agent B notes`: done/tested/deferred, matrix results, bugs
   filed against A's lane.
3. Push the branch; print a final summary.
