"""Adapter D — yt-dlp fallback (auto; bundled) — docs/technical/02 Adapter D.

The maintenance safety net: when syndication refuses a post (NOT_EMBEDDABLE —
sensitive/age-gated/fresh) or changes shape outright, yt-dlp's continuously
patched extractor is the second hop. Imported as a LIBRARY — no subprocess,
no CLI-output parsing. Bundled as a standard runtime dependency so
`fallback = "auto"` never quietly has no second hop.

Resolve-only: the info dict feeds ResolvedTweet; OUR engine still downloads,
so naming, sidecars, state, and dedup behave identically for every adapter.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from xdl.models import (
    Author,
    ErrorCode,
    MediaAsset,
    ResolvedTweet,
    TweetRef,
    Variant,
    XdlError,
)

_VERSION_DATE_RE = re.compile(r"^(\d{4})\.(\d{1,2})\.(\d{1,2})")

#: Builds a YoutubeDL instance from options (injectable for hermetic tests).
YdlFactory = Callable[[dict[str, Any]], Any]


def stale_ytdlp_warning(
    *, max_age_days: int = 14, today: datetime | None = None
) -> str | None:
    """Warning line when the installed yt-dlp is older than ~14 days (X
    extractor fixes land continuously; `pip install -U yt-dlp` is the whole
    remedy — docs/technical/02 Adapter D). None = fresh or unparseable."""
    try:
        import yt_dlp
    except ImportError:
        return None
    m = _VERSION_DATE_RE.match(yt_dlp.version.__version__)
    if m is None:
        return None  # dev/hash build — cannot age-check
    released = datetime(int(m[1]), int(m[2]), int(m[3]), tzinfo=UTC)
    now = today or datetime.now(tz=UTC)
    age = (now - released).days
    if age > max_age_days:
        return (
            f"installed yt-dlp is {age} days old — X extractor fixes land "
            "continuously; pip install -U yt-dlp"
        )
    return None


class YtdlpResolver:
    name = "ytdlp"

    def __init__(self, *, ydl_factory: YdlFactory | None = None) -> None:
        self._factory = ydl_factory

    async def resolve(self, ref: TweetRef) -> ResolvedTweet:
        # yt-dlp's extractor is synchronous — keep the event loop free for
        # the other in-flight items (to_thread = a worker-thread hop).
        return await asyncio.to_thread(self._resolve_sync, ref)

    def _resolve_sync(self, ref: TweetRef) -> ResolvedTweet:
        factory = self._factory or _real_ydl
        opts: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "noprogress": True,
            "socket_timeout": 15,
        }
        try:
            with factory(opts) as ydl:
                info = ydl.extract_info(ref.canonical_url, download=False)
        except Exception as err:  # yt_dlp.utils.DownloadError and friends
            raise _map_error(err) from err
        if not info:
            raise XdlError(ErrorCode.NOT_FOUND, f"yt-dlp: no result for {ref.canonical_url}")
        if info.get("_type") == "playlist":
            entries = info.get("entries") or []
            if not entries:
                raise XdlError(
                    ErrorCode.NOT_FOUND, f"yt-dlp: empty playlist for {ref.canonical_url}"
                )
            info = entries[0]
        return _map_info(ref, info)


def _real_ydl(opts: dict[str, Any]) -> Any:
    import yt_dlp

    return yt_dlp.YoutubeDL(opts)


# --------------------------------------------------------------------------- #
# error mapping (docs/technical/02 Adapter D)
# --------------------------------------------------------------------------- #


def _map_error(err: Exception) -> XdlError:
    """404 -> NOT_FOUND (definitive), login-required -> AUTH_REQUIRED,
    sensitive/age-gated/protected -> NOT_EMBEDDABLE, network -> NETWORK,
    no-video -> NO_VIDEO (live-observed yt-dlp twitter message)."""
    msg = str(err)
    lower = msg.lower()
    if (
        "404" in msg
        or "not found" in lower
        or "does not exist" in lower
        or "been deleted" in lower
        or "unavailable" in lower
    ):
        return XdlError(ErrorCode.NOT_FOUND, f"yt-dlp: {msg}")
    if "no video" in lower:
        return XdlError(ErrorCode.NO_VIDEO, f"yt-dlp: {msg}")
    if (
        "login" in lower
        or "sign in" in lower
        or "sign-in" in lower
        or "authenticate" in lower
        or "http error 401" in lower
    ):
        return XdlError(ErrorCode.AUTH_REQUIRED, f"yt-dlp: {msg}")
    if (
        "sensitive" in lower
        or "age-gated" in lower
        or "age gated" in lower
        or "protected" in lower
        or "private" in lower
    ):
        return XdlError(ErrorCode.NOT_EMBEDDABLE, f"yt-dlp: {msg}")
    if (
        "timed out" in lower
        or "timeout" in lower
        or "connection" in lower
        or "network" in lower
        or "getaddrinfo" in lower
        or "temporary failure" in lower
    ):
        return XdlError(ErrorCode.NETWORK, f"yt-dlp: {msg}")
    return XdlError(ErrorCode.UPSTREAM_ERROR, f"yt-dlp: {msg}")


# --------------------------------------------------------------------------- #
# info-dict mapping
# --------------------------------------------------------------------------- #


def _map_info(ref: TweetRef, info: dict[str, Any]) -> ResolvedTweet:
    screen_name = str(
        info.get("uploader") or info.get("channel") or info.get("uploader_id") or "i"
    ).lstrip("@")

    variants, muted = _variants(info.get("formats") or [])
    if not variants:
        # Only the FINAL adapter's payload may report NO_VIDEO (round-2
        # semantics); yt-dlp is normally last in the chain.
        raise XdlError(ErrorCode.NO_VIDEO, "yt-dlp: post has no video formats")

    thumbnail = str(info.get("thumbnail") or "")
    asset = MediaAsset(
        kind="gif" if muted else "video",
        index=1,
        variants=variants,
        url_best=thumbnail,
        poster_url=thumbnail or None,
    )
    return ResolvedTweet(
        ref=ref,
        text=str(info.get("description") or info.get("title") or ""),
        created_at=_created_at(info),
        author=Author(screen_name=screen_name,
                       display_name=str(info.get("uploader") or "")),
        media=[asset],
        source="ytdlp",
    )


def _variants(formats: list[Any]) -> tuple[list[Variant], bool]:
    """Video formats -> Variants (m3u8 protocols tagged as HLS content type).

    Returns (variants, muted): muted = no format carried audio — X serves
    GIFs as silent looping MP4s, which is how we keep `kind == "gif"`.
    """
    out: list[Variant] = []
    has_audio = False
    for f in formats:
        if not isinstance(f, dict):
            continue
        if str(f.get("vcodec") or "none") == "none":
            continue  # audio-only / storyboard tracks
        url = f.get("url")
        if not url:
            continue
        is_m3u8 = str(f.get("protocol") or "").startswith("m3u8")
        tbr = f.get("tbr")
        content_type = (
            "application/x-mpegURL"
            if is_m3u8
            else f"video/{f.get('ext') or 'mp4'}"
        )
        out.append(
            Variant(
                url=str(url),
                bitrate=int(tbr * 1000) if isinstance(tbr, (int, float)) else None,
                content_type=content_type,
                width=f.get("width") if isinstance(f.get("width"), int) else None,
                height=f.get("height") if isinstance(f.get("height"), int) else None,
            )
        )
        if not is_m3u8 and str(f.get("acodec") or "none") != "none":
            has_audio = True
    return out, not has_audio


def _created_at(info: dict[str, Any]) -> datetime:
    ts = info.get("timestamp")
    if isinstance(ts, (int, float)) and not isinstance(ts, bool):
        return datetime.fromtimestamp(ts, tz=UTC)
    upload = info.get("upload_date")  # "YYYYMMDD"
    if isinstance(upload, str) and len(upload) == 8 and upload.isdigit():
        return datetime(int(upload[:4]), int(upload[4:6]), int(upload[6:8]), tzinfo=UTC)
    raise XdlError(
        ErrorCode.UPSTREAM_ERROR, "yt-dlp payload has no timestamp/upload_date"
    )
