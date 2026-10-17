# COORDINATION

Cross-lane notes for the two parallel implementation agents.
**Append-only, own section only** — Agent A writes under `## Agent A notes`,
Agent B under `## Agent B notes`. Never edit the other agent's section or this
header. See `agent-prompts/README.md` for the ownership map.

## Agent A notes

**2026-10-17 — M0+M1+M2 complete on `feat/core` (commits through adc072f).**
Gates: `ruff check .` clean, `mypy` strict clean, `pytest` 184 passed / 1
skipped (the skip = B's `golden_tokens.json`, loads automatically when
present). Live-verified on Windows: `xdl <url>` downloads a real public video
post (best-bitrate MP4 + sidecar), second run skips, batch file + t.co +
mirror-host dedup, `xdl retry-failed`, exit codes 0/1/2.

### Critical decision — token algorithm (READ BEFORE RECORDING GOLDENS)

`compute_token` is a **byte-exact port of V8's `DoubleToRadixStringView`**
(Dragon-style fraction loop, half-ulp delta, round-to-even, carry back-trace),
NOT the doc-sketch "generate + greedy right-trim". Verified **0 mismatches
against Node v22 across 3000 sampled tweet ids**. Consequence: id `"20"` →
`"6dq1a2xwd93"` (11 chars), while the plan's quoted anchor `"6dq1a2xwd9"` is
the sketch-algorithm output. The live endpoint accepts BOTH (probed
2026-10-17: `token=zzzzzz` and empty token also return HTTP 200 — it does not
validate tokens today). I shipped V8-exact because:
1. Agent B's goldens are Node-computed (`((Number(id)/1e15)*Math.PI).toString(36)...`)
   and `test_token.py` exact-matches them — the sketch would fail ~half of them.
2. V8-exact = byte-identical to react-tweet, the reference client, so we stay
   correct if X ever starts enforcing.
`test_token.py` asserts the plan anchor as a strict prefix
(`compute_token("20").startswith("6dq1a2xwd9")`) plus 17 Node-verified goldens.

### Deviations from docs (all deliberate, smallest sensible choice)

1. **`select()` returns `list[Chosen]`** (one per video/gif asset), not the
   single `Chosen` implied by docs/04's table — multi-video posts need one
   choice per asset. `Chosen(asset, variant)` matches the doc's shape.
2. **`run_batch(req, *, client=None, on_event=None)`** — extra keyword-only
   params beyond the documented signature: injectable httpx client (hermetic
   tests) and event subscriber (CLI renderer today, web UI tomorrow). No
   behavior change when omitted.
3. **Concurrency = semaphore-bounded item tasks** instead of a literal
   `asyncio.Queue` + download workers (docs/02 letter). Resolve sem (4) +
   download sem (3) bound in-flight exactly the same way; resolves of later
   items overlap transfers of earlier ones. Semantics identical, results
   fall out per-item naturally.
4. **`Author` gained `verified: bool = False`** — the sidecar schema
   (docs/tech/06) requires it; docs/04's model sketch omitted it.
5. **`BatchRequest` fields**: `quality: str | None`, `dry_run: bool` added
   (per-run flags that don't belong in Settings per docs/07's model).
6. **`tweets.media_count` = count of *downloadable* media (video/gif)**, not
   all mediaDetails — a photo in a mixed post must not anchor completeness.
   Photo-only posts fail `NO_VIDEO` at select, never reaching state.
7. **App dir = `%LOCALAPPDATA%\xdl`** via `platformdirs.user_config_dir("xdl",
   appauthor=False)` — platformdirs v4 nests author+name (`...\xdl\xdl`)
   otherwise. Plan text said `%APPDATA%\xdl`; single-level Local is cleaner,
   M6 makes it configurable. `StorageCfg.db_path` overrides it (tests use tmp).
8. **`--quality` max_height unmet by any variant → closest fit** (smallest
   variant above the cap), not an error. docs/02 stage table mentions
   `QUALITY_UNAVAILABLE`, docs/08 taxonomy has no such code — revisit at M5.
9. **Intent links accept `/intent/tweet` (legacy) as well as `/intent/post`.**
10. **HLS-best items fail `HLS_REQUIRES_FFMPEG` at the orchestrator** until
    B's `media/hls.py` merges — integration point: `app._download_media` has
    the marked `is_hls(chosen.variant)` branch; swap in `hls.remux` there.
11. **CLI surface**: `xdl [OPTIONS] URLS...` as default command via a
    single-command typer app + group dispatch in `main()` (click group
    callbacks can't intersperse options after positionals AND keep
    subcommands). `xdl retry-failed` works; `xdl serve` wiring waits for B's
    `ui/app.py`.
12. **`yt-dlp` adapter not yet implemented** (M4 per plan) — `fallback=auto`
    currently means syndication-only; `YtdlpResolver` slots into
    `resolve/base.build_chain` when built.

### For Agent B's UI lane (frozen shapes to code against)

- `app.run_batch(req: BatchRequest, *, client=None, on_event=None) -> BatchReport`
  — `on_event` receives `report.events.ProgressEvent` (dataclass:
  `kind: EventKind`, `tweet_id`, `media_index`, `path`, `offset`, `total`,
  `status`, `detail`, `counters`). Emit points: batch_start/resolve_start/
  resolve_done/download_start/download_progress/download_done/item_done/
  batch_done. `counters` carries `{"total": n}` on start and ok/skipped/
  partial/failed on batch_done.
- Models match docs/04 + notes 4-5 above; `ItemResult.files` holds **ok**
  outcomes only (skipped media are in `ItemResult.media` with status
  "skipped").
- `run_batch` is synchronous-blocking per item but fully concurrent inside;
  drive it via `asyncio.run`/`create_task` from FastAPI.

### Next for my lane

M4 (retry/backoff via ThrottleState, circuit breaker, Ctrl+C 130), then M5
multi-video `_vN` naming polish + `--force` `.bak` collision handling, then M6
config precedence completion + logging.


## Agent B notes

### 2026-10-17 — M1 spike: fixtures + token goldens ready (branch `feat/ui-spike`, uncommitted-to-main checkout: `downloader-agent-b`)

- **Fixtures**: `tests/fixtures/syndication/*.json` (14 files, envelope format + provenance in `tests/fixtures/README.md`). Response shapes captured live: full video (4 id variants incl. react-tweet canonical `2040511285998313827`), animated_gif (all-mp4, no duration), 4-/3-photo grids, single photo, mixed media (GIF+photo+video in one `mediaDetails`, `1553065817788407808` — covers multi-item parsing), text-only 200, media-stripped 200, `TweetTombstone` 200 (`1872514028259680687`, suspended-account text), 404 dog-page HTML. `hls_only_synthesized.json` hand-built per rules (marked `synthesized: true`) because **m3u8-only variant arrays never occur live in 2026-10** — every video tweet has m3u8 + ≥2 mp4s.
- **Token goldens**: `tests/fixtures/golden_tokens.json` — 13 ids (incl. `20`), Node-22-exact, live-verified 13/13 HTTP 200. **Algorithm detail**: the docs' `_f64_to_base36_js` sketch does NOT match real engines; verbatim V8 `DoubleToRadixStringView` port (repeated-multiplication digits, delta-bound round-to-even, carry backtrace) does — see `scripts/record_fixture.py::_f64_to_base36_js`, 267/267 identical vs Node. Also live-proven: **token value is effectively a cache-buster** (any non-empty token → 200; empty token → 200 `{}`); doc anchor `getToken('20')='6dq1a2xwd9'` is stale (real `'6dq1a2xwd93'`).
- **Selection-rule gap**: `mediaDetails[].video_info.variants[]` has **only content_type/bitrate/url — no width/height**. The "best mp4 < 720p → HLS" rule can't read resolution from the payload; use m3u8 RESOLUTION lines or a bitrate heuristic.
- **Gating**: CDN returns full bodies for `possibly_sensitive` posts; deleted vs never-existed are byte-identical 404 dog-HTML. So "sensitive" and "deleted" are NOT distinguishable server-side; tombstone is the only distinct "gone" shape.
- **Transport gotcha**: identical params + UA over Python stdlib `urllib` → instant 404; httpx/curl → 200. Client fingerprinting exists — use httpx + browser-lib UA, never `urllib.request`.
- **Hard rate limit**: `syndication.twitter.com/srv/timeline-profile/*` is 429 all day from this IP (don't build anything on it). `cdn.syndication.twimg.com` fine at ~1 req/s and 13-rapid bursts.
- Spike scripts: `scripts/record_fixture.py`, `scripts/find_candidates.py`, `scripts/probe_batch.py` (record_fixture is the reference `getToken()` implementation for the golden lock; standalone, imports nothing from src/xdl).
