"""Fxtwitter adapter mapping (opt-in fallback; docs/technical/02 Adapter B)."""

from __future__ import annotations

import httpx
import pytest

from xdl.models import ErrorCode, TweetRef, XdlError
from xdl.resolve.fxtwitter import FxtwitterResolver

ID = "1750123456789012352"


def ref() -> TweetRef:
    return TweetRef(tweet_id=ID, screen_name=None,
                    canonical_url=f"https://x.com/i/status/{ID}")


def client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


PAYLOAD = {
    "tweet": {
        "id_str": ID,
        "url": f"https://x.com/someuser/status/{ID}",
        "text": "a video post",
        "created_at": "2026-09-30T21:14:03Z",
        "created_timestamp": "1759269243",
        "author": {"screen_name": "someuser", "name": "Some User"},
        "media": {
            "photos": [{"url": "https://pbs.twimg.com/media/a.jpg",
                        "width": 900, "height": 1600}],
            "videos": [
                {"url": "https://video.twimg.com/ext_tw_video/1/vid/avc1/1280x720/best.mp4",
                 "type": "video", "thumbnail": "https://pbs.twimg.com/ext_tw_video_thumb/t.jpg",
                 "width": 1280, "height": 720},
                {"url": "https://video.twimg.com/tweet_video/gif2.mp4",
                 "type": "gif", "thumbnail": "https://pbs.twimg.com/tweet_video_thumb/g2.jpg",
                 "width": 480, "height": 270},
            ],
        },
    }
}


async def test_mapping() -> None:
    async with client(lambda r: httpx.Response(200, json=PAYLOAD)) as c:
        tweet = await FxtwitterResolver(c).resolve(ref())

    assert tweet.author.screen_name == "someuser"
    assert tweet.source == "fxtwitter"
    kinds = [(m.kind, m.index) for m in tweet.media]
    assert kinds == [("photo", 1), ("video", 2), ("gif", 3)]
    video = tweet.media[1]
    assert video.variants[0].url.endswith("best.mp4")
    assert video.variants[0].bitrate is None  # no bitrate ladder
    assert video.variants[0].height == 720
    assert video.poster_url and video.poster_url.endswith("t.jpg")


async def test_created_timestamp_unix_fallback() -> None:
    payload = {"tweet": {"created_timestamp": "1759269243", "author": {}}}
    async with client(lambda r: httpx.Response(200, json=payload)) as c:
        tweet = await FxtwitterResolver(c).resolve(ref())
    assert tweet.created_at.year == 2025


async def test_404_maps_not_found() -> None:
    async with client(lambda r: httpx.Response(404)) as c:
        with pytest.raises(XdlError) as exc:
            await FxtwitterResolver(c).resolve(ref())
    assert exc.value.code is ErrorCode.NOT_FOUND


async def test_missing_tweet_object() -> None:
    async with client(lambda r: httpx.Response(200, json={"code": 404})) as c:
        with pytest.raises(XdlError) as exc:
            await FxtwitterResolver(c).resolve(ref())
    assert exc.value.code is ErrorCode.NOT_FOUND
