# Technical 03 — Download Engine

Responsibility: `src/xdl/engine/`. Move bytes from an allowed CDN URL to a verified file
on disk, resumably, politely, and cancellably.

## Single-file algorithm

```
download(job):
    dest_part = dest.with_suffix(dest.suffix + ".part")
    offset = dest_part.size if dest_part.exists() else 0
    async with throttle.gate():                     # pacing + 429 cooldown
        headers = {"Range": f"bytes={offset}-"} if offset else {}
        async with client.stream("GET", job.url, headers=headers) as r:
            if r.status_code == 429: throttle.report_429(r.headers); raise Retryable
            if offset and r.status_code == 200:      # server ignored Range
                offset = 0; truncate dest_part
            if r.status_code == 403: raise LinkExpired        # signed URL expired mid-flight
            if r.status_code == 206:                          # authoritative total:
                total = int(r.headers["content-range"].rsplit("/", 1)[1])
            else:
                total = int(r.headers.get("content-length", 0)) or None   # None = unknown
            r.raise_for_status()
            with open(dest_part, "ab" if offset else "wb") as f:
                async for chunk in r.aiter_bytes(1 << 16):     # 64 KiB
                    f.write(chunk)
                    job.progress(len(chunk), total)
                    if cancelled: raise Cancelled
    verify size == total (when known)
    fsync + atomic rename dest_part -> dest
```

Key properties:

- **`.part` suffix keeps the final namespace clean** — anything without `.part` is a
  complete file (state DB and skip logic rely on this invariant).
- **Range resume**: `video.twimg.com` supports Range. If a server responds 200 to a
  ranged request (proxy misbehaving), restart from zero — never append to a mismatched
  prefix. Optional hardening: store `ETag`/`Last-Modified` of the first attempt in the
  `.part`'s sidecar (`.part.meta`) and restart if they changed (post edited / URL
  re-signed to different rendition).
- **Expired signed URLs (audit round 2, layering fix round 3)**: `video.twimg.com`
  URLs carry short-lived tokens; a resumed or long-running transfer can hit `403`
  mid-flight. A 403 on a *media* URL is `LINK_EXPIRED`, not `AUTH_REQUIRED`: the
  engine's only job is to **raise `XdlError(LINK_EXPIRED)`** — it never calls a
  resolver (layering rule 6). The **orchestrator** catches it, re-resolves the post,
  and re-dispatches the transfer for the same rendition (resuming the Range when
  the object path is unchanged; restarting clean if the rendition changed) — once per
  item. Only a 403 at the *resolve* stage means auth/protected content.
- **Atomic completion**: write to `.part`, `os.replace` to final name. A crash at any
  instant never leaves a half-file posing as complete.
- **Length handling & verification (audit round 2)**: on a 206, the total comes from
  `Content-Range: bytes s-e/TOTAL` — a 206's `Content-Length` is only the *remaining*
  bytes, so the original spec's "`content-length + offset`" was a bug (it also
  mis-fires on chunked responses where the header is absent). Chunked/missing length →
  total unknown: progress renders indeterminate and integrity is "stream ended
  cleanly + rename succeeded" — **never** a mismatch verdict, and never a reason to
  destroy the `.part`. When the total is known → exact size match or
  `INTEGRITY_MISMATCH` (delete `.part`, item fails, retryable on next run). There is
  no sha256 gate: CDNs don't publish digests, and size + a clean stream end is the
  practical bar. `fetch(url, dest, client, *, cb, throttle, cancel)` takes exactly
  those five things — no job object carrying fields nobody reads.
- **Cancellation**: checked per chunk; propagates out, `.part` retained for resume.

## Concurrency

- One item-task per input; the orchestrator bounds it with two **independent**
  semaphores: `resolve_sem` (default 4) for resolve+select, `download_sem`
  (default 3) for transfers. The resolve slot is released before the transfer phase
  starts, so resolves of later items overlap transfers of earlier ones at full
  `resolve_concurrency` — a slow 500 MB file never delays resolving the next URL,
  and a transfer parked in a 429 cooldown holds no resolve capacity (DECISIONS.md #23).
- Total in-flight HTTP requests bounded by `download_sem + resolve_sem` — a hard
  ceiling keeps failure domains small and rate limits predictable.
- ffmpeg subprocesses (HLS remux, MP3/GIF conversion) are not network-bound; they run
  inside the item task that needs them.

## Client configuration

```python
client = httpx.AsyncClient(
    http2=False,                     # MockTransport parity; flip at integration tuning
    timeout=httpx.Timeout(connect=10, read=60, write=30, pool=15),
    limits=httpx.Limits(max_connections=8, max_keepalive_connections=6),
    follow_redirects=False,          # CDN URLs are final; redirects = smell
    headers={"user-agent": UA, "accept": "*/*"},
)
```

- Redirects disabled deliberately: variant URLs are direct CDN links; a 3xx here means
  something changed upstream — surface it (`UPSTREAM_ERROR`) rather than silently
  following to an unknown host. (The `t.co` expander is the only sanctioned redirect
  follower, and it's one-hop.)
- HTTP/2 is off (and `httpx[http2]` is no longer a dependency): it cannot be exercised
  through `MockTransport`, so enabling it would ship an untested code path.

## Retry policy

| Condition | Retry? | Backoff |
|---|---|---|
| 429 | yes, via global throttle | honor `Retry-After` (seconds or HTTP-date); else 2× last, cap 300 s |
| 500/502/503/504 | yes | exp backoff 1,2,4,8,16 s × jitter(0.75–1.25), max 5 attempts |
| Connect/read timeout | yes | same as 5xx |
| 403 on media URL | once | `LINK_EXPIRED` — one re-resolve with a fresh signed URL (resume if same rendition), then fail |
| 404/401 on media URL | no | fail item (`NOT_FOUND`/`AUTH_REQUIRED`) |
| 416 (range not satisfiable) | no | `.part` is corrupt/larger than resource → truncate, one clean restart, then fail |
| TLS error | yes (once) | then fail `NETWORK` |

Backoff sleep is interruptible (Ctrl+C aware) and runs *inside* `throttle.gate()` so a
global cooldown absorbs individual backoffs naturally.

## Throttling & pacing (`engine/throttle.py`)

- `ThrottleState` holds: `pause_until: float`, `min_gap: float`, `last_request: float`.
- `gate()` before every remote request (resolve **and** download) reserves a slot and
  then waits for it: `slot = max(now, pause_until, last_request + min_gap)`,
  `last_request = slot`, `await sleep(slot - now)`. The reservation happens *before*
  the await on purpose — reading `last_request`, sleeping and then writing it lets
  every concurrent caller read the same stale timestamp, sleep the same duration and
  burst, which defeats the pacer exactly when it matters (DECISIONS.md #24).
- Resolve pacing: `min_gap` jittered at startup to 0.2–0.5 s (avoids thundering
  resolve bursts on 100-URL batches).
- `report_429(ra)` sets `pause_until = now + max(ra, 30)` globally — one worker's 429
  pauses **all** workers, which is the point.
- Circuit breaker lives in the orchestrator (consecutive-failure count, threshold
  default 8): trips → drain queue, report remaining items as `SKIPPED_CIRCUIT_OPEN`,
  exit code 1. Fully specified in
  [08-errors-and-rate-limits.md](08-errors-and-rate-limits.md).

## Graceful shutdown

- SIGINT #1: `cancel_event.set()`; intake stops; in-flight downloads finish their
  current chunk then raise `Cancelled`; state rows for partial items stay `incomplete`
  with `.part` on disk; summary prints what's resumable; exit 130.
- SIGINT #2: immediate `os._exit(130)` (`.part` invariant makes this safe).

## Disk & filesystem

- Pre-batch: check free space ≥ sum of known `content-length`s × 1.05 when resolvable
  ahead; else warn once. `ENOSPC` mid-download → item fails `DISK_FULL`, batch continues
  (other items may fit; summary says why).
- Windows notes: paths ≤ 260 unless long-path registry enabled — sanitizer caps segments
  (see [05-storage-naming-dedup.md](05-storage-naming-dedup.md)); `os.replace` is atomic
  on NTFS; open files with no sharing flags needed (plain `open`).
- **Event-loop hygiene (audit round 2)**: per-chunk `f.write` of 64 KiB to the OS cache
  stays synchronous, but every genuinely blocking call — `fsync`, `os.replace`, SQLite
  commits — runs via `await asyncio.to_thread(...)` so NTFS / antivirus-scanned /
  OneDrive-synced folders can't stall the event loop (50–200 ms stalls on Windows are
  real) and freeze other downloads' streams or progress bars. If profiling ever shows
  chunk-write stalls, wrap those too — no `aiofiles` needed.
- fsync before rename only for final completion (per-chapter fsync is pointless on
  downloads — size check carries correctness).

## Defaults table (all configurable)

| Knob | Default | Flag |
|---|---|---|
| download concurrency | 3 | `--concurrency` |
| resolve concurrency | 4 | `--resolve-concurrency` |
| resolve min-gap | 0.2–0.5 s jittered | `--delay` (sets fixed floor) |
| max retries per item | 5 | `--retries` |
| backoff base / cap | 1 s / 300 s | config |
| chunk size | 64 KiB | config |
| ffmpeg concurrency | 2 | config |
