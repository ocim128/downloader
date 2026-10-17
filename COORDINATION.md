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


**2026-10-17 — ROUND 2 complete on `feat/core-2` (M4 + M5 + cross-lane surfaces).**
Gates: `ruff` clean, `mypy` strict clean, `pytest` **298 passed** (was 230).
Live-verified on Windows against real endpoints/ffmpeg: single-URL download +
skip-on-rerun, `--gif` (real 5 MB GIF + sidecar), `--audio-only` (valid ID3 MP3
from a synthetic audio source via real ffmpeg; the canonical reacttweet video
is audio-less and now fails with a clear typed message), yt-dlp fallback chain
(syndication 404 -> yt-dlp hop, terminal `NO_VIDEO` in one attempt), and the
stale-yt-dlp warning (59 days old -> warning printed).

### Round-2 delivered

**M4 resilience** — per-item retries (exp backoff 1,2,4,8,16 s x jitter
0.75-1.25, `--retries` = total attempts, INTEGRITY_MISMATCH restarts at most
once, waits run through `throttle.gate()` and are cancel-interruptible);
resolve stage now gated + paced like downloads (docs/03 "gate before every
remote request"); resolve-stage 429s feed the global pause via
`ThrottleState.report_pause` (download 429s already did); escalation 3x429/60 s
-> pause >= 120 s (timestamps kept in ThrottleState); circuit breaker (8
consecutive failed/partial items, 0 non-failures between -> open; unstarted
items `SKIPPED_CIRCUIT_OPEN`, recorded incomplete only-if-untracked so
`retry-failed` picks them up; exit 1); graceful Ctrl+C (`models.CancelToken`,
engine checks per chunk, orchestrator per item/media, `BatchReport.cancelled`,
CLI exits 130 after summary, second SIGINT `os._exit(130)`); LINK_EXPIRED
recovery fixes (round-1 bug: re-resolve used default-best policy — now the
batch policy; same-object (host+path) resumes `.part`, changed rendition
restarts clean); engine OSError mapping -> `DISK_FULL`/`WRITE_ERROR` at item
level; **YtdlpResolver** (resolve-only, library import, injectable
`ydl_factory` for hermetic tests, error mapping incl. live-observed
"No video could be found" -> NO_VIDEO) wired into `build_chain` so
`fallback="auto"` finally means syndication->yt-dlp; stale-yt-dlp warning at
CLI startup when > 14 days old.

**M5 media completeness** — HLS wired at the marked `is_hls` branch
(`hls.download_hls` with injected `fetch_text` through the shared client +
throttle; progress frames -> download_progress events); MP4 fast-path always,
HLS only when no usable MP4 or the playlist genuinely beats the best MP4:
sub-720p best-MP4 + ffmpeg present + m3u8 in asset -> fetch master, compare
RESOLUTION, upgrade only if taller (best-effort, any failure keeps the MP4);
no-ffmpeg -> best-MP4 degradation when an MP4 exists, else
`HLS_REQUIRES_FFMPEG` + install hint; `--audio-only` (ffmpeg
`-vn -c:a libmp3lame -q:a 0`, honest quality note in --help) and `--gif`
(palettegen/paletteuse 15 fps) via new `media/convert.py`, staged through a
transient `<stem>.src.mp4` container that is cleaned on success/failure and
resumes the pipeline if a crashed attempt left it behind; `--force` moves the
existing final to `<name>.bak` (last bak wins) and discards stale `.part`;
`_vN` naming verified incl. ext swap (`_v2.mp3`).

**Cross-lane** — `xdl.app.analyze(urls, settings, *, client=None) ->
list[ResolvedTweet | XdlError]` (async, 1:1 with inputs, no state/file
writes); `xdl serve` delegates to `xdl.ui.__main__.main()` (flags owned by
B's argparse; typer stub is pass-through with `add_help_option=False`);
`xdl config` prints resolved Settings + config-file/db paths (source tracking
waits for B's config.py machinery); `report/logging.py` does not exist on
main yet -> marked TODO in `cli.main()` to call `setup_logging(settings)`.

### Round-2 deviations (all deliberate; docs/ are B's this round — fold into the doc sweep)

1. **New ErrorCodes `FFMPEG_REQUIRED`, `TRANSCODE_FAILED`** — docs/08 has no
   codes for user-requested conversions; additive StrEnum members. B: add to
   the docs/08 table (FFMPEG_REQUIRED: "--audio-only/--gif needs ffmpeg —
   install hint", TRANSCODE_FAILED: "ffmpeg failed: stderr tail"), and to any
   UI error-text table.
2. **ffmpeg argv now carries explicit `-f mp4|mp3|gif` before `-y`** — the
   `.part` suffix defeats ffmpeg's extension-based output-format inference
   (found live; hermetic sims can't catch it). `tests/unit/test_hls.py`'s
   golden argv updated accordingly (I own tests/unit/**).
3. **QUALITY_UNAVAILABLE (round-1 note #8) — decision for B's doc sweep**:
   closest-fit is the shipped behavior and stays; do NOT add a
   QUALITY_UNAVAILABLE code anywhere. Document in docs/02 select-stage and
   docs/08: "no variant under a `--quality N` cap -> closest fit (smallest
   above the cap), not an error".
4. **`BatchReport.cancelled: bool = False`** (additive) and
   **`run_batch(..., cancel: CancelToken | None = None)`** — UI may pass its
   own token later; render an "interrupted" line when `report.cancelled`
   (CLI exits 130 after the summary).
5. **`models.CancelToken`** — plain class (`.cancel()`, `.cancelled`); the
   shared Ctrl+C contract CLI->orchestrator->engine.
6. **conftest hermetic settings**: `resolver.fallback="none"` (a syndication
   404 must not fall through to the REAL bundled yt-dlp in tests) +
   `engine.delay=0.001` (jittered 0.2-0.5 s resolve pacing would slow the
   suite). UI tests unaffected (own stubs).
7. **Circuit threshold 8 is a constant in app.py** — if B's config.py wants
   `engine.circuit_threshold` / backoff knobs, say so in your notes and I'll
   thread them; until then the docs/08 default is hardwired.
8. **`mark_tweet_incomplete_if_untracked`** inserts a sentinel row
   (`file_path = f"{id}.unresolved"`, status incomplete) ONLY when the tweet
   has zero downloads rows — never clobbers ok history (PK is
   tweet_id+media_index).
9. **Convert-mode state anchoring**: an ok row only anchors a path when its
   suffix matches the requested output ext (an .mp4 row must not anchor an
   MP3 run). Re-running a completed tweet with a different output mode needs
   `--force` (skip check fires first) — expected.
10. **Transcode failures clean the transient `.src.mp4`** (TRANSCODE_FAILED
    is terminal, the container is not worth keeping); "Output file does not
    contain any stream" is rewritten to "source has no encodable stream
    (audio-less video with --audio-only?)".
11. **`analyze` is async** (`await analyze(...)`) — matches the UI event
    loop; errors are values (XdlError instances), never raised.
12. **`xdl serve`** is a pass-through typer stub (`allow_extra_args`,
    `add_help_option=False`) rewriting `sys.argv` to `["xdl serve", ...]` and
    calling B's `main()` — one source of truth for flags + the loopback guard.

### Round-2 deferred (smallest-sensible choices)

- `ytdlp_mode = "full"` (yt-dlp-driven downloads) — resolve-only shipped per
  M4 spec; "full" is a config value that currently behaves as resolve-only.
- HLS manual segment fallback (`--hls-manual`, docs/04 "rare, optional") —
  not built; default answer remains "install ffmpeg".
- Pre-batch free-space check — mid-write ENOSPC is handled (DISK_FULL,
  item-level); the ahead-of-time warning is not built.
- Live Ctrl+C matrix run — cancel mechanics covered at engine/app/CLI levels
  by tests; the manual matrix pass is B's lane this round.


## Agent B notes

### 2026-10-17 — M1 spike: fixtures + token goldens ready (branch `feat/ui-spike`, uncommitted-to-main checkout: `downloader-agent-b`)

- **Fixtures**: `tests/fixtures/syndication/*.json` (14 files, envelope format + provenance in `tests/fixtures/README.md`). Response shapes captured live: full video (4 id variants incl. react-tweet canonical `2040511285998313827`), animated_gif (all-mp4, no duration), 4-/3-photo grids, single photo, mixed media (GIF+photo+video in one `mediaDetails`, `1553065817788407808` — covers multi-item parsing), text-only 200, media-stripped 200, `TweetTombstone` 200 (`1872514028259680687`, suspended-account text), 404 dog-page HTML. `hls_only_synthesized.json` hand-built per rules (marked `synthesized: true`) because **m3u8-only variant arrays never occur live in 2026-10** — every video tweet has m3u8 + ≥2 mp4s.
- **Token goldens**: `tests/fixtures/golden_tokens.json` — 13 ids (incl. `20`), Node-22-exact, live-verified 13/13 HTTP 200. **Algorithm detail**: the docs' `_f64_to_base36_js` sketch does NOT match real engines; verbatim V8 `DoubleToRadixStringView` port (repeated-multiplication digits, delta-bound round-to-even, carry backtrace) does — see `scripts/record_fixture.py::_f64_to_base36_js`, 267/267 identical vs Node. Also live-proven: **token value is effectively a cache-buster** (any non-empty token → 200; empty token → 200 `{}`); doc anchor `getToken('20')='6dq1a2xwd9'` is stale (real `'6dq1a2xwd93'`).
- **Selection-rule gap**: `mediaDetails[].video_info.variants[]` has **only content_type/bitrate/url — no width/height**. The "best mp4 < 720p → HLS" rule can't read resolution from the payload; use m3u8 RESOLUTION lines or a bitrate heuristic.
- **Gating**: CDN returns full bodies for `possibly_sensitive` posts; deleted vs never-existed are byte-identical 404 dog-HTML. So "sensitive" and "deleted" are NOT distinguishable server-side; tombstone is the only distinct "gone" shape.
- **Transport gotcha**: identical params + UA over Python stdlib `urllib` → instant 404; httpx/curl → 200. Client fingerprinting exists — use httpx + browser-lib UA, never `urllib.request`.
- **Hard rate limit**: `syndication.twitter.com/srv/timeline-profile/*` is 429 all day from this IP (don't build anything on it). `cdn.syndication.twimg.com` fine at ~1 req/s and 13-rapid bursts.
- Spike scripts: `scripts/record_fixture.py`, `scripts/find_candidates.py`, `scripts/probe_batch.py` (record_fixture is the reference `getToken()` implementation for the golden lock; standalone, imports nothing from src/xdl).

### 2026-10-17 — M2: `src/xdl/media/hls.py` ready (commit `c4f164e`)

- **API** (pure module, no network in-process; fetch_text is INJECTED per docs/technical/04): `parse_master(master_text, base_url) -> list[Rendition]` (relative+absolute URLs, `URI="..."` attribute form, iframe EXT-X-MEDIA skipped, EOF-flush), `choose_rendition(renditions, policy: QualityPolicy) -> Rendition|None` (best/worst/max_height-closest-above; bandwidth fallback when no RESOLUTION), `detect_drm(master_text) -> str` (ok | aes128-ok | widevine | sample_aes → DRM_UNSUPPORTED), `target_duration(text) -> int`, `ffmpeg_argv(master_url, child_url, dest, *, user_agent, progress: bool) -> list[str]` (exact docs argv incl. `-user_agent/-allowed_extensions ALL/-protocol_whitelist file,https,tls,tcp`), `parse_progress_frames(line) -> int|None`, `require_ffmpeg(ffmpeg) -> str|None` (message w/ winget+choco hint), `download_hls(master_url, dest: Path, ...) -> Path` — fetch→parse→choose→DRM gate→ffmpeg on `dest+».part»`→rename on rc==0; TimeoutError→kill→HLS_FAILED; stderr tail capped 2000 bytes into detail.
- **Fix in my parser during tests**: STREAM-INF pend-guard was inverted (never set `pending`) — fixed; the `tests/fixtures/hls/master_*.m3u8` suite would have caught it, that's what it's for.
- **25 hermetic tests** (`tests/unit/test_hls.py`) incl. sim-ffmpeg subprocess cases (success/no-ffmpeg-fails-fast/DRM/fail-tail/timeout/progress frames) + 1 `@pytest.mark.live` smoke (live marker, skips by default).
- **For Agent A**: the swap-in point at `xdl/app.py` `_download_media` `is_hls(variant)` branch: call `hls.download_hls(master_url, dest.part_path…)`, `fetch_text = client.get(...).text` — model matches engine's existing part-file + rename invariant exactly. Live tweets always have m3u8 **and** mp4s (P1 finding), so recommend: HLS as fallback when the best-mp4 policy wants >max_bitrate, not the default path — keep MP4 fast, HLS only when genuinely better.

### 2026-10-17 — M3: local web UI done (this commit)

- **Delivered**: `src/xdl/ui/` — `app.py` (`create_app(settings, run_batch, *, analyze=None, list_batches=None, item_files=None) -> FastAPI`; all remote-callables injected, so tests are hermetic), `__main__.py` (`python -m xdl.ui` — `xdl serve` alias when Agent A lands cli entry; hard-fails non-loopback `--host`; wires REAL core via deferred `import xdl.app` + `build_chain`), `templates/index.html`, `static/{style.css,app.js,no-thumb.svg}`; `tests/ui/` stub orchestrator + 19 endpoint tests (CSP/host-guard/409/413/400/SSE event order + dedupe/retry/reveal/503s/options 422s). Full suite 230 passed; ruff + mypy(strict) clean; real `python -m xdl.ui --no-open` boot smoke-checked (200 + CSP header).
- **Endpoints** per docs/technical/10 with deviations: (1) `img-src` widened for `pbs.twimg.com`/`ton.twimg.com` poster thumbs (spec-doc'd); (2) GET /jobs returns state-store rows when `list_batches` injected — in-memory hub only backs this server's own jobs + the lone in-flight slot detail; (3) POST /jobs item overrides take `quality`/`format` per item, applied as a per-batch quality pass-through only (see M6 todo below).
- **Run-batch contract honored**: `run_batch(req, *, on_event) -> BatchReport`, `report.results` 1:1 aligned with POST-deduped `job.inputs` (retry relies on this order). Input normalization + dedup double-checked core-side (stub matches core semantics). `.part`+rename invariant untouched.
- **Deviations/TODOs for Agent A (please take or veto)**:
  1. **Analyze**: `__main__._analyze` composes `urls.normalize + build_chain` per line inside the UI layer (lifetime One httpx client per call). If/when app exposes a resolve-only helper (e.g. `xdl.app.analyze(urls, settings) -> list[ResolvedTweet|XdlError]`), I'll switch injection to it — flagged TODO in code.
  2. **MP3 / per-item quality**: spec says per-item `format='mp3'`; batch currently can't express per-item choices → recommend M6 adds `BatchRequest.quality_by_item: dict[int, str] | None` or accepts a `[format] items` payload; UI already collects the choices.
  3. **JobOptions concurrency cap `le=5`** (UI-side honesty cap; EngineCfg itself is unbounded — fine).
  4. **`hub.running_id` single-slot** can bite a user with a long batch wanting multiple sequential batches — acceptable per spec (one in-flight, 409 otherwise).
  5. **SSE closes after `batch_done`** (backlog + heartbeat until terminal event) — page re-opens with dedup-by-seq if needed; late joiners get full history since hub retains all events for the session.
- **Not in this lane**: nothing else pending on my side for M3. Remaining for full closure: Agent A's M4/M5/M6 core pieces + final doc sweep.

### 2026-10-17 (round 2) — P1-P4 complete on `feat/ui-2` (commits 9c84358/7c1a2f2/2f9eb9c)

- **P1 - UI on the real core** (`tests/ui/real_core.py` + rewritten `tests/ui/test_serve.py`):
  `RealCoreRunner` wraps the REAL `xdl.app.run_batch` with an injected `httpx.MockTransport`
  world built from the recorded fixtures under `tests/fixtures/` (chibicode video / F1
  video / cwsports photo/gif/likewise; unknown ids → 404 dog client; media bodies are
  `b"media:<filename>"`). The UI's happy path, skip-on-rerun, mixed-batch, retry, quality
  pass-through, analyze, reveal, and all 4xx/5xx shapes now run end-to-end against the real
  orchestrator; the stub remains only for fault injection (409 race, crash-after-resolve,
  429 storm, exact SSE tick order). `hub.summary` now surfaces `"error": job.error`.
  `scripts/ui_smoke.py` boots the app on a random localhost port and posts real batches —
  used as the live restart-check for `python -m xdl.ui`.
- **P2 - M6 shipped** in my lane: `src/xdl/config.py` now completes the precedence design
  (see CHANGELOG): CLI > `XDL_*` env (mechanical `XDL_SECTION__FIELD` + conveniences
  `XDL_OUTPUT/XDL_CONCURRENCY/XDL_RESOLVE_CONCURRENCY/XDL_FALLBACK/XDL_LOG_LEVEL`; empty
  values skipped) > `config.toml` > defaults, plus **unknown-key warn-and-drop** (UserWarning,
  2 levels) so stale templates can't brick the CLI, plus `LogCfg.dir`/`LogCfg.console_level`.
  `src/xdl/report/logging.py::setup_logging(settings, *, verbose=0)`: rich console per `-v`,
  rotating 5 MB × 3 file handler at `%LOCALAPPDATA%/xdl/logs/xdl.log` (INFO always-on),
  cookie-value redaction filter (auth_token/ct0/...) on the file handler. Signature
  preserved as `Settings.load(overrides, *, config_path=None, read_env=True)` (Agent A's
  `cli.py` 137/162 call `Settings.load(overrides)` — no merge break).
- **CLI logging wiring is pending on your lane** (not mine to edit `cli.py`): in
  `cli.main()`, after `settings = Settings.load(overrides)`, call
  `from xdl.report.logging import setup_logging; setup_logging(settings, verbose=verbose)`.
  Not actually wired anywhere yet by me, so nothing double-initializes.
- **P3 - docs reconciliation shipped** (README.md refresh, docs/README.md/docmap ok as-is,
  docs/02 architecture + concurrency/appDir edits, 04-project-structure full tree+signatures
  resync incl. `select(media, policy) -> list[Chosen]` (dev. 1), `run_batch(req, *, client,
  on_event)` (dev. 2), `Author.verified` (dev. 4), BatchRequest flags (dev. 5), 05-milestones
  M0-M3 ticked + M1 spike findings, tech/07 shipped CLI surface + config file + log paths.
- **P4 - manual Windows matrix, live results** (exits always via `xdl` on fresh PowerShell):
  | # | Case | Result |
  |---|------|--------|
  | 1 | fresh SMP → download | PASS (ok 1, exit 0) |
  | 2 | second run skips all | PASS (skipped, exit 0) |
  | 3 | `-f` file w/ comments, blanks, in-batch dups, fxtwitter-mirror dedup | PASS (SKIPPED_DUP_INPUT counted, folded by tweet id) |
  | 4 | photo-only → NO_VIDEO | PASS (failed, exit 1) |
  | 5 | tombstone + never-existed → both NOT_EMBEDDABLE | PASS (exit 1/2; confirmed indistinguishable) |
  | 6 | animated_gif → mp4 downloaded | PASS |
  | 7 | `--dry-run` fresh post | PASS (prints `would write ...` path, writes nothing — file count before/after identical) |
  | 8 | `XDL_OUTPUT` + `XDL_CONCURRENCY` env override + rerun-skip across output move | PASS (file landed in alt dir; skip survives since state db is time-independent of output dir) |
  | 9 | 220-line `-f file` unattended (dup-heavy + failures) | PASS (ok 0/skipped 219/failed 1/exit 1; tombstone retried once, 3 videos dedup-skipped) |
  | 10 | live multi-video post + `xdl retry-failed` on partial | **NOT RUN** — could not source a live multi-video; `find_candidates.py timeline-screen-name` is 429-dead (a known finding) and no other id source; hermetic coverage exists (`tests/integration/test_pipeline.py::test_multi_video_partial_then_retry`) |
  | 11 | Ctrl+C mid-download → re-run resumes at offset | **NOT RUN** — no long-download candidate at hand; M4 graceful Ctrl+C also not on main; hermetic resume is proven at `tests/unit/test_downloader.py:63` `test_resume_sends_range_and_merges` |
  | 12 | `--audio-only`, `xdl config`, `xdl db` | **BLOCKED** — these surface pieces are not on main yet (M5/M6-A), see tech/07's shipped/planned table |
- **Bug filed against your lane (state-app-like)**: retry of a previously-FAILED `NO_VIDEO`
  item now reports `ok 0 · skipped 1` instead of failed: `app.py`'s `upsert_tweet` records
  `media_count=0` for photo-only posts → `seen_complete()` (ok_rows == media_count) sees
  0 == 0 → counted complete → skip. A fix likely needs an `engine_result`-aware decision (or
  persist NO_VIDEO/NOT_EMBEDDABLE as failure states instead of `media_count=0` + generic
  de-mapping). Canary reproducer + rationale live in `tests/ui/README.md` filings section.
  Fine to defer to `feat/core-2`.
- **Gates at handoff**: `pytest` 248 passed / `ruff check .` clean / `mypy` strict clean
  (29 files). No live-network test paths added.
- All results quoted from live local runs against `main` HEAD-as-of-2f9eb9c; nothing
  merged to main or `feat/core-2` from this lane.

## Integrator notes

### 2026-10-17 — round-2 integration (main)
- Merged feat/core-2 then feat/ui-2: no conflicts (ownership held). Gates on
  merged main: 317 tests / ruff / mypy clean.
- **Fixed B's filing (NO_VIDEO retry false-complete)**: `app._process_item` no
  longer upserts a `tweets` row before `select()` succeeds — a photo-only post
  must not anchor `media_count=0` (0==0 made every re-run a false skip).
  Regression: `test_photo_only_fails_again_on_rerun`.
- **Wired `setup_logging`** into `cli._run` after settings assembly (the
  round-2 planned touchpoint; TODO comment removed).
- Live check on merged main: mixed-media post 1553065817788407808 → 2 media
  (gif+video; photo excluded) ok, exit 0; stale-yt-dlp startup warning fired.
- Remaining backlog lives in docs/05 + both round-2 handoffs (M7 optional;
  ytdlp full mode; --hls-manual; pre-batch free-space check; live Ctrl+C pass).
