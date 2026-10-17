#!/usr/bin/env python3
"""M1 spike: record a syndication tweet-result fixture, fully standalone.

Deliberately does NOT import anything from src/xdl — the core pipeline may not
exist yet (parallel lane). Run from the repo root:

    .venv/Scripts/python scripts/record_fixture.py <url-or-id> <name>

Writes tests/fixtures/syndication/<name>.json as an envelope:
    {"recorded_at", "tweet_id", "source_url", "http_status",
     "response": <parsed body>, "raw_response_on_error": <text, 404/tomb cases>}

Network access is allowed ONLY in scripts/ — never import or call this from pytest.
"""

from __future__ import annotations

import json
import math
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "syndication"

SYNDICATION_URL = "https://cdn.syndication.twimg.com/tweet-result"
# Captured verbatim from react-tweet's packages/react-tweet/src/api/fetch-tweet.ts
# (spike date 2026-10-17) — per docs/technical/02-media-resolution.md.
FEATURES = ";".join(
    [
        "tfw_timeline_list:",
        "tfw_follower_count_sunset:true",
        "tfw_tweet_edit_backend:on",
        "tfw_refsrc_session:on",
        "tfw_fosnr_soft_interventions_enabled:on",
        "tfw_show_birdwatch_pivots_enabled:on",
        "tfw_show_business_verified_badge:on",
        "tfw_duplicate_scribes_to_settings:on",
        "tfw_use_profile_image_shape_enabled:on",
        "tfw_show_blue_verified_badge:on",
        "tfw_legacy_timeline_sunset:true",
        "tfw_show_gov_verified_badge:on",
        "tfw_show_business_affiliate_badge:on",
        "tfw_tweet_edit_frontend:on",
    ]
)
DEFAULT_UA = "curl/8.0.1"  # pre-verified 2026-10-17: guest 200 with a plain curl UA

_B36_DIGITS = "0123456789abcdefghijklmnopqrstuvwxyz"


# ---------------------------------------------------------------------------
# Token algorithm — exact port of react-tweet getToken() semantics
# (docs/technical/02-media-resolution.md). This is the reference copy for the
# spike; src/xdl/resolve/syndication.py will reimplement (Agent A) and lock it
# against the same golden values.
#
# NOTE (spike finding, 2026-10-17): docs/technical/02's `_f64_to_base36_js`
# sketch ("exact rational of the f64, then shortest round-trip trim") does NOT
# reproduce the real JS engines. React-tweet runs in browsers/Node, whose
# toString(36) (V8 `DoubleToRadixStringView`, src/numbers/conversions.cc)
# generates digits by repeated f64 multiplication with a delta error bound and
# round-to-even carry into already-written digits. This file ports THAT
# algorithm verbatim; `_f64_to_base36_js` below is bit-identical to Node for
# all probed doubles (sweep in tests/fixtures/golden_tokens.json derivation).
# ---------------------------------------------------------------------------


def _f64_to_base36_js(value: float) -> str:
    """Verbatim port of V8 DoubleToRadixStringView(value, 36).

    i.e. Number.prototype.toString(36) — digits are generated from the f64
    itself (repeated multiplication), not from an exact rational.
    """
    if value != value or math.isinf(value):
        raise ValueError("toString(36) is only defined for finite non-NaN doubles")
    if value == 0.0:  # callers handle ±0 before this function ("0", signless)
        return "0"
    negative = value < 0
    if negative:
        value = -value

    integer = float(math.floor(value))
    fraction = value - integer
    # Only compute fractional digits up to the input double's precision.
    delta = 0.5 * (math.nextafter(value, math.inf) - value)
    delta_is_positive = True
    if delta <= 0.0:
        # CPython never flushes denormals; the flush branch is unreachable here.
        # (V8 checks base::FPU::GetFlushDenormals(); mirrored for verbatim-ness).
        delta = math.nextafter(0.0, math.inf)

    integer_chars: list[str] = []
    fraction_chars: list[str] = []

    if delta_is_positive and fraction >= delta:
        while True:
            # Shift up by one digit.
            fraction *= 36
            delta *= 36
            # Write digit.
            digit = int(fraction)
            fraction_chars.append(_B36_DIGITS[digit])
            # Calculate remainder.
            fraction -= digit
            # Round to even.
            if fraction > 0.5 or (fraction == 0.5 and digit & 1):
                if fraction + delta > 1.0:
                    # Carry-over: back-trace already written digits.
                    while True:
                        if not fraction_chars:
                            integer += 1
                            break
                        digit = _B36_DIGITS.index(fraction_chars.pop())
                        if digit + 1 < 36:
                            fraction_chars.append(_B36_DIGITS[digit + 1])
                            break
                    break
            if fraction < delta:
                break

    # Compute integer digits. Fill unrepresented digits with zero.
    # Double(integer / 36).Exponent() > 0  ==  integer / 36 >= 2^53:
    # below that, an f64 holds the quotient's unit position exactly (fmod path).
    while integer / 36 >= 2.0**53:
        integer /= 36
        integer_chars.append("0")
    while integer > 0:
        remainder = math.fmod(integer, 36.0)
        integer_chars.append(_B36_DIGITS[int(remainder)])
        integer = (integer - remainder) / 36.0

    s = "".join(reversed(integer_chars))
    if fraction_chars:
        s += "." + "".join(fraction_chars)
    elif not s:
        s = "0"
    if s.startswith("."):  # caller normalization (matches "0.c" for 1/3)
        s = "0" + s
    if negative:
        s = "-" + s
    return s


def get_token(tweet_id: str) -> str:
    """getToken(id) = ((Number(id)/1e15)*Math.PI).toString(36).replace(/(0+|\\.)/g,'')."""
    # JS Number(id) for <=19-digit ids is lossy f64: mimic with float(int(id)).
    x = float(int(tweet_id)) / 1e15 * math.pi
    s = _f64_to_base36_js(x)
    return re.sub(r"(0+|\.)", "", s)


# ---------------------------------------------------------------------------
# URL -> id extraction (loose; the real parser lives in src/xdl/urls.py)
# ---------------------------------------------------------------------------


def extract_id(raw: str) -> tuple[str, str]:
    """Return (tweet_id, source_url-ish) from a bare id or any post URL form."""
    raw = raw.strip()
    if raw.isdigit():
        return raw, raw
    m = re.search(r"(?:twitter\.com|x\.com|vxtwitter\.com)/.*/status(?:es)?/(\d+)", raw)
    if m:
        source = raw
        p = urlparse(source)
        if not p.scheme:
            source = "https://" + raw
        return m.group(1), source
    raise SystemExit(f"cannot extract tweet id from: {raw!r}")


# ---------------------------------------------------------------------------
# Fixture recording
# ---------------------------------------------------------------------------


def record(tweet_id: str, name: str, source_url: str) -> None:
    token = get_token(tweet_id)
    params = {"id": tweet_id, "lang": "en", "features": FEATURES, "token": token}
    headers = {"User-Agent": DEFAULT_UA, "Accept": "application/json"}
    url = httpx.Request("GET", SYNDICATION_URL, params=params).url

    print(f"GET {url}")
    print(f"token = {token!r}")
    envelope: dict[str, object] = {
        "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "tweet_id": tweet_id,
        "source_url": source_url,
        "http_status": None,
        "request_token": token,
        "request_features": FEATURES,
    }
    try:
        resp = httpx.get(SYNDICATION_URL, params=params, headers=headers, timeout=30)
    except Exception as exc:  # keep the failure shape itself as the fixture
        envelope["http_status"] = 0
        envelope["response"] = {"raw": f"network-error: {exc!r}"}
        status = 0
    else:
        status = resp.status_code
        envelope["http_status"] = status
        try:
            envelope["response"] = resp.json()
        except ValueError:
            envelope["response"] = {"raw": resp.text}

    if status == 200:
        body = envelope["response"]
        tn = body.get("__typename") if isinstance(body, dict) else None
        kinds = []
        if isinstance(body, dict):
            for md in body.get("mediaDetails") or []:
                kinds.append(md.get("type"))
            videos = [md for md in body.get("mediaDetails") or [] if md.get("video_info")]
            only_hls = videos and all(
                not any(v.get("content_type") == "video/mp4" for v in md["video_info"]["variants"])
                for md in videos
            )
        else:
            only_hls = False
        print(f"HTTP 200 ({status}); __typename={tn}; media types={kinds}; hls_only={only_hls}")
        shape = _describe(body)
        print(f"shape: {shape}")

    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    dest = FIXTURE_DIR / f"{name}.json"
    dest.write_text(json.dumps(envelope, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {dest}")


def _describe(body: object) -> str:
    if not isinstance(body, dict):
        return f"non-object: {body!r}"
    if not body:
        return "EMPTY OBJECT"
    tn = body.get("__typename")
    if tn == "TweetTombstone":
        return "typename=TweetTombstone tombstone"
    if tn and tn != "Tweet":
        return f"typename={tn} tombstone-like"
    media = body.get("mediaDetails") or []
    kinds = [m.get("type") for m in media]
    n_variants = [len((m.get("video_info") or {}).get("variants") or []) for m in media]
    return f"media={kinds} variants_per_video={n_variants}"


def verify_token_goldens(ids: list[str]) -> int:
    """Compute tokens and live-verify each with a GET (expect HTTP 200)."""
    failures = 0
    for tid in ids:
        token = get_token(tid)
        resp = httpx.get(
            SYNDICATION_URL,
            params={"id": tid, "lang": "en", "features": FEATURES, "token": token},
            headers={"User-Agent": DEFAULT_UA, "Accept": "application/json"},
            timeout=30,
        )
        ok = resp.status_code == 200
        if not ok:
            failures += 1
        print(f"id={tid:<20} token={token:<14} HTTP {resp.status_code} {'OK' if ok else 'FAIL'}")
        if not ok:
            print(f"    body head: {resp.text[:200]!r}")
    return failures


def main(argv: list[str]) -> int:
    if len(argv) >= 2 and argv[1] == "--verify-goldens":
        ids = argv[2:]
        return 1 if verify_token_goldens(ids) else 0
    if len(argv) != 3:
        print(__doc__)
        return 2
    tweet_id, source = extract_id(argv[1])
    record(tweet_id, argv[2], source)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
