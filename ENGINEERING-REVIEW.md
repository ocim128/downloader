# Engineering Review — `xdl` (2026-10-17)

> **Status: all 10 findings implemented.** Gates after implementation: **321 passed,
> 1 deselected** (was 311 passed, 1 deselected), `ruff` clean, `mypy --strict` clean,
> `scripts/ui_smoke.py` PASS, suite runtime **30.84 s → 13.99 s** (−55%).
> Ten new regression tests, each verified to fail against the pre-fix code.
> See `CHANGELOG.md` for the shipped summary and `DECISIONS.md` #23–27 for the
> durable constraints.
>
> Two honourable mentions were **not** implemented, on purpose — see
> "Deliberately not changed" at the end.

Scope: `src/xdl` (5,309 LOC), 311 tests, ruff + mypy --strict clean. All findings below were
verified against the running code, not inferred from reading alone. Measurements are from this
machine (Windows, Python 3.12.3, local NTFS).

Baseline: `pytest -q` = **311 passed in 30.84s**.

| # | Finding | Effort | Risk | Score |
|---|---------|--------|------|-------|
| 1 | `ThrottleState.gate()` doesn't serialize concurrent callers | XS | Low | **9.5** |
| 2 | SSE stream never closes for a finished job → 15s hang, 57% of suite time | XS | Low | **9.2** |
| 3 | `analyze()` / `_normalize_inputs()` are fully serial | S | Low | **8.6** |
| 4 | SQLite: commit-per-row + N+1 reads + `synchronous=FULL` | XS | Low | **8.0** |
| 5 | `resolve_sem` wraps the whole download stage | S–M | Medium | **7.4** |
| 6 | Job event buffer unbounded → 6.8 MB SSE replay | S | Low | **7.2** |
| 7 | Default `pytest` hits the network; no CI at all | XS | Low | **7.0** |
| 8 | Retries are invisible (no attempt counts, no durations) | S | Low | **6.4** |
| 9 | HLS master playlist fetched twice per upgraded item | XS | Low | **5.8** |
| 10 | Fire-and-forget `create_task` with the warning suppressed | XS | Low | **5.2** |

---

## 1. `ThrottleState.gate()` doesn't serialize concurrent callers

**Score 9.5** · Effort **XS** · Risk **Low**

### Problem

`src/xdl/engine/throttle.py:32-38`:

```python
async def gate(self) -> None:
    now = time.monotonic()
    wait = max(self._pause_until - now, self._min_gap - (now - self._last_request))
    if wait > 0:
        await asyncio.sleep(wait)
    self._last_request = time.monotonic()
```

The read of `_last_request` and the write of `_last_request` are separated by an `await`. Under
concurrency every waiter reads the *same* stale timestamp, computes the *same* wait, sleeps the
same duration, and then all of them fire simultaneously. `_last_request` is only updated after
the sleep, so the reservation is never made.

Measured with `min_gap=0.4`, 4 concurrent `gate()` calls:

```
fired at +0.000s
fired at +0.421s
fired at +0.421s     <-- burst
fired at +0.421s     <-- burst
spread = 0.421s  (expected >= 1.2s)
```

This is the default configuration path: `run_batch` creates `resolve_sem = Semaphore(4)`
(`app.py:126`) and `download_sem = Semaphore(3)` (`app.py:119`), so up to 4 resolves and 3
transfers legitimately race through `gate()` at the same instant.

### Why this is high ROI

The entire rate-limit mitigation — jittered 0.2–0.5 s pacing, global 429 cooldown, the 3×429/60 s
escalation — is built on `gate()` being a real serialization point. It isn't. The `min_gap` floor
protects nothing when 4 workers are in flight, so the pacer's *only* remaining effect is a
startup offset. The 429 cooldown (`_pause_until`) does still work — it's absolute, not relative —
which is why this never showed up as an obvious outage. It degrades into "get 429'd first, then
back off", exactly the behaviour the design set out to prevent.

`tests/unit/test_throttle.py` only ever calls `gate()` sequentially, so the bug is invisible to
the suite.

### Recommended solution

Reserve the slot *before* awaiting. The event loop is single-threaded, so the read-modify-write is
atomic as long as there is no `await` between the read and the write:

```python
async def gate(self) -> None:
    now = time.monotonic()
    slot = max(now, self._pause_until, self._last_request + self._min_gap)
    self._last_request = slot          # reserve BEFORE sleeping
    if (delay := slot - now) > 0:
        await asyncio.sleep(delay)
```

Verified with the same probe: `+0.000s, +0.406s, +0.813s, +1.219s` — correctly spaced, and the
global cooldown still dominates via `max()`. No lock needed.

Add one regression test: launch 4 `gate()` calls under `asyncio.gather` against `FakeClock` and
assert 4 distinct slots spaced by `min_gap`.

### Expected impact

- Request pacing actually holds at 1 / `min_gap` regardless of `resolve_concurrency`.
- Removes the burst-then-429 failure mode on large batches; fewer retries, shorter wall clock.
- ~15 lines changed including the test.

---

## 2. SSE stream never closes for a finished job

**Score 9.2** · Effort **XS** · Risk **Low**

### Problem

`src/xdl/ui/app.py:361-392`. The generator replays `job.events` as backlog, then enters:

```python
while True:
    ...
    try:
        seq, payload = await asyncio.wait_for(queue.get(), timeout=15)
    except TimeoutError:
        if job.done:
            return
        yield b": ping\n\n"
        continue
```

If the job is **already finished** when a subscriber attaches (open a completed job from history,
refresh the tab after a batch ended, or a test that POSTs then connects), the backlog contains
`batch_done`, the loop drains it — and then blocks on an empty queue for the full 15 s before the
`job.done` check fires. Every such subscriber hangs for 15 s.

Measured: `tests/ui/test_serve.py::test_stub_sse_exact_tick_order` takes **15.02s** — exactly the
`timeout=15` constant — and is 48% of the whole suite.

### Why this is high ROI

One 3-line fix buys the largest single test-speed win available in this repo, and fixes a real UX
wart (the browser keeps a dead EventSource open for 15 s after a batch you're just *viewing*).

### Recommended solution

```python
backlog = list(job.events)
base_seq = int(backlog[-1]["seq"]) if backlog else 0
done_seen = any(p["kind"] == EventKind.batch_done.value for p in backlog)
for payload in backlog:
    if await request.is_disconnected():
        return
    yield _sse(payload)
if done_seen and job.done:
    return  # already-finished job: replay then close, no 15 s idle
```

### Expected impact (measured)

- `tests/ui/test_serve.py`: 20.3s → **5.81s**
- Full suite: **30.84s → 13.32s** (−57%), 311 passed, ruff + mypy still clean.
- 15 s of dead-air removed from every completed-job page view.

---

## 3. `analyze()` and `_normalize_inputs()` are fully serial

**Score 8.6** · Effort **S** · Risk **Low**

### Problem

Two `for` loops that `await` a network call per iteration:

- `src/xdl/app.py:193-201` — `analyze()`: `for raw in urls: await throttle.gate(); ... await chain.resolve(ref)`
- `src/xdl/app.py:350-361` — `_normalize_inputs()`: `for raw in req.inputs: await normalize_url(...)`

`analyze()` is the backing implementation of `POST /analyze`, i.e. the web UI's "analyze these
links" flow, which accepts up to `MAX_URLS = 500`. It is strictly sequential: N × (gate + resolve
latency).

Measured with a 50 ms-per-request mock and `delay=0.001`, 20 URLs:

```
analyze(): 20 urls, 50 ms/req -> 1.21s   (perfectly linear)
```

With real X latency (200–600 ms) plus the 0.2–0.5 s default gate, 50 URLs is **~20–55 s of
wall-clock inside a single HTTP request handler**. At 500 URLs it's 5–10 minutes, with the browser
connection held open the whole time.

Secondary: `_normalize_inputs` performs t.co expansion with **no throttle at all** (it calls
`normalize_url` directly, bypassing `gate()`), so a batch of pasted t.co links fires N unpaced
requests back-to-back — the one place in the codebase that ignores its own pacing policy.

### Why this is high ROI

`run_batch` already proves the pattern: bounded `asyncio.gather` over an item list. The concurrency
machinery (`ThrottleState`, `Semaphore`, per-item error mapping) already exists; `analyze()` just
doesn't use it. This is the difference between a UI feature that feels instant and one that looks
broken.

### Recommended solution

```python
async def analyze(urls, settings, *, client=None):
    ...
    sem = asyncio.Semaphore(settings.engine.resolve_concurrency)

    async def one(raw: str) -> ResolvedTweet | XdlError:
        async with sem:
            try:
                await throttle.gate()
                return await chain.resolve(await normalize_url(http, raw))
            except (XdlError, httpx.HTTPError) as err:
                return err if isinstance(err, XdlError) else XdlError(ErrorCode.NETWORK, ...)

    return list(await asyncio.gather(*(one(u) for u in urls)))
```

`gather` preserves input order, so `zip(raw_urls, results, strict=True)` in
`ui/__main__.py:73` keeps working unchanged. Same treatment for `_normalize_inputs`, which should
also `await throttle.gate()` around `expand_tco` so the pacing policy holds uniformly.

### Expected impact

- `POST /analyze`: ~75% latency reduction at 4-way fan-out (measured 1.21s → ~0.25s at 50 ms/req;
  scales to 50 URLs: ~30s → ~8s).
- Removes the only unpaced request path in the codebase.
- ~25 lines, no interface change.

---

## 4. SQLite: commit-per-row, N+1 reads, `synchronous=FULL`

**Score 8.0** · Effort **XS** · Risk **Low**

### Problem

Three separate costs in `src/xdl/storage/state.py`, all on the hot path and all **blocking calls
executed inside the event loop**:

1. **`synchronous` is never set**, so WAL runs with the default `FULL` — an fsync per commit.
   Measured: **1.05 ms/commit** (`FULL`) vs **0.02 ms/commit** (`NORMAL`) — 50×.
2. **Every mutation commits individually** — `record()` (`:178`), `mark_incomplete()` (`:204`),
   `upsert_tweet()` (`:106`), `new_batch()`/`finish_batch()`. A 200-item × 2-media batch issues
   ~600 fsync-backed commits = ~630 ms of blocked event loop.
3. **`ok_rows()` is re-queried per media file.** `_process_item` already computes
   `ok_by_index = {r.media_index: r for r in ctx.state.ok_rows(...)}` (`app.py:426`), then
   `_download_media` throws it away and runs a fresh `SELECT` per media (`app.py:504-508`). A
   4-media post = 4 redundant queries. `seen_complete()` also issues 2 statements
   (`get_tweet` + `COUNT`) before `ok_rows()` issues a third — the skip path costs 3 queries where
   1 would do.

### Why this is high ROI

These are blocking syscalls in the same thread that pumps 3 concurrent download streams. On a
network drive or spinning disk (a plausible target for a media downloader) the per-commit cost is
10–50 ms, which starts visibly stuttering progress bars. The fix is one pragma plus threading one
dict through.

### Recommended solution

```python
# state.py __init__, after journal_mode=WAL
self._conn.execute("PRAGMA synchronous=NORMAL")   # WAL-safe: no corruption risk
self._conn.execute("PRAGMA busy_timeout=5000")    # explicit > implicit
```

`NORMAL` in WAL mode risks losing the *last* transaction(s) on OS crash or power loss — never
database corruption. For this app that trade is free: the idempotent-replay path
(`app.py:529-535`) already treats "file present, no state row" as a normal condition and re-finalizes
without re-downloading.

Then pass the already-computed map instead of re-querying:

```python
async def _download_media(tweet, chosen, ctx, *, known_ok: dict[int, DownloadRow] | None = None):
    row = (known_ok or {}).get(index)
    if row is not None and Path(row.file_path).is_file():
        ...
```

and thread `ok_by_index` from `_process_item` (`app.py:434`).

Optional, later: batch commits per item (`record` + `mark_incomplete` in one transaction) if
profiling still shows it.

### Expected impact

- Commit cost: 1.05 ms → 0.02 ms (**50×**); ~630 ms of blocked loop per 200-item batch → ~12 ms.
- 3 queries per skip → 1; 4 redundant queries per 4-media download → 0.
- ~15 lines.

---

## 5. `resolve_sem` wraps the whole download stage

**Score 7.4** · Effort **S–M** · Risk **Medium**

### Problem

`src/xdl/app.py:142-149`:

```python
async with resolve_sem:            # 4 slots
    ...
    result = await _process_item(item, ctx)   # <-- includes every download
```

and the download happens *inside* that block via `ctx.download_sem` (`app.py:434-435`).

So an item holds a **resolve** slot for the entire duration of its **transfers**. The two bounds
are nested, not independent. With `resolve_concurrency=4` and `concurrency=3`, at most 4 items are
ever in flight, and in the steady state 3 of those 4 slots are held by downloading items — leaving
an effective resolve parallelism of **1**, not the configured 4.

The module docstring (`app.py:14-17`) claims this is "semantics identical to a queue+worker
topology for bounded fan-out". It isn't: with a queue+workers design, resolve capacity and transfer
capacity are independent pools. Here, slow downloads directly starve resolves — and a download
stalled behind a 120 s throttle cooldown holds a resolve slot hostage for the entire pause, which
is precisely when you'd want to keep resolving other items.

### Why this is high ROI

It's the difference between a pipelined batch and a batch whose throughput is bounded by
`min(resolve_concurrency, ...)` items in flight. It also means `resolve_concurrency` is a knob that
doesn't do what its name says — a trap for anyone tuning a large batch.

### Recommended solution

Split the stage boundary so the semaphores stop overlapping:

```python
async def run_item(item: _Item) -> ItemResult:
    ...
    async with resolve_sem:                       # resolve + select only
        if ctx.breaker.open:
            return _circuit_result(ctx, item)
        resolved = await _resolve_and_select(item, ctx)   # returns tweet/chosen or ItemResult
    if isinstance(resolved, ItemResult):
        ctx.breaker.record(resolved.status)
        return resolved
    result = await _download_item(resolved, item, ctx)     # download_sem acquired inside
    ctx.breaker.record(result.status)
    return result
```

Keep the breaker record *after* the download so the existing 8-consecutive-failure semantics are
preserved exactly.

**Risk is Medium, not Low**, because this reorders the item lifecycle and the current test suite
covers the resilience semantics tightly (`tests/integration/test_resilience.py`, 16 tests). Do it
with the suite green, and add one test that asserts a slow transfer no longer blocks a later
item's resolve.

### Expected impact

- Resolve parallelism goes from effectively 1 to `resolve_concurrency` (4) in the steady state.
- Batch wall clock improves whenever resolve latency is a meaningful fraction of transfer time
  (many small videos, or heavy 429 cooldowns).
- No public interface change.

---

## 6. Job event buffer is unbounded; reconnect replays all of it

**Score 7.2** · Effort **S** · Risk **Low**

### Problem

`_Job.events` (`src/xdl/ui/app.py:128`) is append-only and never trimmed. `_Hub.jobs` is likewise
never evicted. `download_progress` is emitted per 64 KiB chunk (`engine/downloader.py:24`), so a
500-item batch produces tens of thousands of events.

Measured for 500 items × 1 media × 80 chunks:

```
events buffered: 41000   (≈7.2 MB of repr, far more with dict overhead)
SSE replay of the full backlog: 6.8 MB, 79 ms to serialize
```

Every `EventSource` (re)connect replays that entire backlog through `applyEvent`, which does a
`getElementById` + 5 `querySelector` calls per progress event (`static/app.js:186-193`). Refresh
the tab mid-batch and you get a multi-megabyte download and thousands of redundant DOM writes for
progress bars that are already at their final width.

`_counts()` (`app.py:196-209`) also scans the whole list when `job.report is None` — 3.8 ms per
call for the in-flight job. Minor on its own, but it's the same root cause: the buffer is being
used as a database.

### Why this is high ROI

A long-lived `xdl serve` accumulates memory per batch with no ceiling. For a personal tool that's
tolerable at 50 URLs and unpleasant at 500 — and the fix is small and local.

### Recommended solution

Two independent, low-risk changes:

1. **Don't buffer progress ticks.** In `progress_sink` (`app.py:481-487`), append only lifecycle
   events (`batch_*`, `resolve_*`, `download_start`, `download_done`, `item_done`) to
   `job.events`; still fan out progress events to live subscribers. Backlog replay then carries
   state, not history. This alone drops the buffer from 41k to ~2k events.
2. **Bound the hub.** Evict finished jobs beyond the last N (say 50) in `_Hub.create`, and skip
   retention entirely for jobs that already have a `report` (the report is the durable record).

Optionally maintain incremental counters in `emit()` so `_counts()` becomes O(1).

### Expected impact

- Event buffer: 41,000 → ~2,000 events for a 500-item batch (>95% reduction).
- Reconnect replay: 6.8 MB → ~0.3 MB; no DOM thrash for stale progress bars.
- Bounded memory for a long-running server.
- ~20 lines.

---

## 7. Default `pytest` hits the network; there is no CI

**Score 7.0** · Effort **XS** · Risk **Low**

### Problem

- `pyproject.toml:78-83` defines the `live` marker but sets no `addopts`, so a bare `pytest`
  runs `tests/unit/test_hls.py::test_live_smoke_parse_real_master_playlist` — which makes two real
  HTTP calls to `cdn.syndication.twimg.com` and `video.twimg.com` (2.31s, second-slowest test in
  the suite). Every other test is hermetic by careful design (`conftest.py` pins `fallback="none"`
  specifically to avoid the network); this one test leaks it back in. It will fail on a plane, in
  a sandbox, or the day X changes a header.
- There is no `.github/workflows`, no Makefile, no task runner. 311 tests, `ruff`, and
  `mypy --strict` all pass locally, but nothing enforces that on the way in. The git log shows
  audit rounds catching regressions by hand ("fix NO_VIDEO false-complete", "fix live 500 on index
  with batch history") — exactly the class of bug a 90-second CI job catches automatically.

### Why this is high ROI

Cheapest possible compounding improvement: the gates already exist and already pass. Cost is one
YAML file.

### Recommended solution

```toml
[tool.pytest.ini_options]
addopts = "-m 'not live'"      # hermetic by default
```

then run live checks explicitly with `pytest -m live`. And:

```yaml
# .github/workflows/ci.yml
- run: pip install -e ".[dev]"
- run: ruff check src tests
- run: mypy
- run: pytest -q          # now 13s, network-free
```

Keep the `live` job separate, scheduled (nightly), never blocking PRs.

### Expected impact

- Default suite becomes deterministic and offline.
- Every PR gated on 311 tests + lint + strict types in ~1 minute.
- Kills the "audit round N finds it by hand" loop.

---

## 8. Retries are invisible

**Score 6.4** · Effort **S** · Risk **Low**

### Problem

`_with_retries` (`src/xdl/app.py:275-315`) accepts an `on_error` hook, but only the resolve path
wires it (`app.py:450-456`, and only to report 429s). The download path
(`app.py:618-624`) passes none. Consequence: a file that needed 3 attempts and succeeded is
indistinguishable from one that succeeded first try — no attempt count, no duration, nothing in
`ItemResult`, `MediaOutcome`, or the rendered summary.

The same gap applies to timing: nothing anywhere records how long an item took. The summary table
(`report/render.py:82-104`) shows status and detail only.

Note: a full logging subsystem was deliberately deleted in audit round 3 (`DECISIONS.md:99`) as a
phantom feature. This is *not* a request to bring it back — it's the opposite: put the signal on
the data structures that already exist and already flow to both frontends.

### Why this is high ROI

When a batch is slow or flaky, the current answer to "why?" is "re-run it and watch". Two integers
per item make that question answerable from the report, at zero new surface area and zero new
flags. This is the smallest possible observability win and it compounds with every future
diagnosis.

### Recommended solution

Extend the existing shapes rather than adding a subsystem:

```python
class MediaOutcome(BaseModel):
    ...
    attempts: int = 1
    elapsed_ms: int = 0
```

Have `_with_retries` return `(value, attempts)` or accept a mutable counter, set it in
`_download_media`, and render it in the summary only when `attempts > 1`:

```
#3  …a1b2  user   ok   1 media file(s) downloaded (2 attempts, 8.4s)
```

Because `ProgressEvent` and `ItemResult` are already the contract for both CLI and web UI, both
frontends get this for free.

### Expected impact

- Flaky-network batches become diagnosable from a single report.
- Identifies chronic retry hot spots (which is also how you'd justify tuning `retries`).
- ~20 lines across `models.py`, `app.py`, `render.py`.

---

## 9. HLS master playlist fetched twice per upgraded item

**Score 5.8** · Effort **XS** · Risk **Low**

### Problem

`_maybe_upgrade_to_hls` (`src/xdl/app.py:674-679`) fetches the master playlist to decide whether
HLS is genuinely taller:

```python
r = await ctx.http.get(m3u8.url)
best = hls.choose_rendition(hls.parse_master(r.text, m3u8.url), ctx.policy)
```

It then returns the m3u8 `Chosen`, and `_acquire` routes to `_acquire_hls` → `hls.download_hls`,
which fetches the **same** master again (`media/hls.py:226`, `master = await fetch_text(master_url)`).

Every sub-720p item that has an HLS sidecar pays one extra round-trip, unpaced with respect to the
parse that already happened.

### Why this is high ROI

Small but pure waste on a hot path, and the fix is contained: `_maybe_upgrade_to_hls` already has
the parsed renditions — carry them forward instead of discarding them.

### Recommended solution

Return the parsed renditions alongside the `Chosen` (e.g. a small `_HlsPlan` dataclass, or a
`dict[str, list[Rendition]]` on `_RunCtx` keyed by master URL) and let `download_hls` accept an
optional pre-fetched `master_text`/`renditions` argument. One extra keyword arg in
`hls.download_hls`, one field on the return of `_maybe_upgrade_to_hls`.

Also worth noting: the `except (XdlError, httpx.HTTPError): return None` at `app.py:680-681`
silently swallows upgrade failures with no trace anywhere — a `detail` string on the event would
make "why didn't this get 1080p?" answerable.

### Expected impact

- One HTTP request removed per HLS-upgraded item (~1 per sub-720p post).
- Slightly better pacing accuracy (no unpaced fetch).

---

## 10. Fire-and-forget `create_task` with the warning suppressed

**Score 5.2** · Effort **XS** · Risk **Low**

### Problem

`src/xdl/ui/app.py:334` and `:420`:

```python
asyncio.create_task(_run(hub, run_batch, req, job))  # noqa: RUF006
```

The event loop holds only a weak reference to a task. `RUF006` exists precisely because a task with
no strong reference can be garbage-collected mid-flight, and its exception silently dropped. The
`# noqa` documents that the authors knew and chose to look away. `_run` does catch `Exception`, so
the blast radius is small — but a cancelled task, or a GC before the first `await` completes, is
an untraceable "the batch just never started".

### Why this is high ROI

Three lines, removes a whole class of "it worked on my machine" reports from a long-running server.

### Recommended solution

Keep a strong reference set on the hub and discard on completion:

```python
class _Hub:
    def __init__(self) -> None:
        ...
        self._tasks: set[asyncio.Task[None]] = set()

    def spawn(self, coro: Awaitable[None]) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
```

Replace both call sites with `hub.spawn(_run(...))` and delete the two `# noqa` comments.

### Expected impact

- Eliminates the GC-drops-a-batch failure mode; tasks are now observable and cancellable.
- ~8 lines, and one fewer suppressed lint rule.

---

## Honourable mentions (not scored — too small to justify a section)

- **`httpx[http2]` extra with `http2=False`** (`pyproject.toml:15`, `app.py:78`). `h2 4.4.1` is
  installed and never used. Either drop the extra or flip the flag and re-run the integration
  suite — the comment says "HTTP/2 flip at integration tuning", so it's already planned; right now
  it's pure install weight.
- **`_load_toml` instantiates a whole `Settings()`** just to get `app_dir` (`config.py:106`).
  Trivial, but it's a pydantic model construction on every load; `platformdirs.user_config_dir`
  is a direct call away.
- **`is_allowed_media_url` accepts `x.com`/`*.x.com`** (`urls.py:142-150`). Media never lives
  there — only `*.twimg.com` does. Tightening the allowlist to `twimg.com` is a free security
  narrowing, at the cost of one line and a test update.
- **`parse_textarea` dedupes raw strings** (`ui/app.py:212-228`) while `_normalize_inputs`
  dedupes by tweet id. `x.com/u/status/1` and `twitter.com/u/status/1` both survive the UI's
  dedupe and are only caught later. Harmless today (the orchestrator catches it) but the duplicate
  count reported back to the user is understated.

---

## Suggested execution order

1. **#1 + #2 + #4** — three XS changes, ~40 lines total, no design decisions. Do these in one
   commit; the suite halves in runtime and the pacer starts working.
2. **#7** — lock the gains in with CI before touching anything riskier.
3. **#3** — the analyze fan-out, self-contained, biggest user-visible latency win.
4. **#6, #9, #10** — UI lifecycle and small waste removal.
5. **#5** — the semaphore split, alone, with the resilience suite as the safety net.
6. **#8** — instrumentation last, once the shape of the data has settled.

*(Executed in exactly this order. #5 landed with a dedicated regression test that was first
confirmed to fail under the old nesting.)*

---

## Deliberately not changed

Two honourable mentions were dropped after implementation started, because on closer
inspection they were not clearly correct:

- **Tightening `is_allowed_media_url` to `twimg.com` only.** Every media URL in the
  fixtures is `*.twimg.com`, but the allowlist also admits `*.x.com`, and the URL
  comes from a *resolver payload* (yt-dlp, fxtwitter) rather than from user input.
  I could not verify against live yt-dlp/fxtwitter output that an `x.com`-hosted
  media URL never occurs, and the failure mode of being wrong is a hard
  `URL_UNRECOGNIZED` on a post that used to download. Marginal security gain
  (the host is already resolver-controlled) for an unverifiable regression risk:
  not worth shipping. Left as-is, deliberately.
- **Deduping `parse_textarea` by tweet id.** The orchestrator already dedupes by id
  authoritatively; changing the UI to do it too would make the UI diverge from the
  CLI file-reader semantics it claims to mirror, and would only change a cosmetic
  "duplicates" count. Not worth the divergence.

## Extra fix found during implementation

Not in the original ten, fixed because it sits inside code the review already rewrote:

- **A t.co transport error aborted the entire batch.** `_normalize_inputs` caught only
  `XdlError`, so an `httpx.ConnectError` from the t.co expander propagated out of
  `run_batch` as a raw traceback — one dead short link killed a 500-URL batch.
  `analyze()` had always mapped this correctly, so the two paths disagreed. Now mapped
  to a per-item `NETWORK` failure. Regression test:
  `test_tco_transport_failure_fails_one_item_not_the_batch`.

- **Same bug class, second instance: `_acquire_hls.fetch_text`.** Auditing for the t.co
  pattern rather than just patching it turned up `ctx.http.get(master_url)` called with no
  `httpx.HTTPError` mapping, so an HLS manifest transport error also unwound through
  `gather`. Fixed by mapping to `NETWORK` and threading `master_text` through so the
  manifest is only fetched once (which is also finding #9). Regression test:
  `test_hls_manifest_transport_error_fails_item_not_batch`.

- **The real root cause: no item-level containment boundary.** Both instances above were
  symptoms of the same structural gap — any unmapped exception type escaping one item
  killed the batch. `run_item` now wraps `_run_item` in an explicit boundary that converts
  anything not already typed into an `UNEXPECTED` item result and records it against the
  circuit breaker. This is defence in depth, not a licence to leave mapping gaps: every
  *known* failure is still mapped closer to its source, and `UNEXPECTED` is deliberately
  absent from `_RETRYABLE` so a genuine bug is never retried into silence. Documented as
  `DECISIONS.md` #27 so it is not "simplified" away later. Regression test:
  `test_unexpected_item_exception_is_contained`.

