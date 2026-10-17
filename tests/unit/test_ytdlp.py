"""YtdlpResolver (Adapter D): info-dict mapping, error taxonomy, chain wiring.

Hermetic: yt-dlp's YoutubeDL is replaced by an injectable factory stub — no
extraction, no network. The real package IS imported for the version check
(bundled dependency), never invoked.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from xdl.config import Settings
from xdl.models import ErrorCode, TweetRef, XdlError
from xdl.resolve.base import FallbackChain, build_chain
from xdl.resolve.ytdlp import YtdlpResolver, stale_ytdlp_warning

REF = TweetRef(
    tweet_id="1750123456789012352",
    screen_name="someuser",
    canonical_url="https://x.com/someuser/status/1750123456789012352",
)


class DownloadError(Exception):
    """Shape-compatible stand-in for yt_dlp.utils.DownloadError."""


class StubYdl:
    def __init__(self, info: dict[str, Any] | None, error: Exception | None) -> None:
        self._info = info
        self._error = error
        self.requested: str | None = None

    def __enter__(self) -> StubYdl:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def extract_info(self, url: str, download: bool = True) -> dict[str, Any] | None:
        self.requested = url
        if self._error is not None:
            raise self._error
        return self._info


def resolver(info: dict[str, Any] | None = None, error: Exception | None = None) -> YtdlpResolver:
    stub: dict[str, StubYdl] = {}

    def factory(opts: dict[str, Any]) -> StubYdl:
        assert opts["skip_download"] is True and opts["quiet"] is True
        stub["ydl"] = StubYdl(info, error)
        return stub["ydl"]

    return YtdlpResolver(ydl_factory=factory)


def video_info(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": REF.tweet_id,
        "title": "a video post",
        "description": "full post text",
        "uploader": "@someuser",
        "timestamp": 1759266843,
        "thumbnail": "https://pbs.twimg.com/thumb.jpg",
        "formats": [
            {"url": "https://video.twimg.com/320.mp4", "ext": "mp4",
             "protocol": "https", "vcodec": "avc1", "acodec": "mp4a",
             "tbr": 320.0, "width": 320, "height": 480},
            {"url": "https://video.twimg.com/720.mp4", "ext": "mp4",
             "protocol": "https", "vcodec": "avc1", "acodec": "mp4a",
             "tbr": 2176.0, "width": 720, "height": 1280},
            {"url": "https://video.twimg.com/pl/master.m3u8", "ext": "mp4",
             "protocol": "m3u8_native", "vcodec": "avc1", "acodec": "mp4a",
             "tbr": None, "width": None, "height": None},
        ],
    }
    base.update(overrides)
    return base


async def test_maps_info_dict_to_resolved_tweet() -> None:
    tweet = await resolver(video_info()).resolve(REF)
    assert tweet.source == "ytdlp"
    assert tweet.ref == REF
    assert tweet.author.screen_name == "someuser"  # leading @ stripped
    assert tweet.created_at == datetime.fromtimestamp(1759266843, tz=UTC)
    assert tweet.text == "full post text"  # description wins over title
    (asset,) = tweet.media
    assert asset.kind == "video"  # formats carry audio
    urls = [v.url for v in asset.variants]
    assert urls == [
        "https://video.twimg.com/320.mp4",
        "https://video.twimg.com/720.mp4",
        "https://video.twimg.com/pl/master.m3u8",
    ]
    best = asset.variants[1]
    assert best.bitrate == 2176000  # tbr kbps -> bps
    assert (best.width, best.height) == (720, 1280)
    hls_variant = asset.variants[2]
    assert hls_variant.content_type == "application/x-mpegURL"
    assert hls_variant.bitrate is None


async def test_muted_formats_map_to_gif_kind() -> None:
    info = video_info(formats=[
        {"url": "https://video.twimg.com/gif.mp4", "ext": "mp4",
         "protocol": "https", "vcodec": "avc1", "acodec": "none",
         "tbr": 800.0, "width": 480, "height": 480},
    ])
    tweet = await resolver(info).resolve(REF)
    assert tweet.media[0].kind == "gif"  # X serves GIFs as silent MP4s


async def test_playlist_entry_unwrapped() -> None:
    info = {"_type": "playlist", "entries": [video_info(title="first")]}
    tweet = await resolver(info).resolve(REF)
    assert tweet.text == "full post text"


async def test_upload_date_fallback_and_missing_dates() -> None:
    tweet = await resolver(video_info(timestamp=None, upload_date="20260930")).resolve(REF)
    assert tweet.created_at.date().isoformat() == "2026-09-30"
    with pytest.raises(XdlError) as excinfo:
        await resolver(video_info(timestamp=None, upload_date=None)).resolve(REF)
    assert excinfo.value.code is ErrorCode.UPSTREAM_ERROR


async def test_no_video_formats_raises_no_video() -> None:
    with pytest.raises(XdlError) as excinfo:
        await resolver(video_info(formats=[])).resolve(REF)
    assert excinfo.value.code is ErrorCode.NO_VIDEO


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("ERROR: [twitter] 1750: HTTP Error 404: Not Found", ErrorCode.NOT_FOUND),
        ("ERROR: [twitter] 1750: This post is unavailable", ErrorCode.NOT_FOUND),
        ("ERROR: [twitter] 1750: Login needed to view this content", ErrorCode.AUTH_REQUIRED),
        ("ERROR: [twitter] 1750: HTTP Error 401: Unauthorized", ErrorCode.AUTH_REQUIRED),
        ("ERROR: [twitter] 1750: Content is sensitive", ErrorCode.NOT_EMBEDDABLE),
        ("ERROR: [twitter] 1750: Protected account", ErrorCode.NOT_EMBEDDABLE),
        ("ERROR: unable to download webpage: connection timed out", ErrorCode.NETWORK),
        ("ERROR: [twitter] 1750: something novel happened", ErrorCode.UPSTREAM_ERROR),
    ],
)
async def test_error_taxonomy_mapping(message: str, expected: ErrorCode) -> None:
    with pytest.raises(XdlError) as excinfo:
        await resolver(error=DownloadError(message)).resolve(REF)
    assert excinfo.value.code is expected


def test_stale_ytdlp_warning_thresholds() -> None:
    # Dates are relative to the INSTALLED yt-dlp's version date, so the test
    # stays correct as the dependency is upgraded.
    import re
    from datetime import timedelta

    import yt_dlp

    m = re.match(r"^(\d{4})\.(\d{1,2})\.(\d{1,2})", yt_dlp.version.__version__)
    assert m is not None, "released yt-dlp versions are date-based"
    released = datetime(int(m[1]), int(m[2]), int(m[3]), tzinfo=UTC)

    fresh_day = released + timedelta(days=7)
    stale_day = released + timedelta(days=30)
    assert stale_ytdlp_warning(today=fresh_day) is None
    warning = stale_ytdlp_warning(today=stale_day)
    assert warning is not None and "pip install -U yt-dlp" in warning
    assert "16 days old" in warning or "days old" in warning  # 30 - release-day rounding


def test_stale_warning_ignores_hash_versions(monkeypatch: pytest.MonkeyPatch) -> None:
    import yt_dlp.version

    monkeypatch.setattr(yt_dlp.version, "__version__", "abc123deadbeef")
    assert stale_ytdlp_warning() is None  # dev build: cannot age-check


# --------------------------------------------------------------------------- #
# build_chain wiring (docs/technical/02 chain order)
# --------------------------------------------------------------------------- #


def _client() -> httpx.AsyncClient:
    # Never used for requests in these tests (SyndicationResolver is only
    # constructed, not called).
    return httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(500)))


def test_build_chain_auto_includes_ytdlp() -> None:
    chain = build_chain(Settings(resolver={"fallback": "auto"}), _client())
    assert chain.names == ["syndication", "ytdlp"]


def test_build_chain_explicit_ytdlp() -> None:
    chain = build_chain(Settings(resolver={"fallback": "ytdlp"}), _client())
    assert chain.names == ["syndication", "ytdlp"]


def test_build_chain_fxtwitter_and_none() -> None:
    assert build_chain(Settings(resolver={"fallback": "fxtwitter"}), _client()).names == [
        "syndication", "fxtwitter",
    ]
    assert build_chain(Settings(resolver={"fallback": "none"}), _client()).names == [
        "syndication",
    ]


async def test_chain_falls_through_to_ytdlp_stub() -> None:
    """Syndication 404 (NOT_EMBEDDABLE) -> yt-dlp hop resolves the post."""
    from xdl.resolve.syndication import SyndicationResolver

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        chain = FallbackChain([
            SyndicationResolver(client),
            resolver(video_info()),
        ])
        tweet = await chain.resolve(REF)
    assert tweet.source == "ytdlp"
    assert tweet.author.screen_name == "someuser"
