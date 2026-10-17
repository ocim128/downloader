"""Adapter B — api.fxtwitter.com (opt-in fallback, third-party).

Never the default: enabling it sends the URL list to that service's operator.
Enabled via `--fallback fxtwitter` or config.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx

from xdl.models import (
    Author,
    ErrorCode,
    MediaAsset,
    ResolvedTweet,
    TweetRef,
    Variant,
    XdlError,
)

FXTWITTER_URL = "https://api.fxtwitter.com/status/{tweet_id}"


class FxtwitterResolver:
    name = "fxtwitter"

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def resolve(self, ref: TweetRef) -> ResolvedTweet:
        url = FXTWITTER_URL.format(tweet_id=ref.tweet_id)
        try:
            r = await self._client.get(url, headers={"accept": "application/json"})
        except httpx.HTTPError as err:
            raise XdlError(ErrorCode.NETWORK, f"connection failed: {err}") from err

        if r.status_code == 404:
            raise XdlError(ErrorCode.NOT_FOUND, f"fxtwitter 404 for id {ref.tweet_id}")
        if r.status_code == 429:
            raise XdlError(ErrorCode.RATE_LIMITED, "fxtwitter rate limited")
        if r.status_code >= 500:
            raise XdlError(ErrorCode.UPSTREAM_ERROR, f"fxtwitter HTTP {r.status_code}")
        if r.status_code != 200:
            raise XdlError(ErrorCode.UPSTREAM_ERROR, f"fxtwitter HTTP {r.status_code}")

        try:
            payload = r.json()
        except ValueError as err:
            raise XdlError(ErrorCode.UPSTREAM_ERROR, "fxtwitter returned invalid JSON") from err

        tweet = payload.get("tweet") if isinstance(payload, dict) else None
        if not isinstance(tweet, dict):
            raise XdlError(ErrorCode.NOT_FOUND, "fxtwitter payload has no tweet object")

        return _map(ref, tweet)


def _map(ref: TweetRef, tweet: dict[str, object]) -> ResolvedTweet:
    author_raw = tweet.get("author")
    author_dict = author_raw if isinstance(author_raw, dict) else {}
    author = Author(
        screen_name=str(author_dict.get("screen_name", "")),
        display_name=str(author_dict.get("name", "")),
    )

    media: list[MediaAsset] = []
    media_raw = tweet.get("media")
    if isinstance(media_raw, dict):
        photos = media_raw.get("photos")
        if isinstance(photos, list):
            for p in photos:
                photo = p if isinstance(p, dict) else {}
                url = str(photo.get("url", ""))
                media.append(
                    MediaAsset(kind="photo", index=len(media) + 1, url_best=url, poster_url=url)
                )
        videos = media_raw.get("videos")
        if isinstance(videos, list):
            for v in videos:
                video = v if isinstance(v, dict) else {}
                kind = "gif" if str(video.get("type")) == "gif" else "video"
                variant = Variant(
                    url=str(video.get("url", "")),
                    bitrate=None,  # single progressive MP4, no bitrate ladder
                    content_type="video/mp4",
                    width=_opt_int(video.get("width")),
                    height=_opt_int(video.get("height")),
                )
                thumb = str(video.get("thumbnail") or "") or None
                media.append(
                    MediaAsset(
                        kind=kind,
                        index=len(media) + 1,
                        variants=[variant] if variant.url else [],
                        url_best=thumb or "",
                        poster_url=thumb,
                    )
                )

    raw_created = str(tweet.get("created_at") or "")
    raw_ts = tweet.get("created_timestamp")
    if raw_created:
        created = _parse_created(raw_created)
    elif isinstance(raw_ts, str) and raw_ts.isdigit():
        created = datetime.fromtimestamp(int(raw_ts), tz=UTC)
    else:
        raise XdlError(ErrorCode.UPSTREAM_ERROR, "no created_at/created_timestamp in payload")

    return ResolvedTweet(
        ref=ref,
        text=str(tweet.get("text", "")),
        created_at=created,
        author=author,
        media=media,
        source="fxtwitter",
    )


def _parse_created(raw: str) -> datetime:
    if raw.isdigit():  # created_timestamp (unix seconds)
        return datetime.fromtimestamp(int(raw), tz=UTC)
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as err:
        raise XdlError(ErrorCode.UPSTREAM_ERROR, f"unparseable created_at: {raw!r}") from err


def _opt_int(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None
