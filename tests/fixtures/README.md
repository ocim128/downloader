# Syndication fixtures (M1 spike, Agent B)

Live-recorded `cdn.syndication.twimg.com/tweet-result` responses captured on
**2026-10-17** by `scripts/record_fixture.py` (Agent B spike; network access is
allowed only under `scripts/` — these fixtures are the hermetic replacements).

## Envelope format

```jsonc
{
  "recorded_at": "ISO-8601 UTC",
  "tweet_id": "id string",
  "source_url": "the post URL form the id was sourced from (or bare id)",
  "http_status": 200,            // real HTTP status at record time
  "request_token": "getToken() output used in the request",
  "request_features": "features param value used (see below)",
  "response": { ... },           // parsed JSON body; {"raw": "<text>"} if non-JSON
  "synthesized": true(*)         // ONLY on hand-edited fixtures
}
```

## Fixtures

| file | tweet id | shape | provenance |
| --- | --- | --- | --- |
| `video_nascar_65s.json` | `2058755555980681488` | 1 video, 65.2s; variants = m3u8 + 3 mp4 (432k/832k/1280k) | NASCAR post, found via web search 2026-10 |
| `video_nhl_37s.json` | `2057633662485540993` | 1 video, 37.3s; m3u8 + 3 mp4 | NHL clip via web search |
| `video_f1_8s.json` | `2110047410852680127` | 1 video, 8.0s; m3u8 + 4 mp4 | F1 clip via web search (2026) |
| `video_chibicode_13s.json` | `2040511285998313827` | 1 video, 13.2s; m3u8 + 3 mp4; has `possibly_sensitive: false` | canonical react-tweet demo id (their docs/API reference) |
| `gif_nhl_rosters.json` | `2100595165371199589` | 1 `animated_gif`; single `video/mp4` variant, **no m3u8**, no duration | @NHL_Rosters post via web search |
| `mixed_media_gif_photo_video.json` | `1553065817788407808` | `mediaDetails` = \[animated_gif, photo, video] — the multi-item/mixed-media shape | from X devcommunity thread "Cannot get multiple videos in a single tweet" (Oct 2022 demo post, still live) |
| `photos_flotus_grid4.json` | `449660889793581056` | 4 photos (grid) | cited example in X's 2014 "multiple photos" blog post |
| `photos_mlb_grid3.json` | `449198059382386688` | 3 photos (grid) | same blog post |
| `photo_single_cwsports.json` | `2106066240427147458` | 1 photo | CW Sports post via web search |
| `no_media_vercel_text.json` | `1683920951807971329` | `__typename=Tweet`, no `mediaDetails` (text-only) | the react-tweet launch example id from Vercel's "Introducing React Tweet" blog |
| `no_media_stripped_64min.json` | `2107273352608198907` | HTTP 200 with `mediaDetails` absent though the post visibly had a 64-min video on x.com — media-stripped 200 shape | @UW_Football post via web search |
| `tombstone_unavailable.json` | `1872514028259680687` | `__typename=TweetTombstone`, text "This Post is from a suspended account. Learn more" + help.x.com link | "_screenshoter" post referenced from a devcommunity shadowban thread (real "unavailable" post) |
| `deleted_404_html.json` | `1234567890123456789` | **HTTP 404**, body is the X HTML dog/error page (kept raw, non-JSON) | synthetic (never-existed id) — see caveat below |
| `hls_only_synthesized.json` | `2058755555980681488` | video whose `variants` contain **only** `application/x-mpegURL` | **synthesized** from `video_nascar_65s.json` by stripping mp4 variants; never observed live |

## Findings Agent A should rely on

1. **Token is a cache-buster, not an auth gate.** Any non-empty token returns
   200 + full body for a viewable tweet; an empty token returns 200 with `{}`.
   Invalid ids return 404 regardless of token. Agent A must still implement
   `getToken()` exactly so requests look like react-tweet's (and so tests are
   deterministic), but playback failures will not be traced to token values.
2. **Doc-anchor discrepancy.** `docs/technical/02-media-resolution.md` anchors
   `getToken('20') = '6dq1a2xwd9'`. Node 22 today yields `'6dq1a2xwd93'`
   (trailing `3`), and `'6dq1a2xwd93'` is what we used to get live 200s. The
   golden file records Node-exact values: `tests/fixtures/golden_tokens.json`.
   Also: docs' `_f64_to_base36_js` sketch (exact rational + shortest
   round-trip trim) does **not** reproduce JS engines; the V8
   `DoubleToRadixStringView` algorithm (repeated-multiplication digits with
   delta-bound round-to-even + carry backtrace) does — `scripts/record_fixture.py`
   contains a verbatim port, verified 267/267 bit-identical against Node 22.
3. **variants carry `bitrate` + `url` + `content_type` only — no `width`/`height`.**
   The docs' "pick best mp4; if < 720p use HLS" rule cannot read resolution
   from the syndication payload — it must live in the HLS playlist parse
   (RESOLUTION lines) or use a bitrate heuristic. Flagging so the selection
   helper's contract matches reality, not the sketch.
4. **`animated_gif` has no `duration_millis`** in `video_info` and its variant
   array has no m3u8 — gif handling is a plain mp4 pick.
5. **Deleted vs never-existed are indistinguishable** for the CDN: both are
   the same 404 + `class="dog"` HTML page. `deleted_404_html.json` therefore
   stands in for both; the only *distinguishable* "gone" case is the tombstone.
6. **Sensitive/age-gated posts are NOT gated at this endpoint.** Posts flagged
   `possibly_sensitive` (e.g. `video_chibicode_13s.json`, `possibly_sensitive:
   false` there, other 200s included flagged ones) return full bodies guest.
   If a post reaches a 404/empty because of audience restrictions it is not
   distinguishable from a deleted post either (same dog page).
7. **Request fingerprinting exists.** Identical params via Python stdlib
   `urllib` receive instant 404s; httpx or curl with
   `User-Agent: curl/8.0.1` receive 200. Keep requests on httpx (or curl) with
   a browser-lib UA set; do not use `urllib.request` in the client.
8. **Rate limits.** `syndication.twitter.com/srv/timeline-profile/*` replied
   HTTP 429 "Rate limit exceeded" all day (both guest curl and after waits) —
   do not build around it. `cdn.syndication.twimg.com` tolerated ~1 req/s
   sustained and bursts up to ~13 rapid requests without complaint.

## Regenerating

```powershell
.venv/Scripts/python scripts/record_fixture.py <url-or-id> <fixture-name>
.venv/Scripts/python scripts/record_fixture.py --verify-goldens 20 <id>...
```

Goldens (`tests/fixtures/golden_tokens.json`) were produced by Node 22
(`node scripts/golden_tokens.mjs`) as source of truth and then
live-verified 13/13 (HTTP 200) with `--verify-goldens`. Format:
`[{"id": "20", "token": "6dq1a2xwd93"}, ...]` — Agent A's `get_token()` must
produce these exactly.
