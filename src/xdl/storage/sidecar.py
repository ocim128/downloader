"""`<basename>.meta.json` sidecar writer (docs/technical/06).

One sidecar per media file (multi-video posts keep each `_v2.mp4`
self-describing). Atomic write: temp + rename. Write order for crash-safety
lives in the orchestrator: media rename -> sidecar -> state row.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime
from pathlib import Path

from xdl import __version__
from xdl.models import Chosen, ResolvedTweet

SCHEMA_VERSION = 1


def build_payload(
    tweet: ResolvedTweet,
    chosen: Chosen,
    media_file: Path,
    *,
    size_bytes: int | None,
) -> dict[str, object]:
    variant = chosen.variant
    asset = chosen.asset
    media_entry: dict[str, object] = {
        "index": asset.index,
        "kind": asset.kind,
        "file": media_file.name,
        "width": variant.width,
        "height": variant.height,
        "bitrate": variant.bitrate,
        "content_type": variant.content_type,
        "source_variant_url": variant.url,
        "size_bytes": size_bytes,
        "poster_url": asset.poster_url or asset.url_best or None,
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "tweet_id": tweet.ref.tweet_id,
        "url": tweet.ref.canonical_url,
        "text": tweet.text,
        "created_at": _iso_z(tweet.created_at),
        "author": {
            "screen_name": tweet.author.screen_name,
            "name": tweet.author.display_name,
            "verified": tweet.author.verified,
        },
        "media": [media_entry],
        "stats": {"likes": None, "reposts": None, "replies": None, "views": None},
        "source": tweet.source,
        "downloaded_at": _iso_z(datetime.now(tz=UTC)),
        "xdl_version": __version__,
    }


def sidecar_path(media_file: Path) -> Path:
    return media_file.with_name(f"{media_file.name}.meta.json")


async def write_sidecar(media_file: Path, payload: dict[str, object]) -> Path:
    """Atomic sidecar write: `<media>.meta.json.part` -> rename."""
    target = sidecar_path(media_file)
    part = target.with_name(target.name + ".part")
    await asyncio.to_thread(_write_atomic, part, target, payload)
    return target


def _write_atomic(part: Path, target: Path, payload: dict[str, object]) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with part.open("w", encoding="utf-8", newline="\n") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    part.replace(target)


def _iso_z(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
