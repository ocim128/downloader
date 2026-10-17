# DECISIONS

The durable decisions behind xdl — the ones a future change must not silently
undo. Historical lane-by-lane handoffs (rounds 1–2) are archived in
[`docs/archive/COORDINATION.md`](docs/archive/COORDINATION.md); implementation
plans live in [`docs/05-implementation-plan.md`](docs/05-implementation-plan.md).

Where this file and `docs/` disagree, this file and the code win.

## Resolver

1. **The syndication token is a byte-exact V8 `DoubleToRadixStringView` port**
   (Dragon-style fraction loop, half-ulp delta, round-to-even, carry
   back-trace), verified 0 mismatches against Node 22 across 3000 sampled ids.
   The docs' "generate + greedy right-trim" sketch produces different strings
   and is wrong. The live endpoint does not validate tokens today (any
   non-empty token → 200); we stay exact so enforcement cannot break us.
   `tests/unit/test_token.py` holds 17 Node-computed goldens.
2. **yt-dlp is resolve-only.** It feeds `ResolvedTweet`; our engine always
   downloads, so naming/sidecars/state/dedup are identical across adapters.
   The `ytdlp_mode = "full"` knob was deleted rather than left as a
   one-option config choice.
3. **No Googlebot retry strategy.** `resolver.googlebot_retry` and the UA
   constants were deleted; the strategy was never built and no call site
   existed.
4. **Intent links accept both `/intent/post` and `/intent/tweet`.**
5. **`--quality N` with no variant under the cap → closest fit** (smallest
   variant above the cap), never an error. There is no `QUALITY_UNAVAILABLE`
   code; do not add one.

## Pipeline & models

6. **`select()` returns `list[Chosen]`** — one per video/gif asset; multi-video
   posts need one choice each.
7. **`run_batch(req, *, client=None, on_event=None, cancel=None)`** — the
   keyword-only extras are the injection points for hermetic tests, the CLI
   renderer, and the web UI's cancel token.
8. **Concurrency is semaphore-bounded item tasks**, not a literal
   `asyncio.Queue` + worker pool. Resolve sem (4) + download sem (3) bound
   in-flight identically; later resolves overlap earlier transfers.
9. **`tweets.media_count` counts downloadable media (video/gif) only**, and a
   `tweets` row is written only after `select()` succeeds. A photo-only post
   must never anchor `media_count=0` (0 == 0 made every re-run a false skip).
10. **The engine takes `fetch(url, dest, client, ...)`, not a `DownloadJob`.**
    The 8-field model was built and then only two fields were read; the engine
    must not import resolver types.
11. **`ErrorCode` includes `FFMPEG_REQUIRED` and `TRANSCODE_FAILED`** for
    user-requested conversions; the docs/08 table is additive here.
12. **Circuit breaker threshold is 8 consecutive failed/partial items**, a
    constant in `app.py`. Unstarted items become `SKIPPED_CIRCUIT_OPEN` and are
    recorded incomplete only when the tweet has no download rows.

## Storage & naming

13. **Default filename is the bare tweet id** (`{id}.mp4`), layout
    `by-author`. Multi-video suffix `_v{index}`. `{date}_{id}` remains
    available via `filename_template`; there is no migration — skip reads the
    stored `file_path`, so old and new names coexist.
14. **Sidecars are `<media name>.meta.json`** (`<id>.mp4.meta.json`), appended
    to the full media filename.
15. **Convert-mode state anchoring is suffix-aware**: an `.mp4` ok-row must not
    anchor an MP3 run. Switching output mode for a completed tweet requires
    `--force`.
16. **App dir is `%LOCALAPPDATA%\xdl`** via
    `platformdirs.user_config_dir("xdl", appauthor=False)` — v4 would
    otherwise nest `xdl\xdl`. `StorageCfg.db_path` overrides it.
17. **`rebuild_from_disk` is gone.** The sidecar rescan had no CLI or UI
    surface; git history preserves it if state recovery ever becomes a real
    need.

## CLI & UI

18. **CLI dispatch is a single-command typer app plus a group**, with a manual
    argv sniff in `main()`. This is what buys `xdl URL -o DIR` interspersion
    while keeping subcommands; a plain click group cannot do both.
19. **`xdl serve` delegates to `xdl.ui.__main__`** so flags and the
    loopback-only bind guard live in exactly one place.
20. **The web UI is loopback-only, one in-flight batch, SSE with backlog+seq**
    (exactly-once replay across reconnects). `/analyze` cards describe the
    post only — there is no per-item format surface; `quality: "mp3"` is
    batch-level and maps to `--audio-only`.
21. **`/analyze` goes through `xdl.app.analyze` + one `to_card` shaper.**
    There is one analyze implementation; the UI path gets the same throttle
    and typed errors as the CLI.
22. **The UI accepts RFC 9112 absolute-form request targets.** A forward proxy
    forwards loopback requests as `POST http://127.0.0.1:7788/jobs`; uvicorn's
    h11 parser does not normalize that, so routing 404s. Middleware rewrites
    the target when the authority is loopback, and leaves anything else to the
    Host guard. Do not remove it to "simplify" the middleware stack.

## Concurrency & storage

23. **`resolve_sem` covers resolve+select only; transfers hold `download_sem`.**
    The two bounds are independent pools. Nesting the transfer inside the
    resolve slot (the original shape) silently collapsed effective resolve
    parallelism to `resolve_concurrency - concurrency` — 1 with the defaults —
    and let a transfer stalled in a 429 cooldown occupy a resolve slot. Do not
    re-merge `_prepare_item` and `_download_item`; `tests/integration/
    test_pipeline.py::test_transfer_does_not_hold_a_resolve_slot` fails if you
    do.
24. **`ThrottleState.gate()` reserves its slot before awaiting.** The read and
    write of `_last_request` must stay in one un-interleaved span (no `await`
    between them) or concurrent callers all read the same timestamp, sleep the
    same amount and burst — which is what the pacer exists to prevent.
25. **SQLite runs `synchronous=NORMAL`, not `FULL`.** ~50x cheaper per commit
    (1.05 ms → 0.02 ms measured) and every write is on the event-loop thread.
    Under WAL this can lose the last transaction on a crash but cannot corrupt
    the database; the idempotent-replay path (file present, no state row →
    re-finalize without re-downloading) makes that loss free. Do not "harden"
    this back to `FULL` without measuring.
26. **The UI buffers lifecycle events in full and progress ticks in a ring.**
    Replay must reconstruct per-item state (so every `resolve_*`/`download_*`/
    `item_done` is kept) but a 64 KiB-granularity tick stream is worthless once
    superseded (a 500-item batch buffered 41k events / 6.8 MB). `last_seq`, not
    the last buffered seq, is the replay boundary — the progress tail can be
    evicted, so the two differ.
27. **Failures are contained at the ITEM boundary, never at the batch.** Expected
    failures are `XdlError`s raised where they happen; `run_batch` additionally
    wraps each item so an escaping `XdlError` becomes that item's failure and any
    other `Exception` becomes `UNEXPECTED`. Rationale: a batch of 500 URLs must not
    be lost to one malformed post or one unmapped exception type — and a batch
    downloader has no retry-from-scratch story, so a crash is total data loss for
    that run. `CancelledError`/`KeyboardInterrupt` are `BaseException` and stay
    uncaught on purpose. Do not "simplify" this away to a bare `gather`.

## Deliberately not built

- yt-dlp-driven downloads (`ytdlp_mode = "full"`).
- HLS manual segment fallback (`--hls-manual`); the answer is "install
  ffmpeg".
- Pre-batch free-space check (mid-write `DISK_FULL` is handled per item).
- `--checksum` / sha256 sidecar field, `expected_size` plumbing.
- An `xdl rebuild` command.
- A logging subsystem: `report/logging.py`, `LogCfg`, and `-v/-vv` were
  deleted because no module ever emitted a log record. Re-add it only
  together with real call sites (429, retry give-up, `.part` resume, breaker
  open). Note that retry visibility is *not* a reason to re-add it: attempts
  and wall time now ride on `MediaOutcome.attempts` / `elapsed_ms` and
  `ItemResult.elapsed_ms`, which both frontends already consume.
