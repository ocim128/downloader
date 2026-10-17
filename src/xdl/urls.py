"""URL parsing, normalization, t.co expansion (docs/technical/01).

Pure and synchronous for parsing; t.co expansion is async (one redirect hop).
Host matching is exact-string on the registrable domain — no suffix tricks.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import httpx

from xdl.models import ErrorCode, TweetRef, XdlError

_ALLOWED_HOSTS = frozenset(
    {
        "x.com",
        "twitter.com",
        "platform.twitter.com",
        "fxtwitter.com",
        "vxtwitter.com",
        "fixupx.com",
        "twittpr.com",
    }
)

# Spec reference implementation (docs/technical/01), slightly loosened to also
# catch URLs embedded in free text (form 14) via .search().
STATUS_RE = re.compile(
    r"(?:https?:)?//(?:www\.|mobile\.)?"
    r"(?P<host>(?:fxtwitter|vxtwitter|fixupx|twittpr|x|twitter)\.com)"
    r"(?::\d+)?"  # tolerate stray port from sloppy pastes
    r"/(?P<user>[A-Za-z0-9_]{1,15})"
    r"/status(?:es)?/(?P<id>\d{2,19})(?!\d)"  # boundary: no silent truncation
    r"(?:/[^\s?&#]*)?"  # optional /video/1 suffix (we take all media)
    r"(?:[?&#][^\s]*)?"  # optional query/fragment
)

WEB_RE = re.compile(
    r"(?:https?:)?//(?:www\.|mobile\.)?"
    r"(?:(?:fxtwitter|vxtwitter|fixupx|twittpr|x|twitter)\.com)"
    r"(?::\d+)?/i/web/status(?:es)?/(?P<id>\d{2,19})(?!\d)"
    r"(?:/[^\s?&#]*)?(?:[?&#][^\s]*)?"
)

# Status path with a digit id of invalid length (too short/long) — used to
# raise TWEET_ID_INVALID instead of a bare URL_UNRECOGNIZED, but only on
# allowed hosts (an evil host must stay URL_UNRECOGNIZED).
_BAD_ID_RE = re.compile(r"/status(?:es)?/(\d+)(?:[/?&#\s]|$)")
_ALLOWED_HOST_RE = re.compile(
    r"(?:https?:)?//(?:www\.|mobile\.)?"
    r"(?:x|twitter|platform\.twitter|fxtwitter|vxtwitter|fixupx|twittpr)\.com"
    r"(?::\d+)?(?:[/?#]|$)"
)

INTENT_RE = re.compile(
    r"(?:https?:)?//(?:www\.|mobile\.)?"
    r"(?:(?:fxtwitter|vxtwitter|fixupx|twittpr|x|twitter)\.com)"
    r"(?::\d+)?/intent/(?:post|tweet)(?:[?&#][^\s]*)?"
)

EMBED_RE = re.compile(
    r"(?:https?:)?//platform\.twitter\.com/embed/Tweet\.html(?:[?&#][^\s]*)?"
)

TCO_RE = re.compile(r"(?:https?:)?//(?:www\.)?t\.co/(?P<path>[A-Za-z0-9]+)", re.IGNORECASE)

_BARE_DOMAIN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]*\.[A-Za-z]{2,}(?:/|$)")

# Twitter snowflake epoch (ms) — used for plausibility warnings only.
_SNOWFLAKE_EPOCH_MS = 1288834974657


def parse(raw: str, *, id_mode: bool = False) -> TweetRef:
    """Raw input string -> TweetRef, or a typed parse error.

    t.co short links are *not* expanded here (no network in a pure function);
    use `normalize()` or `match_tco()` + `expand_tco()`.
    """
    s = raw.strip()

    if not s:
        raise XdlError(ErrorCode.URL_UNRECOGNIZED, "empty input")

    # Form 13: bare id, only with --id
    if id_mode and s.isdigit():
        _validate_id(s)
        return _ref(s, None)

    if TCO_RE.fullmatch(_ensure_scheme(s)):
        raise XdlError(
            ErrorCode.URL_UNRECOGNIZED,
            "short link needs expansion first (t.co)",
        )

    s = _ensure_scheme(s)

    if m := STATUS_RE.search(s):
        return _ref(m["id"], m["user"])

    if m := WEB_RE.search(s):
        return _ref(m["id"], None)

    if m := INTENT_RE.search(s):
        target = m.group(0)
        if not _host_allowed(target):
            raise XdlError(ErrorCode.URL_UNRECOGNIZED, f"not an X post URL: {raw!r}")
        qs = parse_qs(urlsplit(target).query)
        if ids := qs.get("tweet_id"):
            _validate_id(ids[0])
            return _ref(ids[0], None)
        raise XdlError(ErrorCode.URL_UNRECOGNIZED, "intent link without tweet_id")

    if m := EMBED_RE.search(s):
        qs = parse_qs(urlsplit(m.group(0)).query)
        if ids := qs.get("id"):
            _validate_id(ids[0])
            return _ref(ids[0], None)
        raise XdlError(ErrorCode.URL_UNRECOGNIZED, "embed link without id")

    # Recognizable status path with an invalid-length id -> typed error, not
    # a generic "unrecognized" (spec: >19 digits or 1 digit -> TWEET_ID_INVALID).
    if (m := _BAD_ID_RE.search(s)) and _ALLOWED_HOST_RE.search(s):
        raise XdlError(ErrorCode.TWEET_ID_INVALID, f"implausible post id: {m.group(1)!r}")

    raise XdlError(ErrorCode.URL_UNRECOGNIZED, f"not an X post URL: {raw!r}")


def match_tco(raw: str) -> str | None:
    """Return the t.co URL if `raw` is (or contains) one, else None."""
    s = _ensure_scheme(raw.strip())
    m = TCO_RE.search(s)
    return f"https://t.co/{m['path']}" if m else None


async def expand_tco(client: httpx.AsyncClient, url: str) -> str:
    """One redirect hop only; the result must re-parse (defense in depth)."""
    r = await client.get(url, follow_redirects=False)
    loc: str | None = r.headers.get("location")
    if not loc:
        raise XdlError(ErrorCode.URL_UNRECOGNIZED, "short link did not resolve (no redirect)")
    return loc


async def normalize(
    client: httpx.AsyncClient,
    raw: str,
    *,
    id_mode: bool = False,
) -> TweetRef:
    """Full normalize: t.co expansion (one hop) + parse + validate."""
    if tco := match_tco(raw):
        target = await expand_tco(client, tco)
        return parse(target, id_mode=id_mode)
    return parse(raw, id_mode=id_mode)


def is_allowed_media_url(url: str) -> bool:
    """Trust boundary: media URLs must live on *.twimg.com / *.x.com."""
    host = (urlsplit(url).hostname or "").lower()
    return (
        host == "twimg.com"
        or host.endswith(".twimg.com")
        or host == "x.com"
        or host.endswith(".x.com")
    )


def snowflake_plausible(tweet_id: str, *, now: datetime | None = None) -> bool:
    """(id >> 22) + epoch within 2006-11-01 .. now+1d. Warning, not rejection."""
    ts_ms = (int(tweet_id) >> 22) + _SNOWFLAKE_EPOCH_MS
    created = datetime.fromtimestamp(ts_ms / 1000, tz=UTC)
    upper = (now or datetime.now(tz=UTC)) + timedelta(days=1)
    return datetime(2006, 11, 1, tzinfo=UTC) <= created <= upper


def _ensure_scheme(s: str) -> str:
    """Tolerate scheme-less and protocol-relative forms (12, 15)."""
    if "://" in s or s.startswith("//"):
        return s
    if _BARE_DOMAIN.match(s):
        return f"https://{s}"
    return s


def _host_allowed(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    stripped = host.removeprefix("www.").removeprefix("mobile.")
    return host in _ALLOWED_HOSTS or stripped in _ALLOWED_HOSTS


def _validate_id(tweet_id: str) -> None:
    if not tweet_id.isdigit() or not 2 <= len(tweet_id) <= 19:
        raise XdlError(ErrorCode.TWEET_ID_INVALID, f"implausible post id: {tweet_id!r}")


def _ref(tweet_id: str, screen_name: str | None) -> TweetRef:
    _validate_id(tweet_id)
    user = screen_name or "i"
    return TweetRef(
        tweet_id=tweet_id,
        screen_name=screen_name,
        canonical_url=f"https://x.com/{user}/status/{tweet_id}",
    )
