#!/usr/bin/env python3
"""M1 spike: discover candidate tweet ids + auto-classify via syndication.

Standalone (no src/xdl imports), run from repo root:
    .venv/Scripts/python scripts/find_candidates.py <screen_name> [...]
    .venv/Scripts/python scripts/find_candidates.py --check <id> [...]

Spikes only — never called from pytest.
"""

from __future__ import annotations

import json
import re
import sys
from typing import Any

import httpx

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
# re-use the standalone token/features implementation
from record_fixture import DEFAULT_UA, FEATURES, SYNDICATION_URL, get_token  # noqa: E402

TIMELINE_URL = "https://syndication.twitter.com/srv/timeline-profile/screen-name/{name}"


def timeline_ids(screen_name: str) -> list[str]:
    resp = httpx.get(
        TIMELINE_URL.format(name=screen_name),
        params={"showReplies": "false", "hideBorder": "true", "limit": "50"},
        headers={"User-Agent": DEFAULT_UA},
        timeout=30,
        follow_redirects=True,
    )
    ids = sorted(set(re.findall(r'data-tweet-id="(\d{6,})"', resp.text)))
    if not ids:  # maybe JSON body differs; raw fallback
        ids = sorted(set(re.findall(r'"status/(\d{16,})"', resp.text)))
    if not ids:
        print(f"[{screen_name}] HTTP {resp.status_code}; no ids found in {len(resp.text)} bytes")
        print(resp.text[:300])
        return []
    return ids


def classify(tweet_id: str) -> dict[str, Any]:
    resp = httpx.get(
        SYNDICATION_URL,
        params={"id": tweet_id, "lang": "en", "features": FEATURES, "token": get_token(tweet_id)},
        headers={"User-Agent": DEFAULT_UA, "Accept": "application/json"},
        timeout=30,
    )
    out: dict[str, Any] = {"id": tweet_id, "status": resp.status_code, "type": "?", "detail": ""}
    try:
        body = resp.json()
    except ValueError:
        out["type"] = "non-json"
        out["detail"] = resp.text[:200]
        return out
    if resp.status_code != 200 or not isinstance(body, dict) or not body:
        out["type"] = "404/empty"
        return out
    tn = body.get("__typename")
    if tn == "TweetTombstone":
        out["type"] = "tombstone"
        return out
    if tn == "TweetWithheld":
        out["type"] = "withheld"
        return out
    media = body.get("mediaDetails") or []
    kinds = [m.get("type") for m in media]
    n_videos = sum(1 for m in media if m.get("video_info"))
    hls_only = False
    for m in media:
        vi = m.get("video_info")
        if vi:
            vs = vi.get("variants") or []
            hls_only = hls_only or (vs and not any(
                v.get("content_type") == "video/mp4" for v in vs
            ))
    videos_av = sum(
        1
        for m in media
        if m.get("video_info")
        and any(v.get("content_type") == "video/mp4" for v in (m.get("video_info") or {}).get("variants") or [])
    )
    if n_videos == 0:
        out["type"] = "photo-only" if kinds else "no-media"
        out["detail"] = f"kinds={kinds}"
    elif videos_av >= 2:
        out["type"] = "multi-video"
        out["detail"] = f"kinds={kinds}"
    elif hls_only and videos_av == 0:
        out["type"] = "hls-only"
        out["detail"] = f"kinds={kinds}"
    elif "animated_gif" in kinds and videos_av == 0:
        out["type"] = "gif"
        out["detail"] = f"kinds={kinds}"
    else:
        out["type"] = "video"
        out["detail"] = f"kinds={kinds}"
    return out


def main(argv: list[str]) -> int:
    if len(argv) >= 2 and argv[1] == "--check":
        rows = [classify(t) for t in argv[2:]]
    else:
        names = argv[1:] or ["espn"]
        ids: list[str] = []
        for name in names:
            ids.extend(timeline_ids(name))
        rows = [classify(i) for i in dict.fromkeys(ids)]
    for r in rows:
        print(f"{r['id']:<20} {r['status']} {r['type']:<12} {r['detail']}")
    json.dump(rows, open(".tmp_candidates.json", "w", encoding="utf-8"), indent=1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
