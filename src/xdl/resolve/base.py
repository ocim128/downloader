"""Resolver protocol + FallbackChain + error mapping (docs/technical/02).

Chain semantics (audit round 2): a syndication 404/tombstone/empty response is
`NOT_EMBEDDABLE` — it does NOT mean the post is gone — and always falls through
to the next configured adapter. A post is reported NOT_FOUND only after every
adapter has failed.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

import httpx

from xdl.config import Settings
from xdl.models import ErrorCode, ResolvedTweet, TweetRef, XdlError


@runtime_checkable
class Resolver(Protocol):
    """TweetRef -> ResolvedTweet via some remote source."""

    name: str

    async def resolve(self, ref: TweetRef) -> ResolvedTweet: ...


class FallbackChain:
    """Walks adapters in order; NOT_EMBEDDABLE (and other adapter failures)
    fall through. Terminal error: NOT_FOUND wins (definitive), then
    RATE_LIMITED (retryable signal), else the last adapter's error."""

    def __init__(self, adapters: list[Resolver]) -> None:
        if not adapters:
            raise ValueError("FallbackChain needs at least one adapter")
        self._adapters = adapters

    @property
    def names(self) -> list[str]:
        return [a.name for a in self._adapters]

    async def resolve(self, ref: TweetRef) -> ResolvedTweet:
        errors: list[XdlError] = []
        for adapter in self._adapters:
            try:
                return await adapter.resolve(ref)
            except XdlError as err:
                errors.append(err)
                continue
        raise _terminal_error(errors, self.names)


def _terminal_error(errors: list[XdlError], names: list[str]) -> XdlError:
    trail = " -> ".join(names)
    for err in reversed(errors):
        if err.code is ErrorCode.NOT_FOUND:
            return XdlError(err.code, f"{err.detail} (tried: {trail})")
    for err in errors:
        if err.code is ErrorCode.RATE_LIMITED:
            return XdlError(err.code, f"{err.detail} (tried: {trail})")
    last = (
        errors[-1]
        if errors
        else XdlError(ErrorCode.UPSTREAM_ERROR, "no adapter produced a result")
    )
    return XdlError(last.code, f"{last.detail} (tried: {trail})")


def build_chain(settings: Settings, client: httpx.AsyncClient) -> FallbackChain:
    """Wire the adapter chain per resolver config (docs/technical/02).

    Order: syndication fast-path -> yt-dlp (Adapter D) whenever importable
    under `fallback = "auto"` (the default — NOT_EMBEDDABLE posts get the
    safety-net second hop) -> fxtwitter when explicitly configured. Chain
    composition per fallback setting:

    - `auto`      [syndication, ytdlp]  (ytdlp silently skipped if the bundled
                  dep is somehow missing)
    - `ytdlp`     [syndication, ytdlp]  (missing dep fails fast — explicit ask)
    - `fxtwitter` [syndication, fxtwitter]
    - `none`      [syndication]
    """
    from xdl.resolve.syndication import SyndicationResolver

    adapters: list[Resolver] = [SyndicationResolver(client)]
    fallback = settings.resolver.fallback
    if fallback in ("auto", "ytdlp"):
        try:
            import yt_dlp  # noqa: F401  (importability is the "auto" gate)

            from xdl.resolve.ytdlp import YtdlpResolver

            adapters.append(YtdlpResolver(mode=settings.resolver.ytdlp_mode))
        except ImportError:
            if fallback == "ytdlp":
                raise XdlError(
                    ErrorCode.UPSTREAM_ERROR,
                    "yt-dlp is not installed — pip install -U yt-dlp",
                ) from None
    if fallback == "fxtwitter":
        from xdl.resolve.fxtwitter import FxtwitterResolver

        adapters.append(FxtwitterResolver(client))
    return FallbackChain(adapters)
