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
    """Wire the adapter chain per resolver config.

    `fallback = "auto"` currently means syndication fast-path; the yt-dlp
    second hop (Adapter D) lands with M4 and slots in here automatically
    whenever it is importable.
    """
    from xdl.resolve.fxtwitter import FxtwitterResolver
    from xdl.resolve.syndication import SyndicationResolver

    adapters: list[Resolver] = [SyndicationResolver(client)]
    if settings.resolver.fallback == "fxtwitter":
        adapters.append(FxtwitterResolver(client))
    return FallbackChain(adapters)
