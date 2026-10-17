"""Variant selection per quality policy (docs/technical/02, docs/technical/04).

Policy is progressive-first (audit round 2): payload variants publish no HLS
bitrates, so a progressive MP4 always wins over HLS unless none exists.
"""

from __future__ import annotations

from xdl.models import Chosen, ErrorCode, MediaAsset, QualityPolicy, Variant, XdlError

HLS_CONTENT_TYPES = frozenset(
    {"application/x-mpegurl", "application/vnd.apple.mpegurl"}
)


def is_hls(variant: Variant) -> bool:
    return variant.content_type.lower() in HLS_CONTENT_TYPES


def select(media: list[MediaAsset], policy: QualityPolicy) -> list[Chosen]:
    """Choose one variant per downloadable (video/gif) asset.

    Photo-only posts raise NO_VIDEO — expected noise in mixed batches.
    """
    chosen: list[Chosen] = []
    for asset in media:
        if asset.kind == "photo":
            continue
        chosen.append(Chosen(asset=asset, variant=choose(asset, policy)))
    if not chosen:
        raise XdlError(ErrorCode.NO_VIDEO, "post has no video (photos only)")
    return chosen


def choose(asset: MediaAsset, policy: QualityPolicy) -> Variant:
    progressive = [v for v in asset.variants if not is_hls(v)]
    hls = [v for v in asset.variants if is_hls(v)]
    pool = progressive or hls  # progressive-first

    if not pool:
        raise XdlError(ErrorCode.NO_VIDEO, f"media #{asset.index} has no variants")

    if hls and not progressive:
        # Best available is HLS: needs the ffmpeg remux path (media/hls, M5).
        return _prefer_mp4(hls)

    assert progressive
    if policy.mode == "worst":
        return min(progressive, key=_bitrate_key)
    if policy.mode == "max_height" and policy.max_height is not None:
        capped = [
            v
            for v in progressive
            if v.height is not None and v.height <= policy.max_height
        ]
        if capped:
            return max(capped, key=_bitrate_key)
        # Nothing under the cap: closest fit = the smallest variant above it.
        return min(progressive, key=_height_key)
    return max(progressive, key=_bitrate_key)  # best


def _bitrate_key(v: Variant) -> tuple[int, bool]:
    # bitrate (bps); mp4 wins ties over other containers.
    return (v.bitrate or 0, v.content_type == "video/mp4")


def _height_key(v: Variant) -> tuple[int, int]:
    return (v.height or 10**9, -(v.bitrate or 0))


def _prefer_mp4(variants: list[Variant]) -> Variant:
    mp4s = [v for v in variants if v.content_type == "video/mp4"]
    return mp4s[0] if mp4s else variants[0]
