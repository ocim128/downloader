"""Sidecar payload schema + atomic write (docs/technical/06)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from xdl.models import Author, Chosen, MediaAsset, ResolvedTweet, TweetRef, Variant
from xdl.storage.sidecar import build_payload, sidecar_path, write_sidecar


def tweet() -> ResolvedTweet:
    return ResolvedTweet(
        ref=TweetRef(tweet_id="1750123456789012352", screen_name="someuser",
                     canonical_url="https://x.com/someuser/status/1750123456789012352"),
        text="caption text",
        created_at=datetime(2026, 9, 30, 21, 14, 3, tzinfo=UTC),
        author=Author(screen_name="someuser", display_name="Some User", verified=False),
        media=[MediaAsset(kind="video", index=2,
                          url_best="https://pbs.twimg.com/thumb.jpg",
                          poster_url="https://pbs.twimg.com/thumb.jpg")],
        source="syndication",
    )


def chosen() -> Chosen:
    return Chosen(
        asset=tweet().media[0],
        variant=Variant(url="https://video.twimg.com/ext_tw_video/1/720.mp4",
                        bitrate=2176000, content_type="video/mp4",
                        width=1280, height=720),
    )


def test_payload_schema(tmp_path: Path) -> None:
    media_file = tmp_path / "2026-09-30_1750123456789012352_v2.mp4"
    payload = build_payload(tweet(), chosen(), media_file, size_bytes=24117248)

    assert payload["schema_version"] == 1
    assert payload["tweet_id"] == "1750123456789012352"
    assert payload["url"] == "https://x.com/someuser/status/1750123456789012352"
    assert payload["created_at"] == "2026-09-30T21:14:03Z"  # ISO Z, seconds
    assert payload["author"] == {"screen_name": "someuser", "name": "Some User",
                                 "verified": False}
    assert payload["source"] == "syndication"
    assert payload["stats"] == {"likes": None, "reposts": None,
                                "replies": None, "views": None}
    (entry,) = payload["media"]  # one sidecar per media file
    assert entry["index"] == 2
    assert entry["file"] == media_file.name
    assert entry["bitrate"] == 2176000
    assert entry["size_bytes"] == 24117248
    assert entry["poster_url"] == "https://pbs.twimg.com/thumb.jpg"
    assert payload["downloaded_at"].endswith("Z")


async def test_write_sidecar_atomic(tmp_path: Path) -> None:
    media_file = tmp_path / "video.mp4"
    media_file.write_bytes(b"data")
    payload = build_payload(tweet(), chosen(), media_file, size_bytes=4)

    target = await write_sidecar(media_file, payload)

    assert target == sidecar_path(media_file)
    assert target.name == "video.mp4.meta.json"
    assert not (tmp_path / "video.mp4.meta.json.part").exists()
    on_disk = json.loads(target.read_text(encoding="utf-8"))
    assert on_disk["tweet_id"] == "1750123456789012352"
