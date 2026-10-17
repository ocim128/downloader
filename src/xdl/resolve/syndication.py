"""Adapter A — cdn.syndication.twimg.com (primary resolver).

Includes the exact port of react-tweet's `getToken` (docs/technical/02):
JS f64 semantics, shortest-round-trip base-36, strip `0+` runs and the dot.
Golden: tweet id "20" -> token "6dq1a2xwd9" (live-verified 2026-10-17).
"""

from __future__ import annotations

import math
import re
from datetime import datetime
from fractions import Fraction
from typing import Literal

import httpx

from xdl.models import (
    Author,
    ErrorCode,
    MediaAsset,
    ResolvedTweet,
    TweetRef,
    Variant,
    XdlError,
)

SYNDICATION_URL = "https://cdn.syndication.twimg.com/tweet-result"

DEFAULT_UA = "xdl/0.1 (+https://github.com/ocim128/downloader)"
GOOGLEBOT_UA = "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"

# Features list captured from react-tweet's fetch-tweet.ts (drifts upstream;
# stored as one constant, never modeled). Live-verified 2026-10-17.
FEATURES = ";".join(
    (
        "tfw_timeline_list:",
        "tfw_follower_count_sunset:true",
        "tfw_tweet_edit_backend:on",
        "tfw_refsrc_session:on",
        "tfw_show_blue_verified_badge:on",
        "tfw_legacy_timeline_sunset:true",
        "tfw_show_gov_verified_badge:on",
        "tfw_show_business_affiliate_badge:on",
        "tfw_tweet_edit_frontend:on",
    )
)

# --------------------------------------------------------------------------- #
# Token algorithm — exact port
# --------------------------------------------------------------------------- #

_BASE36_DIGITS = "0123456789abcdefghijklmnopqrstuvwxyz"
_ZERO_RUN_RE = re.compile(r"(0+|\.)")


def _int_to_base36(n: int) -> str:
    if n == 0:
        return "0"
    out = []
    while n:
        n, rem = divmod(n, 36)
        out.append(_BASE36_DIGITS[rem])
    return "".join(reversed(out))


def _float_of_base36(s: str) -> float:
    """Exact parse of a base-36 string (optional '.') to the nearest float."""
    int_s, _, frac_s = s.partition(".")
    value = Fraction(int(int_s, 36)) if int_s else Fraction(0)
    scale = Fraction(1)
    for ch in frac_s:
        scale /= 36
        value += _BASE36_DIGITS.index(ch) * scale
    return float(value)


def _f64_to_base36_js(x: float) -> str:
    """Exact port of V8's `DoubleToRadixStringView` (radix 36).

    Fraction digits are produced by the Dragon-style loop: shift fraction and
    a half-ulp `delta` up by one digit per step, truncate the integer digit,
    round to even on the boundary, carry-propagate on overflow, and stop once
    the remaining fraction is below delta. This is byte-identical to
    `Number.prototype.toString(36)` on V8 (Node/Chrome) — the engine
    react-tweet runs on and the source of our golden tokens. (V8's
    non-power-of-two radix output is not always the shortest round-trip
    string; Node-derived goldens are the contract, verified on 3000 ids.)
    """
    if math.isnan(x) or math.isinf(x):
        raise ValueError(f"cannot encode {x!r} in base 36")
    negative = x < 0
    x = abs(x)
    if x == 0.0:
        return "0"

    integer = math.floor(x)
    fraction = x - integer
    delta = 0.5 * (math.nextafter(x, math.inf) - x)
    if delta <= 0.0:  # denormal edge (unreachable for token-range values)
        delta = math.nextafter(0.0, math.inf)

    frac_chars: list[str] = []
    if fraction >= delta:
        while True:
            fraction = fraction * 36.0
            delta = delta * 36.0
            digit = int(fraction)
            if digit > 35:  # fraction*36 rounded up to 36.0; V8 reads the NUL
                digit = 35  # byte here — clamp rather than crash
            frac_chars.append(_BASE36_DIGITS[digit])
            fraction = fraction - digit
            # Round to even. (Nested ifs mirror V8's control flow exactly.)
            if fraction > 0.5 or (fraction == 0.5 and (digit & 1)):  # noqa: SIM102
                if fraction + delta > 1.0:
                    if _carry_backtrace(frac_chars):  # full rollover: '.' drops
                        integer += 1
                        frac_chars = []
                    break
            if not (fraction >= delta):
                break

    s = _int_to_base36(int(integer))
    if frac_chars:
        s += "." + "".join(frac_chars)
    return ("-" if negative else "") + s


def _carry_backtrace(chars: list[str]) -> bool:
    """Walk emitted digits backwards, incrementing the last non-'z' one and
    dropping everything after it. Returns True when every digit rolled over
    (V8: the carry reaches the '.' and bumps the integer part instead)."""
    while chars:
        last = chars.pop()
        d = _BASE36_DIGITS.index(last)
        if d + 1 < 36:
            chars.append(_BASE36_DIGITS[d + 1])
            return False
    return True


def compute_token(tweet_id: str) -> str:
    """((Number(id) / 1e15) * Math.PI).toString(36).replace(/(0+|\\.)/g, '').

    Python `float` is IEEE-754 f64 like JS `Number`; `float(int(id)) / 1e15 *
    math.pi` performs the same single-precision-lossy ops in the same order.
    """
    x = float(int(tweet_id)) / 1e15 * math.pi
    return _ZERO_RUN_RE.sub("", _f64_to_base36_js(x))


# --------------------------------------------------------------------------- #
# Adapter
# --------------------------------------------------------------------------- #


class SyndicationResolver:
    name = "syndication"

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def resolve(self, ref: TweetRef) -> ResolvedTweet:
        params = {
            "id": ref.tweet_id,
            "lang": "en",
            "token": compute_token(ref.tweet_id),
            "features": FEATURES,
        }
        try:
            r = await self._client.get(SYNDICATION_URL, params=params)
        except httpx.HTTPError as err:
            raise _map_transport_error(err) from err

        if r.status_code == 200:
            try:
                payload = r.json()
            except ValueError as err:
                raise XdlError(
                    ErrorCode.UPSTREAM_ERROR, "syndication returned invalid JSON"
                ) from err
            return _map_payload(ref, payload)

        if r.status_code == 404:
            # Deleted OR sensitive/age-gated/fresh/unindexed — not trustworthy
            # as "post gone": fall through (round-2 semantics).
            raise XdlError(ErrorCode.NOT_EMBEDDABLE, f"syndication 404 for id {ref.tweet_id}")
        if r.status_code in (401, 403):
            raise XdlError(ErrorCode.AUTH_REQUIRED, f"syndication {r.status_code}")
        if r.status_code == 429:
            raise XdlError(
                ErrorCode.RATE_LIMITED,
                "syndication rate limited",
                retry_after=_retry_after_seconds(r.headers.get("retry-after")),
            )
        raise XdlError(ErrorCode.UPSTREAM_ERROR, f"syndication HTTP {r.status_code}")


def _map_transport_error(err: httpx.HTTPError) -> XdlError:
    if isinstance(err, httpx.RemoteProtocolError):
        return XdlError(ErrorCode.UPSTREAM_ERROR, str(err))
    if isinstance(err, httpx.InvalidURL):
        return XdlError(ErrorCode.URL_UNRECOGNIZED, str(err))
    return XdlError(ErrorCode.NETWORK, f"connection failed: {err}")


def _retry_after_seconds(raw: str | None) -> float | None:
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        return None  # HTTP-date parsing lands with M4 throttle work


def _map_payload(ref: TweetRef, payload: dict[str, object]) -> ResolvedTweet:
    if not payload:
        raise XdlError(ErrorCode.NOT_EMBEDDABLE, "syndication returned an empty object")
    if payload.get("__typename") == "TweetTombstone":
        raise XdlError(ErrorCode.NOT_EMBEDDABLE, "tombstone (protected/suspended/age-gated)")

    media_details = payload.get("mediaDetails")
    if not isinstance(media_details, list):
        # Syndication strips media details on sensitive posts too: not
        # trustworthy as "no video" — fall through; only the final
        # adapter's payload may report NO_VIDEO (round-2 semantics).
        raise XdlError(ErrorCode.NOT_EMBEDDABLE, "no mediaDetails in syndication payload")

    user = _as_dict(payload.get("user"))
    author = Author(
        screen_name=str(user.get("screen_name", "")),
        display_name=str(user.get("name", "")),
        verified=bool(user.get("verified", False)),
    )

    media = [_map_media(i, _as_dict(d)) for i, d in enumerate(media_details, start=1)]

    return ResolvedTweet(
        ref=ref,
        text=str(payload.get("text", "")),
        created_at=_parse_created_at(str(payload.get("created_at", ""))),
        author=author,
        media=media,
        source="syndication",
    )


def _as_dict(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _parse_created_at(raw: str) -> datetime:
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        pass
    for fmt in ("%a %b %d %H:%M:%S %z %Y", "%Y-%m-%dT%H:%M:%S.%f%z"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    raise XdlError(ErrorCode.UPSTREAM_ERROR, f"unparseable created_at: {raw!r}")


_KIND_MAP = {"photo": "photo", "video": "video", "animated_gif": "gif"}


def _map_media(index: int, detail: dict[str, object]) -> MediaAsset:
    raw_kind = str(detail.get("type", "photo"))
    kind: Literal["video", "gif", "photo"] = _KIND_MAP.get(raw_kind, "photo")  # type: ignore[assignment]

    variants: list[Variant] = []
    video_info = _as_dict(detail.get("video_info"))
    width = height = None
    original_info = _as_dict(detail.get("original_info"))
    if original_info:
        width = _opt_int(original_info.get("width"))
        height = _opt_int(original_info.get("height"))
    if not width or not height:
        sizes = _as_dict(detail.get("sizes"))
        large = _as_dict(sizes.get("large"))
        width = width or _opt_int(large.get("w"))
        height = height or _opt_int(large.get("h"))

    raw_variants = video_info.get("variants")
    if isinstance(raw_variants, list):
        for v in raw_variants:
            var = _as_dict(v)
            variants.append(
                Variant(
                    url=str(var.get("url", "")),
                    bitrate=_opt_int(var.get("bitrate")),
                    content_type=str(var.get("content_type", "video/mp4")),
                    width=width,
                    height=height,
                )
            )

    return MediaAsset(
        kind=kind,
        index=index,
        variants=variants,
        url_best=str(detail.get("media_url_https", "")),
        poster_url=str(detail.get("media_url_https") or "") or None,
    )


def _opt_int(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None
