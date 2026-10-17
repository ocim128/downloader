# AGENT A — Round 2: Resilience & Media Completeness (M4 + M5)

You are continuing a **working project** — not starting one. `main` already contains
M0–M3: a functional `xdl` CLI (single-URL, batch, dedup/skip, retry-failed, SQLite
state, sidecars), the syndication+fxtwitter resolvers with a V8-exact token port, the
download engine, `media/hls.py` (fully tested, not yet wired into the pipeline), and
the local web UI. 230 hermetic tests green; ruff + mypy strict clean.

**Your lane: Milestones M4 (resilience) then M5 (media completeness), plus small
cross-lane surface items.** Partial completion is fine; deliver in priority order and
leave every stopping point green.

## Before anything else

1. Read `COORDINATION.md` end to end — both round-1 handoffs. Your lane's previous
   agent documented 12 deliberate deviations (multi-Chosen `select()`, `run_batch`
   kwargs, semaphore-not-queue, `Author.verified`, `media_count` = downloadable
   media only, `%LOCALAPPDATA%\xdl`, closest-fit `--quality`, HLS branch marker in
   `app._download_media`, etc.). Honor them; don't "fix" them back to the docs.
2. Skim `docs/05-implementation-plan.md` (M4/M5), `docs/technical/03-download-engine.md`,
   `08-errors-and-rate-limits.md`, `02-media-resolution.md` Adapter D, and
   `04-hls-and-ffmpeg.md`.
3. **Doc-supersession rule:** where code + `COORDINATION.md` disagree with `docs/`,
   the code wins (e.g. the token algorithm — `docs/technical/02` carries a superseded
   banner; the V8 port in `resolve/syndication.py` is the truth).

## Parallel-lane context

Agent B works simultaneously on branch `feat/ui-2` (separate clone) doing: stub→real
UI integration, `config.py` precedence + logging module, docs reconciliation, README,
manual test matrix. **File ownership is a hard boundary:**

- **You own:** `pyproject.toml`, `src/xdl/{__init__,app,cli,models,urls}.py`,
  `src/xdl/engine/**`, `src/xdl/resolve/**`, `src/xdl/media/**` (including
  `hls.py` this round — you are its integration owner), `src/xdl/storage/**`,
  `src/xdl/report/{events,render}.py`, `tests/unit/**`, `tests/adapters/**`,
  `tests/integration/**`, `tests/conftest.py`.
- **Never edit (B's):** `src/xdl/ui/**`, `tests/ui/**`, `src/xdl/config.py`,
  `src/xdl/report/logging.py` (B's new file), `README.md`, `CHANGELOG.md`,
  `docs/**`, `scripts/**`, `tests/fixtures/**`, `agent-prompts/**`.
- If `config.py` needs a signature change for you, request it via `COORDINATION.md`
  and keep calling the current `Settings.load(...)` shape until then.

## Your priority order

### Priority 1 — M4 resilience (per `docs/05` M4 + tech 03/08)

- [ ] Per-item retries: exponential backoff 1,2,4,8,16 s × jitter(0.75–1.25), max 5
      (`--retries`), retryable classes per `docs/technical/08`; sleeps interruptible.
- [ ] 429: honor `Retry-After` (seconds or HTTP-date) via `engine/throttle.py`'s
      global `pause_until` — one worker's 429 pauses ALL workers; escalation per
      docs/technical/08 (3×429/60 s → ≥120 s pause).
- [ ] `LINK_EXPIRED`: media-URL 403 → engine raises `LinkExpiredError` (engine never
      calls a resolver — layering rule 6); orchestrator catches, re-resolves, re-
      dispatches the fresh `DownloadJob` **once** (resume Range if object path
      unchanged, else clean restart). Resolve-stage 403 stays `AUTH_REQUIRED`.
- [ ] Circuit breaker: 8 consecutive item failures, 0 successes between → drain,
      remaining items `SKIPPED_CIRCUIT_OPEN` recorded `incomplete`, exit 1.
- [ ] Graceful Ctrl+C: first SIGINT finishes current chunk, flushes state, prints
      summary, exit 130; second hard-exits (`.part` invariant makes it safe).
- [ ] `YtdlpResolver` (`resolve/ytdlp.py`): library import (bundled dep — no
      subprocess), implements the existing `Resolver` protocol, error mapping per
      docs/technical/02 Adapter D (`404→NOT_FOUND`, login-required/sensitive→
      `AUTH_REQUIRED`/`NOT_EMBEDDABLE`, network→`NETWORK`); wire into
      `resolve/base.build_chain` so `fallback="auto"` finally means
      syndication→yt-dlp (today it silently degrades to syndication-only — your
      round-1 note #12); resolve-only mode; startup warning when the installed
      yt-dlp is older than ~14 days.

### Priority 2 — M5 media completeness

- [ ] **HLS wiring** at the marked `is_hls(...)` branch in `app._download_media`:
      call `media/hls.download_hls(...)` with an injected `fetch_text` (httpx
      client) — B's handoff documents the exact API and the part-file/rename
      invariant match. Selection policy per the field finding (docs/technical/04
      banner): MP4 fast-path always; HLS only when no usable MP4 or the playlist
      genuinely beats the best MP4 (payloads carry no width/height — use m3u8
      RESOLUTION or a bitrate heuristic). Preserve `HLS_REQUIRES_FFMPEG` +
      install-hint degradation when ffmpeg is absent.
- [ ] `--audio-only` → MP3 (`ffmpeg -vn -c:a libmp3lame -q:a 0`; honest quality
      note in `--help`) and `--gif` → real GIF (two-pass palettegen/paletteuse);
      ffmpeg detection + absent-ffmpeg error path; progress via existing events.
- [ ] `--force` re-download with `<name>.bak` collision handling (last bak wins);
      `--no-meta` sidecar skip.
- [ ] Multi-video `_vN` outputs: verify complete against naming spec; fix gaps.
- [ ] Close the `QUALITY_UNAVAILABLE` doc gap (your round-1 note #8): closest-fit is
      implemented — either add the code to taxonomy or (preferred) document
      closest-fit in `docs/02`/`docs/08`… wait, docs are B's this round — record the
      decision in `COORDINATION.md` for B instead.

### Priority 3 — cross-lane surface (small, unblocks B's UI lane)

- [ ] `xdl.app.analyze(urls: list[str], settings) -> list[ResolvedTweet | XdlError]`
      resolve-only helper (B's handoff TODO 1 names this exact shape; B will switch
      the UI's analyze injection to it after merge).
- [ ] `xdl serve` subcommand → delegate to `xdl.ui.__main__` (B's runner already
      hard-fails non-loopback binds).
- [ ] `xdl config` subcommand → print resolved settings + source of each value
      (machinery lands in B's `config.py`; just render `Settings.load()` output).
- [ ] If `src/xdl/report/logging.py` exists (B delivers it mid-round), call
      `setup_logging(settings)` at startup in `cli.main()`; otherwise leave a
      marked TODO.

## Hard rules (unchanged from round 1)

- Layering rules 1–6 in `docs/04` are non-negotiable.
- `pytest` hermetic (no network, no live ffmpeg; `@pytest.mark.live` opt-in only);
  MockTransport for everything remote.
- `ruff check .` + `mypy src` clean; `pytest` green at every commit.
- Windows-first (pathlib only, ruff `PTH`).
- Specs in `docs/` are guidance; shipped code + `COORDINATION.md` are the contract.
  Deviations: smallest sensible choice + note under `## Agent A notes`.

## Setup & git

- Work in the main checkout (Windows, Git Bash, Python 3.12+; `.venv` exists —
  `pip install -e ".[dev]"` if fresh).
- `git checkout main && git pull && git checkout -b feat/core-2` and stay on it.
- Commits prefixed `[core2]`; push `-u origin feat/core-2` at the end (and
  periodically). **Never** merge/rebase onto `main` or `feat/ui-2`.

## Finish line

1. Priority 1 fully done; Priority 2+3 as far as you get, green at each stop.
2. Append handoff to `COORDINATION.md` (`## Agent A notes`): done/tested/deferred,
   deviations, integration points for B.
3. Push the branch; print a final summary.
