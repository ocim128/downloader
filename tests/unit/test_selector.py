"""Quality policy matrix (docs/technical/02 selection + 09 test table)."""

from __future__ import annotations

import pytest

from xdl.media.selector import is_hls, select
from xdl.models import ErrorCode, MediaAsset, QualityPolicy, Variant, XdlError


def variant(url: str, bitrate: int | None, ct: str = "video/mp4", h: int | None = 720) -> Variant:
    return Variant(url=url, bitrate=bitrate, content_type=ct, height=h)


def video_asset(variants: list[Variant], index: int = 1) -> MediaAsset:
    return MediaAsset(kind="video", index=index, variants=variants)


LADDER = [
    variant("https://v.twimg.com/320.mp4", 320_000, h=320),
    variant("https://v.twimg.com/640.mp4", 832_000, h=640),
    variant("https://v.twimg.com/720.mp4", 2_176_000, h=720),
    variant("https://v.twimg.com/master.m3u8", None, ct="application/x-mpegURL", h=None),
]


def test_best_prefers_highest_bitrate_progressive() -> None:
    chosen = select([video_asset(LADDER)], QualityPolicy(mode="best"))
    assert [c.variant.url for c in chosen] == ["https://v.twimg.com/720.mp4"]


def test_worst() -> None:
    chosen = select([video_asset(LADDER)], QualityPolicy(mode="worst"))
    assert chosen[0].variant.bitrate == 320_000


def test_max_height_cap() -> None:
    chosen = select([video_asset(LADDER)], QualityPolicy(mode="max_height", max_height=640))
    assert chosen[0].variant.height == 640


def test_max_height_under_cap_wins() -> None:
    # 320p fits under a 500p cap: it satisfies <= cap even though 640p is closer
    chosen = select([video_asset(LADDER)], QualityPolicy(mode="max_height", max_height=500))
    assert chosen[0].variant.height == 320


def test_max_height_unmet_falls_to_closest() -> None:
    # nothing at all under a 300p cap: closest fit = smallest above it (320p)
    chosen = select([video_asset(LADDER)], QualityPolicy(mode="max_height", max_height=300))
    assert chosen[0].variant.height == 320


def test_hls_only_post_selects_m3u8() -> None:
    hls_only = [
        variant("https://v.twimg.com/master.m3u8", None, ct="application/x-mpegURL", h=None)
    ]
    chosen = select([video_asset(hls_only)], QualityPolicy(mode="best"))
    assert is_hls(chosen[0].variant)


def test_progressive_first_even_when_hls_exists() -> None:
    # round-2 policy: payloads publish no HLS bitrates — progressive wins
    chosen = select([video_asset(LADDER)], QualityPolicy(mode="best"))
    assert not is_hls(chosen[0].variant)


def test_photo_only_raises_no_video() -> None:
    with pytest.raises(XdlError) as exc:
        select([MediaAsset(kind="photo", index=1, url_best="https://pbs.twimg.com/x.jpg")],
               QualityPolicy())
    assert exc.value.code is ErrorCode.NO_VIDEO


def test_mixed_post_ignores_photos() -> None:
    media = [
        MediaAsset(kind="photo", index=1, url_best="https://pbs.twimg.com/a.jpg"),
        video_asset(LADDER, index=2),
    ]
    chosen = select(media, QualityPolicy())
    assert len(chosen) == 1 and chosen[0].asset.index == 2


def test_multi_video_selects_per_asset() -> None:
    media = [video_asset(LADDER, index=1), video_asset(list(reversed(LADDER)), index=2)]
    chosen = select(media, QualityPolicy())
    assert [c.asset.index for c in chosen] == [1, 2]
    assert all(c.variant.bitrate == 2_176_000 for c in chosen)


def test_gif_asset_selectable() -> None:
    gif = MediaAsset(
        kind="gif",
        index=1,
        variants=[variant("https://v.twimg.com/gif.mp4", 560_000, h=360)],
    )
    chosen = select([gif], QualityPolicy())
    assert chosen[0].asset.kind == "gif"


def test_quality_parse() -> None:
    assert QualityPolicy.parse(None).mode == "best"
    assert QualityPolicy.parse("worst").mode == "worst"
    p = QualityPolicy.parse("720")
    assert p.mode == "max_height" and p.max_height == 720
    with pytest.raises(XdlError):
        QualityPolicy.parse("banana")


def test_bitrate_tie_prefers_mp4() -> None:
    variants = [
        variant("https://v.twimg.com/webm.webm", 832_000, ct="video/webm"),
        variant("https://v.twimg.com/mp4.mp4", 832_000, ct="video/mp4"),
    ]
    chosen = select([video_asset(variants)], QualityPolicy())
    assert chosen[0].variant.content_type == "video/mp4"
