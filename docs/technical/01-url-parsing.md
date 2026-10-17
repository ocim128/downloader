# Technical 01 — URL Parsing & Normalization

Responsibility module: `src/xdl/urls.py`. Contract: raw input string → `TweetRef`, or a
typed parse error. Pure, synchronous, exhaustively unit-tested.

## Supported input forms

| # | Form | Example | Handling |
|---|---|---|---|
| 1 | Canonical x.com | `https://x.com/user/status/1750123456789012352` | direct parse |
| 2 | Canonical twitter.com | `https://twitter.com/user/status/1750...` | same, rewrite host to x.com |
| 3 | www / mobile subdomains | `https://www.x.com/...`, `https://mobile.twitter.com/...` | strip subdomain |
| 4 | Legacy plural path | `https://twitter.com/user/statuses/1750...` | accept `status(es)?` |
| 5 | Media-suffixed | `.../status/1750.../video/1`, `/photo/2` | strip suffix (suffix ≠ media choice for downloads; we take all media) |
| 6 | Tracking query | `...?s=20&t=abc` | discard all query params |
| 7 | Web-client fallback | `https://x.com/i/web/status/1750...` | parse id; screen_name unknown |
| 8 | Intent link | `https://x.com/intent/post?tweet_id=1750...` | read `tweet_id` param |
| 9 | Embed iframe src | `https://platform.twitter.com/embed/Tweet.html?id=1750...` | read `id` param |
| 10 | Shortlink | `https://t.co/AbCdEf123` | async expand (one redirect hop), then re-parse |
| 11 | Mirror-host forms | `https://fxtwitter.com/user/status/1750...`, `vxtwitter.com`, `fixupx.com`, `twittpr.com` | parse like canonical; strip mirror host (user pasted from a preview) |
| 12 | Scheme-less | `x.com/user/status/1750...` | tolerate; add `https://` |
| 13 | Bare ID | `1750123456789012352` | only with `--id` mode flag; screen_name unknown |
| 14 | Surrounding whitespace / pasted text | ` <url> ` or `Check this <url> out` | regex-extract first URL candidate from free text (batch files are messy) |
| 15 | Protocol-relative | `//x.com/user/status/...` | tolerate |

## Parsing algorithm

```
normalize(raw):
    s = raw.strip()
    if bare digits (2..19) and id_mode: -> TweetRef(id, screen_name=None)
    if "t.co/" in host: mark needs_expansion (handled by caller, async)
    extract URL via regex if embedded in text (form 14)
    lowercase scheme+host; strip www./mobile.
    host must be in {x.com, twitter.com, platform.twitter.com, fxtwitter.com,
                     vxtwitter.com, fixupx.com, twittpr.com}
    path forms:
        /<user>/status(es)/<id>[/...]            -> TweetRef(id, user)
        /i/web/status/<id>                        -> TweetRef(id, None)
        /intent/post?tweet_id=<id>                -> TweetRef(id, None)
    platform.twitter.com/embed/Tweet.html?id=<id> -> TweetRef(id, None)
    validate id: ^[0-9]{2,19}$ AND within snowflake plausibility
    render canonical_url: https://x.com/{user or 'i'}/status/{id}
```

Host matching is **exact-string on the registrable domain** (no suffix tricks —
`evil-x.com` must not match).

## Validation rules

- `tweet_id`: digits only, length 2–19. Snowflake plausibility (optional warning, not
  rejection): `(id >> 22) + 1288834974657` must land in `2006-11-01 .. now+1d` (Twitter
  epoch, ms). Out-of-range → `TWEET_ID_INVALID` (protects against typos/paste mangling).
- `screen_name`: `^[A-Za-z0-9_]{1,15}$` when present. Mismatch between URL screen_name
  and resolved author screen_name is **normal** (renames, mirrors) — log at debug, trust
  the resolver's value.
- Duplicate ids within one batch (same post pasted twice, or `twitter.com` + `fxtwitter`
  forms of it): dedupe at normalize stage, keep first occurrence, note in report.

## t.co expansion (async, `expand_tco`)

```python
async def expand_tco(client: httpx.AsyncClient, url: str) -> str:
    r = await client.get(url, follow_redirects=False)   # exactly one hop
    loc = r.headers.get("location")
    if not loc: raise UrlError("TCO_NO_REDIRECT")
    return loc
```

- One hop only, then re-run the parser (defense-in-depth: a `t.co` must land on an
  allowed host or it's `URL_UNRECOGNIZED`).
- Timeouts: 5 s connect / 10 s total; failure → `URL_UNRECOGNIZED` with hint
  "short link did not resolve".
- Expansion happens **before** the queue, sequentially with small concurrency (they're
  featherweight), because the parser output feeds dedup.

## Regex (reference implementation)

```python
STATUS_RE = re.compile(
    r"(?:https?:)?//(?:www\.|mobile\.)?"
    r"(?P<host>(?:fxtwitter|vxtwitter|fixupx|twittpr|x|twitter)\.com)"
    r"(?::\d+)?"                      # tolerate stray port from sloppy pastes
    r"/(?P<user>[A-Za-z0-9_]{1,15})"
    r"/status(?:es)?/(?P<id>\d{2,19})"
    r"(?:/[^\s?&#]*)?"                # optional /video/1 suffix
    r"(?:[?&#][^\s]*)?"               # optional query/fragment
)
```

Plus the two query-param forms (`i/web`, `intent/post`, `embed/Tweet.html`) handled by
dedicated small parsers.

## Error cases (unit-test table seeds)

| Input | Result |
|---|---|
| `https://instagram.com/p/xyz` | `URL_UNRECOGNIZED` (host not allowed) |
| `https://evilx.com/user/status/123` | `URL_UNRECOGNIZED` (suffix host) |
| `https://x.com/user/status/17500000000000000000000` (>19) | `TWEET_ID_INVALID` |
| `https://x.com/user/status/12` | ok (id plausibility → warning only) |
| `not a url at all` | `URL_UNRECOGNIZED` |
| `https://t.co/xyz` followed to `https://example.com` | `URL_UNRECOGNIZED` after hop |
| duplicate of earlier line in same batch | deduped, `SKIPPED_DUP_INPUT` |
| empty line / `# comment` in file | ignored silently |

## Output model

```python
class TweetRef(BaseModel):
    tweet_id: str
    screen_name: str | None
    canonical_url: str
```

Everything downstream keys on `tweet_id` alone; `screen_name` is a display convenience
until the resolver returns authoritative author data.
