# COORDINATION

Cross-lane notes for the two parallel implementation agents.
**Append-only, own section only** — Agent A writes under `## Agent A notes`,
Agent B under `## Agent B notes`. Never edit the other agent's section or this
header. See `agent-prompts/README.md` for the ownership map.

## Agent A notes

_(pending)_

## Agent B notes

### 2026-10-17 — M1 spike: fixtures + token goldens ready (branch `feat/ui-spike`, uncommitted-to-main checkout: `downloader-agent-b`)

- **Fixtures**: `tests/fixtures/syndication/*.json` (14 files, envelope format + provenance in `tests/fixtures/README.md`). Response shapes captured live: full video (4 id variants incl. react-tweet canonical `2040511285998313827`), animated_gif (all-mp4, no duration), 4-/3-photo grids, single photo, mixed media (GIF+photo+video in one `mediaDetails`, `1553065817788407808` — covers multi-item parsing), text-only 200, media-stripped 200, `TweetTombstone` 200 (`1872514028259680687`, suspended-account text), 404 dog-page HTML. `hls_only_synthesized.json` hand-built per rules (marked `synthesized: true`) because **m3u8-only variant arrays never occur live in 2026-10** — every video tweet has m3u8 + ≥2 mp4s.
- **Token goldens**: `tests/fixtures/golden_tokens.json` — 13 ids (incl. `20`), Node-22-exact, live-verified 13/13 HTTP 200. **Algorithm detail**: the docs' `_f64_to_base36_js` sketch does NOT match real engines; verbatim V8 `DoubleToRadixStringView` port (repeated-multiplication digits, delta-bound round-to-even, carry backtrace) does — see `scripts/record_fixture.py::_f64_to_base36_js`, 267/267 identical vs Node. Also live-proven: **token value is effectively a cache-buster** (any non-empty token → 200; empty token → 200 `{}`); doc anchor `getToken('20')='6dq1a2xwd9'` is stale (real `'6dq1a2xwd93'`).
- **Selection-rule gap**: `mediaDetails[].video_info.variants[]` has **only content_type/bitrate/url — no width/height**. The "best mp4 < 720p → HLS" rule can't read resolution from the payload; use m3u8 RESOLUTION lines or a bitrate heuristic.
- **Gating**: CDN returns full bodies for `possibly_sensitive` posts; deleted vs never-existed are byte-identical 404 dog-HTML. So "sensitive" and "deleted" are NOT distinguishable server-side; tombstone is the only distinct "gone" shape.
- **Transport gotcha**: identical params + UA over Python stdlib `urllib` → instant 404; httpx/curl → 200. Client fingerprinting exists — use httpx + browser-lib UA, never `urllib.request`.
- **Hard rate limit**: `syndication.twitter.com/srv/timeline-profile/*` is 429 all day from this IP (don't build anything on it). `cdn.syndication.twimg.com` fine at ~1 req/s and 13-rapid bursts.
- Spike scripts: `scripts/record_fixture.py`, `scripts/find_candidates.py`, `scripts/probe_batch.py` (record_fixture is the reference `getToken()` implementation for the golden lock; standalone, imports nothing from src/xdl).
