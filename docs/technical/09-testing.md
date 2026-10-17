# Technical 09 — Testing Strategy

## Principles

1. **Hermetic by default**: `pytest` never touches the network. All remote behavior is
   tested through `httpx.MockTransport` + recorded fixtures.
2. **Live tests are opt-in**: `pytest -m live` hits real endpoints — used in the M1
   spike, after adapter changes, and before releases.
3. **Fixtures are the contract**: adapter tests assert our *mapping* from frozen
   responses; when upstream changes, we re-record, diff, and consciously update —
   the diff review is the actual early-warning system for R1/R3 (endpoint drift).

## Test pyramid

### Unit (fast, no I/O)

| Target | What's locked |
|---|---|
| `urls.parse` | the ~35-case parametrized table from [01-url-parsing.md](01-url-parsing.md), incl. hostile hosts, embedded-in-text URLs, dedup |
| `syndication.token` | **golden values** captured from JS `getToken` for ~10 ids (M1 spike deliverable) — the single most valuable test in the repo |
| `_f64_to_base36_js` | round-trip properties + known JS outputs (e.g. `(0.5).toString(36)` cases, π-multiples in our range) |
| `selector.choose` | matrix: bitrate ladders × policies (best/worst/caps) × ffmpeg-present/absent × HLS-only |
| `naming.sanitize_segment` | Windows table: reserved names, trailing dots, CJK/emoji authors, 255-cap, surrogate pairs |
| `naming.render_path` | determinism (same input → same path), state-anchor override (renamed author reuses stored path) |
| `throttle` | backoff math bounds, jitter ranges, `Retry-After` parsing (int + HTTP-date), global pause visibility |
| `state` | skip semantics with/without file present, retry-failed query, rebuild_from_disk round-trip |
| `hls.parse_master` | fixture playlists: multi-bitrate pick, resolution caps, AES-128 vs DRM detection |

### Adapter tests (`tests/adapters/`)

`MockTransport` serves `tests/fixtures/syndication/*.json`; assert `ResolvedTweet`
field-for-field. Fixture set (each a real recorded response, redacted):

- `video_720p.json` — plain single video, multiple bitrates
- `video_multivideo.json` — 2–4 videos in one post
- `gif_post.json` — `animated_gif`
- `photo_only.json` — no video variants
- `tombstone.json` — `__typename: TweetTombstone`
- `not_found.json` — empty/404 shape
- `hls_only.json` — only m3u8 variants
- fxtwitter equivalents (`tests/fixtures/fxtwitter/`)

Also: request-shape assertions (params incl. token correctness for the id, headers) —
catches "silently stopped sending features" regressions.

### Integration (`tests/integration/test_pipeline.py`)

Full `run_batch` over a mock transport with scripted behaviors:

- mixed batch (ok + photo-only + tombstone + 429-then-success) → correct summary counts
  and exit-code folding
- resume: pre-place a `.part`, assert the request carries `Range: bytes=<size>-`
- server ignores Range (200 response) → restart-from-zero behavior
- 429 with `Retry-After: 1` → global pause observed (both workers), eventual success
- circuit breaker: 8 consecutive 500s → unstarted items marked `SKIPPED_CIRCUIT_OPEN`,
  exit 1
- Ctrl+C simulation: cancel event mid-chunk → exit 130 path, `.part` retained, state
  `incomplete`
- write-order crash simulation: monkeypatch to die between media-rename and state write →
  re-run completes without re-download (idempotent replay)
- multi-video partial completion: 1 of 3 media already `ok` → re-run downloads exactly
  the missing 2 (regression test for the `seen_complete` any-row bug, audit round 2)
- 206 with `Content-Range` parse, and chunked (no length) body → indeterminate
  progress, no false `INTEGRITY_MISMATCH`, `.part` preserved (audit round 2)
- media-URL 403 → one re-resolve with fresh signed URL, item recovers (audit round 2)

## Fixture recording

`scripts/record_fixture.py <url_or_id> [adapter]`:

- hits the live adapter, saves JSON with `scripts/` header (recorded-at, URL, adapter),
- redacts nothing content-wise (fixtures are private test data, committed locally; if
  the repo ever goes public, swap real text/author for synthetic — noted in the script's
  docstring),
- prints the suggested test name.

## Manual test matrix (per release, on Windows)

| Case | Expected |
|---|---|
| Fresh public video post | best-bitrate mp4 + sidecar + state row; second run skips |
| `t.co` link | expands, downloads |
| Batch file with comments/blanks/duplicates/dup-via-fxtwitter | all handled, dedup noted |
| Photo-only post | `NO_VIDEO`, batch continues, exit 1 if mixed |
| Deleted post URL | `NOT_FOUND` |
| Multi-video post | `_v2` files, one sidecar each |
| GIF post | mp4 (or `.gif` with `--gif`) |
| HLS/broadcast post (ffmpeg installed) | remuxed mp4, `-c copy` (check faststart) |
| HLS post, ffmpeg renamed off PATH | clear `HLS_REQUIRES_FFMPEG` + hint (or degraded best-mp4) |
| `--audio-only` | mp3 plays; duration matches |
| Ctrl+C mid-large-download → re-run | resumes at offset (watch log line) |
| 200+ URL batch, unattended | completes or circuit-breaks with accurate summary; no crash |
| Author with emoji/CJK name + reserved-name edge | sane filenames |
| Move `XDL_OUTPUT` + `xdl db rebuild` | state restored from sidecars |
| `--dry-run` | zero bytes written (verify tree mtime unchanged) |

## CI posture (local project)

- Pre-commit: `ruff check` + `ruff format --check` + `mypy src` + `pytest -m "not live"`.
- Optional GitHub Actions running the same (private repo) — nice-to-have, not blocking;
  the pre-commit hook is the real gate on a single-maintainer project.
