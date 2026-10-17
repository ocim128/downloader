# Technical 02 — Media Resolution (Resolvers)

Responsibility: `TweetRef -> ResolvedTweet` (metadata + media variants). All remote
knowledge lives in this layer — the rest of the pipeline is source-agnostic.

## Adapter chain

```
SyndicationResolver        (fast-path; account-free, official embed data)
  └─ NOT_EMBEDDABLE / failure ─> YtdlpResolver   (auto; bundled safety net)
                                  └─ on failure ─> FxtwitterResolver (opt-in; third-party)
                                                   └─ GraphQLResolver (M7, opt-in, own cookies)
```

`FallbackChain` walks the configured order. **Key semantic (audit round 2): a syndication
404 / tombstone / empty response does NOT mean the post is gone** — that endpoint only
serves advertiser-friendly embeds and refuses sensitive / age-gated / fresh /
unindexed posts. Those map to `NOT_EMBEDDABLE`, which **always falls through** to the
next configured adapter; a post is reported `NOT_FOUND` only after failing *every*
adapter. Default `fallback = "auto"`: syndication fast-path → yt-dlp adapter whenever
it is importable → any further configured adapters.

---

## Adapter A — `cdn.syndication.twimg.com` (primary)

The endpoint X's own embed widget and Vercel's `react-tweet` library use. Free,
account-free, returns full post JSON for **public** posts.

### Request

```
GET https://cdn.syndication.twimg.com/tweet-result
    ?id={tweet_id}
    &lang=en
    &token={token}
    &features={features}
```

- `features`: semicolon-joined flag list. **Capture the current list verbatim from
  react-tweet's `fetch-tweet.ts` during the M1 spike** (it drifts; known members include
  `tfw_timeline_list:`, `tfw_follower_count_sunset:true`, `tfw_tweet_edit_backend:on`,
  `tfw_refsrc_session:on`, `tfw_show_blue_verified_badge:on`,
  `tfw_legacy_timeline_sunset:true`, `tfw_show_gov_verified_badge:on`,
  `tfw_show_business_affiliate_badge:on`, `tfw_tweet_edit_frontend:on`, …).
  Store as a single constant; don't model it.

### Token algorithm (exact port from react-tweet)

> **⚠ SUPERSEDED BY IMPLEMENTATION (2026-10-17, round-1 agents).** The sketch below
> is NOT what real engines produce — a byte-exact port of V8's
> `DoubleToRadixStringView` (repeated-multiplication digits, half-ulp delta,
> round-to-even, carry back-trace) is; it was verified 0-mismatch against Node 22
> across 3000 ids and ships in `src/xdl/resolve/syndication.py` (reference copy in
> `scripts/record_fixture.py`), with Node-exact goldens in
> `tests/fixtures/golden_tokens.json`. Correct example: id `"20"` → `"6dq1a2xwd93"`
> (the sketch below yields `"6dq1a2xwd9"` — a prefix only). Live probes also showed
> the endpoint currently accepts *any* non-empty token, and exact-matches
> `urllib.request` fingerprints with 404 (use httpx + a browser UA). The sketch
> remains for historical context only.

TypeScript original:

```ts
function getToken(id: string) {
  return ((Number(id) / 1e15) * Math.PI)
    .toString(6 ** 2)            // base 36, with a fractional part after "."
    .replace(/(0+|\.)/g, '')     // strip every run of zeros and the dot
}
```

Notes that make the Python port subtle:

1. JS `Number(id)` for ≤19-digit ids is **lossy** (f64). Python must mimic the loss:
   `float(int(id))` — do **not** keep int precision. Same for `/1e15` and `*Math.PI`:
   both are single f64 ops in the same order, and Python `float` is IEEE-754 f64, so
   `float(int(id)) / 1e15 * math.pi` reproduces JS bit-for-bit.
2. `Number.prototype.toString(36)` yields the *shortest* base-36 string that round-trips
   the f64. Python has no native equivalent. Exact algorithm for our value range
   (result ∈ [0, ~40)):

```python
def _f64_to_base36_js(x: float) -> str:
    """Shortest base-36 repr that round-trips to the same float (JS semantics)."""
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    if x == int(x):
        return _int_to_base(int(x), digits)
    n, den = x.as_integer_ratio()          # exact rational of the f64
    int_part, rem = divmod(n, den)         # int_part + rem/den fractional
    frac_digits = []
    for _ in range(30):                    # 30 base-36 digits is far beyond f64 resolution
        k, rem = divmod(rem * 36, den)     # exact long division in base 36
        frac_digits.append(digits[k])
    s = _int_to_base(int_part, digits) + "." + "".join(frac_digits)
    # trim to shortest string that parses back to the identical float
    while len(s) > 1 and float_of_base36(s[:-1]) == x:
        s = s[:-1]
    return s
```

   Implement `float_of_base36` with `fractions.Fraction` → `float()` for exactness, and
   treat the sketch above as the spec's intent: **digit generation over the exact
   rational, then shortest-round-trip trim**. ~40 lines total, fully unit-testable.
3. Final step mirrors the regex: `re.sub(r"(0+|\.)", "", s)`.

**Golden tests (M1)**: compute `getToken` in Node/browser for ~10 real tweet ids, hardcode
expected tokens in `test_token.py`. Any future drift shows up as an instant, obvious red.

### Headers & tactics

- Default client UA works in most regions; if 404-rate is abnormal, retry once with
  Googlebot UA `Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)`
  (historically unblocks guest caching layers). Keep this as a documented fallback toggle,
  not the default identity.
- Accept-Encoding gzip/br; JSON responses are ~2–50 KB.

### Response mapping

Relevant subset of the JSON (verify against M1 fixtures; names from react-tweet's
`twitter-url.ts` types / syndication responses):

| JSON path | → Model field |
|---|---|
| `id_str` | `tweet_id` (prefer over numeric `id` — precision) |
| `text` | `text` |
| `created_at` | `created_at` (parse their format; UTC) |
| `user.screen_name`, `user.name` | `author` |
| `mediaDetails[]` | `media[]` — one per photo/video in post |
| `mediaDetails[].type` ∈ `photo \| video \| animated_gif` | `MediaAsset.kind` (`animated_gif` → `gif`) |
| `mediaDetails[].media_url_https` | photo URL / video poster |
| `mediaDetails[].video_info.variants[]` | `variants[]`: `{url, bitrate, content_type, }` (+ width/height from `sizes`/`original_info`) |
| `__typename == "TweetTombstone"` | `NOT_EMBEDDABLE` error (falls through the chain) |
| HTTP 404 / empty object | `NOT_FOUND` |

Video variant facts:

- MP4 variants: several bitrates (typically ~320p/640p/720p/1080p; `bitrate` in bps).
- One variant is usually `application/x-mpegURL` (HLS master) — ignored for selection
  unless best-is-HLS (see [04-hls-and-ffmpeg.md](04-hls-and-ffmpeg.md)).
- Variant URLs are on `video.twimg.com`, pre-signed-ish long URLs, expire after a while —
  **resolve and download in the same run**; don't cache URLs, cache nothing.
- Multi-video posts: multiple entries in `mediaDetails[]` with
  `video_info` each; `ext_tw_video` vs `amplify_video` path prefixes don't matter to us.

### Failure semantics (from react-tweet's error handling)

| Signal | Our error |
|---|---|
| 404 or empty JSON | `NOT_EMBEDDABLE` — post is deleted **or** sensitive/age-gated/fresh/unindexed; fall through to the next adapter; terminal `NOT_FOUND` only if all adapters fail |
| `__typename: "TweetTombstone"` | `NOT_EMBEDDABLE` — protected/suspended/age-gated; falls through (a cookie resolver can still succeed) |
| 401/403 | `AUTH_REQUIRED` |
| 429 (+`Retry-After`) | `RATE_LIMITED` → throttle layer |
| 5xx / timeout | `UPSTREAM_ERROR` (retryable) |
| 200 but no `mediaDetails` | treat as `NOT_EMBEDDABLE` and fall through — syndication also strips media details on sensitive posts, so a syndication-side "no video" is not trustworthy; report `NO_VIDEO` only when the *final* adapter's full payload has no video |

---

## Adapter B — `api.fxtwitter.com` (fallback, opt-in)

Public community API of the fxtwitter project. Structured, generous, but third-party —
never the default, enabled via `--fallback fxtwitter` or config.

**Privacy trade-off (why the default is `none`)**: enabling it sends your full URL list
to that service's operator — for a privacy-motivated local tool that is a real cost,
not just a reliability one. It is also a small public service: expect throttling on
100+ URL batches. Keep it for occasional recovery, not routine bulk.

### Request

```
GET https://api.fxtwitter.com/status/{tweet_id}
Accept: application/json
```

(`/:screen_name/status/:id` also works; we always use the id-only form.)

### Response mapping

| JSON path | → field |
|---|---|
| `tweet.id_str` / `tweet.url` / `tweet.text` / `tweet.created_at` / `tweet.created_timestamp` | identity + text + date |
| `tweet.author.screen_name` / `.name` | `author` |
| `tweet.media.photos[].url` | photo assets |
| `tweet.media.videos[].url` | progressive MP4 URL (best quality) |
| `tweet.media.videos[].thumbnail` / `.width` / `.height` | poster + dims |
| `tweet.media.videos[].type` ∈ `video \| gif` | `kind` |

Differences vs syndication: single MP4 per video (no bitrate ladder — quality cap
inapplicable), includes some engagement stats (optionally into sidecar), and it can
resolve posts syndication's cache misses. Errors: non-200 or `code` field → map like
above; 404 → `NOT_FOUND`.

---

## Adapter C — GraphQL, authenticated (phase 7, opt-in) — outline only

- Endpoint: `https://api.x.com/graphql/{queryId}/TweetResultByRestId?variables={...}&features={...}`.
- Auth: cookies `auth_token` + `ct0` (CSRF header `x-csrf-token`), own account only.
- **Volatility**: `queryId` and the `features` blob rotate with the web client build;
  they must be scraped from the web client JS bundles (document the extraction recipe in
  the adapter when built). This maintenance burden is exactly why it's phase 7 and
  opt-in.
- Value: protected/followed-only posts, age-gated media, higher rate ceilings, exact
  original bitrate metadata.
- Response: `data.tweetResult.result.legacy.extended_entities.media[]` with
  `video_info.variants[]` — same variant shape as syndication; reuse the mapper.
- A **guest-token variant** of this flow (public web bearer + `POST
  /1.1/guest/activate.json`) can resolve many public posts account-free — but that
  bearer/token dance is exactly the maintenance yt-dlp already does internally. Treat
  as research notes only; prefer Adapter D for the account-free second hop.

## Adapter D — yt-dlp fallback (auto; bundled) — outline

Positioned as the **maintenance safety net** (risk R1) *and* — since audit round 2 — the
default second hop for any post syndication refuses (`NOT_EMBEDDABLE`: sensitive /
age-gated / fresh) whenever the package is importable (`fallback = "auto"`). When X
changes the syndication shape outright, the immediate remedy is a config flip or
`pip install -U yt-dlp` while the primary adapter gets patched.

- Bundled as a **standard runtime dependency** (round 3 — an optional extra would ship
  a default install whose `fallback = "auto"` quietly had no second hop). Imported as
  a library — no subprocess, no CLI-output parsing.
- `YtdlpResolver` implements the same `Resolver` protocol and maps
  `yt_dlp` extraction errors onto our taxonomy (`404 → NOT_FOUND`,
  login-required/sensitive → `AUTH_REQUIRED`/`NOT_EMBEDDABLE`, network → `NETWORK`).
- **Resolve-only** (the only mode): use its info dict to build `ResolvedTweet`; our
  engine still downloads, so naming, sidecars, state, and dedup behave identically for
  every adapter. The `ytdlp_mode` knob was removed — a config choice with one option
  is not a choice (DECISIONS.md #2).
- Startup check: warn when the installed yt-dlp is older than ~14 days (X extractor
  fixes land continuously; `pip install -U yt-dlp` is the whole remedy).

## Caching & freshness

- No URL/metadata caching across runs (signed URLs expire; posts change/deleted).
- Within a run, one resolve per `tweet_id` (input dedup upstream guarantees it).
- Optional `--prefetch` mode: resolve all, then download all — default is streaming
  (resolve N+1 while downloading N), because resolutions are cheap and downloads aren't.

## Config surface

```toml
[resolver]
primary = "syndication"        # syndication | fxtwitter
fallback = "auto"              # auto (ytdlp when importable) | ytdlp | fxtwitter | none
# graphql_* options land in phase 7
```
