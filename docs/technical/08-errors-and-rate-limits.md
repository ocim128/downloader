# Technical 08 — Errors & Rate Limits

Design stance: **batches never die silently and never die quietly mid-way without a
report.** Every failure is a typed enum with a user message, a retryability class, and a
defined effect on the run.

## Error taxonomy

`XdlError(code, detail, retryable_after=None)` — one enum, used in `ItemResult` and logs.

| Code | Stage | Retryable | User message (gist) | Effect on run |
|---|---|---|---|---|
| `URL_UNRECOGNIZED` | normalize | no | "not an X post URL: <input>" | item failed |
| `TWEET_ID_INVALID` | normalize | no | "implausible post id: <id>" | item failed |
| `NOT_EMBEDDABLE` | resolve | via next adapter | "not servable by this adapter (sensitive/age-gated/fresh/unindexed)" | falls through the chain; terminal only if no adapter succeeds |
| `NOT_FOUND` | resolve | no | "post deleted or never existed (failed on every adapter)" | item failed |
| `LINK_EXPIRED` | download | yes (once) | "signed CDN URL expired mid-transfer; re-resolving" | engine raises `XdlError(LINK_EXPIRED)`; the **orchestrator** re-resolves and re-dispatches once (layering rule 6), then fail |
| `AUTH_REQUIRED` | resolve/media | no | "needs an account session (see `--cookies-file`)" | item failed |
| `NO_VIDEO` | resolve/select | no | "post has no video (photos only)" | item failed (expected in mixed batches) |
| `RATE_LIMITED` | any network | **yes, timed** | "rate limited; backing off <N>s" | global pause, retry same item |
| `UPSTREAM_ERROR` | resolve/media | yes | "X-side error (HTTP <code>), will retry" | per-item retry w/ backoff |
| `NETWORK` | any network | yes | "connection failed: <cause>" | per-item retry |
| `DRM_UNSUPPORTED` | hls | no | "stream is DRM-protected; unsupported" | item failed |
| `HLS_REQUIRES_FFMPEG` | hls | no | "best quality is HLS and ffmpeg wasn't found — install: winget install ffmpeg (or use --quality to cap below HLS)" | item failed (or degraded per policy) |
| `HLS_FAILED` | hls | no | "ffmpeg failed: <stderr tail>" | item failed |
| `INTEGRITY_MISMATCH` | finalize | yes (once) | "downloaded size ≠ expected; restarting" | one clean restart, then fail |
| `DISK_FULL` | download | no | "out of space" | item failed; batch continues |
| `WRITE_ERROR` | finalize | no | "cannot write <path>: <cause> (name too long? permission?)" | item failed |
| `CANCELLED` | any | — | (not shown; run-level) | exit 130, partials resumable |

`NO_VIDEO` is flagged distinctly in the summary ("7 photo-only") because in mixed batches
it's expected noise, not breakage.

## Retry & backoff (per item)

```
attempt 1 ─fail(retryable)→ wait B·2^0 … B·2^(n-1), B=1s, ×jitter(0.75–1.25)
   … up to --retries (default 5) attempts …
RATE_LIMITED overrides its own wait with Retry-After (default 30s if absent, cap 300s)
```

Waits are `asyncio.sleep` (Ctrl+C interruptible) and run through `throttle.gate()` so
they compose with global cooldowns.

## Rate-limit strategy (whole-run)

| Layer | Mechanism |
|---|---|
| Avoidance | resolve pacing 0.2–0.5 s jittered; download concurrency 3; one shared HTTP/2 client |
| Detection | 429 on any request → `ThrottleState.report_429(Retry-After)` |
| Reaction | **all** workers pause until `pause_until` (gate check before every request); the triggering request retries after |
| Escalation | 3 × 429 within 60 s → `pause_until = now + max(ra × 2, 120s)`, log warning |
| Circuit breaker | 8 consecutive *item* failures (any retryable code) with 0 successes between → open: drain queue, mark unstarted items `SKIPPED_CIRCUIT_OPEN`, summary explains, exit 1 |
| Recovery | breaker half-opens on manual next run (stateless between runs; simplicity) |

`SKIPPED_CIRCUIT_OPEN` items are recorded `incomplete` → `xdl retry-failed` picks them
up naturally later.

## Error → exit code folding

```
all items ok/skipped/dup            → 0
≥1 failed-or-partial, ≥1 ok/skipped → 1
all items failed                    → 2
config/usage error before IO        → 2 (typer's own usage errors)
Ctrl+C                              → 130 (after summary)
```

## Surfacing in the summary

```
┌ Results ────────────────────────────────────────────────────────┐
│ #  id…812   author    status   detail                           │
│ 1  …352     someuser  ok       24.1 MB in 8.3s                  │
│ 2  …353     other     skipped  already downloaded (2 media)     │
│ 3  …354     third     failed   NO_VIDEO — photos only           │
│ 4  …355     fourth    failed   NOT_EMBEDDABLE — sensitive?      │
│ 5  …356     multi     partial  2/3 media ok; 1 failed NETWORK   │
└──────────────────────────────────────────────────────────────────┘
  ok 1 · skipped 1 · partial 1 · failed 2 · exit 1
```

The `detail` line always carries the *next action* hint when one exists (`retry-failed`,
`--cookies-file`, install ffmpeg). The full causal chain (URL, adapter, status code,
attempts) is in the log file at INFO.

## httpx exception mapping

| httpx raises | Our code |
|---|---|
| `ConnectError`, `ConnectTimeout` | `NETWORK` |
| `ReadTimeout`, `WriteTimeout`, `PoolTimeout` | `NETWORK` (read timeout mid-stream = item retry from `.part` offset) |
| `RemoteProtocolError` | `UPSTREAM_ERROR` |
| `HTTPStatusError` | by status (429/4xx/5xx tables above) |
| `TooManyRedirects` | `UPSTREAM_ERROR` (shouldn't happen; redirects off) |
| `InvalidURL` | `URL_UNRECOGNIZED` (defense in depth after parser) |
