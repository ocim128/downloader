"""Syndication adapter: mapping from (inline) recorded-shape JSON + request
shape assertions. Swappable for Agent B's tests/fixtures/syndication/*.json
when they land — the payloads mirror the same shapes."""

from __future__ import annotations

import httpx
import pytest

from xdl.models import ErrorCode, TweetRef, XdlError
from xdl.resolve.syndication import FEATURES, SyndicationResolver, compute_token

ID = "1750123456789012352"


def ref(tweet_id: str = ID) -> TweetRef:
    return TweetRef(tweet_id=tweet_id, screen_name=None,
                    canonical_url=f"https://x.com/i/status/{tweet_id}")


def client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def video_payload(
    *,
    typename: str | None = "Tweet",
    media: list[dict] | None = None,
    with_user: bool = True,
) -> dict:
    payload: dict[str, object] = {
        "__typename": typename or "Tweet",
        "id_str": ID,
        "text": "check this out https://t.co/xyz",
        "created_at": "2026-09-30T21:14:03.000Z",
        "mediaDetails": media if media is not None else [
            {
                "type": "video",
                "media_url_https": "https://pbs.twimg.com/ext_tw_video_thumb/123/p.jpg",
                "original_info": {"width": 1280, "height": 720},
                "video_info": {
                    "variants": [
                        {"url": "https://video.twimg.com/ext_tw_video/1/320.mp4",
                         "bitrate": 320000, "content_type": "video/mp4"},
                        {"url": "https://video.twimg.com/ext_tw_video/1/720.mp4",
                         "bitrate": 2176000, "content_type": "video/mp4"},
                        {"url": "https://video.twimg.com/ext_tw_video/1/master.m3u8",
                         "content_type": "application/x-mpegURL"},
                    ]
                },
            }
        ],
    }
    if with_user:
        payload["user"] = {"screen_name": "someuser", "name": "Some User",
                           "verified": True}
    return payload


async def test_request_shape_token_and_features() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.url.params)
        return httpx.Response(200, json=video_payload())

    async with client(handler) as c:
        await SyndicationResolver(c).resolve(ref())
    assert seen["id"] == ID
    assert seen["lang"] == "en"
    assert seen["token"] == compute_token(ID)
    assert seen["features"] == FEATURES
    assert "tfw_timeline_list" in FEATURES  # the drift-prone member


async def test_mapping_happy_path() -> None:
    async with client(lambda r: httpx.Response(200, json=video_payload())) as c:
        tweet = await SyndicationResolver(c).resolve(ref())

    assert tweet.ref.tweet_id == ID
    assert tweet.text.startswith("check this out")
    assert tweet.author.screen_name == "someuser"
    assert tweet.author.display_name == "Some User"
    assert tweet.author.verified is True
    assert tweet.source == "syndication"
    assert tweet.created_at.year == 2026

    (asset,) = tweet.media
    assert asset.kind == "video" and asset.index == 1
    assert asset.poster_url and asset.poster_url.startswith("https://pbs.twimg.com/")
    assert len(asset.variants) == 3
    best = max((v for v in asset.variants if v.bitrate), key=lambda v: v.bitrate or 0)
    assert best.url.endswith("720.mp4")
    hls = [v for v in asset.variants if v.content_type == "application/x-mpegURL"]
    assert hls and hls[0].bitrate is None
    assert asset.variants[0].width == 1280 and asset.variants[0].height == 720


async def test_mapping_prefers_id_str_over_numeric_id() -> None:
    payload = video_payload()
    payload["id"] = 1.7512345678901232e18  # lossy float form of the id
    async with client(lambda r: httpx.Response(200, json=payload)) as c:
        tweet = await SyndicationResolver(c).resolve(ref())
    assert tweet.ref.tweet_id == ID


async def test_gif_and_multi_video_mapping() -> None:
    payload = video_payload(media=[
        {"type": "animated_gif",
         "media_url_https": "https://pbs.twimg.com/tweet_video_thumb/g.gif",
         "sizes": {"large": {"w": 480, "h": 270}},
         "video_info": {"variants": [
             {"url": "https://video.twimg.com/tweet_video/g.mp4",
              "bitrate": 0, "content_type": "video/mp4"}]}},
        {"type": "video",
         "media_url_https": "https://pbs.twimg.com/ext_tw_video_thumb/2.jpg",
         "original_info": {"width": 1920, "height": 1080},
         "video_info": {"variants": [
             {"url": "https://video.twimg.com/ext_tw_video/2/1080.mp4",
              "bitrate": 4480000, "content_type": "video/mp4"}]}},
    ])
    async with client(lambda r: httpx.Response(200, json=payload)) as c:
        tweet = await SyndicationResolver(c).resolve(ref())
    kinds = [(m.kind, m.index) for m in tweet.media]
    assert kinds == [("gif", 1), ("video", 2)]
    # sizes fallback when original_info absent
    assert tweet.media[0].variants[0].width == 480


async def test_tombstone_maps_not_embeddable() -> None:
    async with client(lambda r: httpx.Response(
            200, json={"__typename": "TweetTombstone"})) as c:
        with pytest.raises(XdlError) as exc:
            await SyndicationResolver(c).resolve(ref())
    assert exc.value.code is ErrorCode.NOT_EMBEDDABLE


async def test_404_maps_not_embeddable_not_not_found() -> None:
    # round-2 semantics: 404 = deleted OR sensitive/fresh; falls through
    async with client(lambda r: httpx.Response(404)) as c:
        with pytest.raises(XdlError) as exc:
            await SyndicationResolver(c).resolve(ref())
    assert exc.value.code is ErrorCode.NOT_EMBEDDABLE


async def test_empty_object_maps_not_embeddable() -> None:
    async with client(lambda r: httpx.Response(200, json={})) as c:
        with pytest.raises(XdlError) as exc:
            await SyndicationResolver(c).resolve(ref())
    assert exc.value.code is ErrorCode.NOT_EMBEDDABLE


async def test_no_media_details_maps_not_embeddable() -> None:
    # sensitive posts strip mediaDetails: only the FINAL adapter may say NO_VIDEO
    payload = video_payload()
    del payload["mediaDetails"]
    async with client(lambda r: httpx.Response(200, json=payload)) as c:
        with pytest.raises(XdlError) as exc:
            await SyndicationResolver(c).resolve(ref())
    assert exc.value.code is ErrorCode.NOT_EMBEDDABLE


async def test_401_403_map_auth_required() -> None:
    for status in (401, 403):
        async with client(lambda r, s=status: httpx.Response(s)) as c:
            with pytest.raises(XdlError) as exc:
                await SyndicationResolver(c).resolve(ref())
        assert exc.value.code is ErrorCode.AUTH_REQUIRED


async def test_429_maps_rate_limited_with_retry_after() -> None:
    async with client(lambda r: httpx.Response(429, headers={"retry-after": "42"})) as c:
        with pytest.raises(XdlError) as exc:
            await SyndicationResolver(c).resolve(ref())
    assert exc.value.code is ErrorCode.RATE_LIMITED
    assert exc.value.retry_after == 42.0


async def test_5xx_maps_upstream_error() -> None:
    async with client(lambda r: httpx.Response(502)) as c:
        with pytest.raises(XdlError) as exc:
            await SyndicationResolver(c).resolve(ref())
    assert exc.value.code is ErrorCode.UPSTREAM_ERROR


async def test_network_maps_network() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("too slow")

    async with client(handler) as c:
        with pytest.raises(XdlError) as exc:
            await SyndicationResolver(c).resolve(ref())
    assert exc.value.code is ErrorCode.NETWORK


async def test_photo_only_maps_to_photo_assets() -> None:
    payload = video_payload(media=[
        {"type": "photo", "media_url_https": "https://pbs.twimg.com/media/a.jpg",
         "sizes": {"large": {"w": 2048, "h": 1365}}},
    ])
    async with client(lambda r: httpx.Response(200, json=payload)) as c:
        tweet = await SyndicationResolver(c).resolve(ref())
    assert tweet.media[0].kind == "photo"
    assert tweet.media[0].url_best.endswith("a.jpg")
    assert tweet.media[0].variants == []
