"""Cross-stage data types (the contract every layer shares).

Shapes here follow docs/04-project-structure.md "Core models"; deviations are
recorded in DECISIONS.md.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from xdl.config import Settings

# --------------------------------------------------------------------------- #
# Errors (taxonomy per docs/technical/08-errors-and-rate-limits.md)
# --------------------------------------------------------------------------- #


class ErrorCode(StrEnum):
    URL_UNRECOGNIZED = "URL_UNRECOGNIZED"
    TWEET_ID_INVALID = "TWEET_ID_INVALID"
    NOT_EMBEDDABLE = "NOT_EMBEDDABLE"
    NOT_FOUND = "NOT_FOUND"
    LINK_EXPIRED = "LINK_EXPIRED"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    NO_VIDEO = "NO_VIDEO"
    RATE_LIMITED = "RATE_LIMITED"
    UPSTREAM_ERROR = "UPSTREAM_ERROR"
    NETWORK = "NETWORK"
    DRM_UNSUPPORTED = "DRM_UNSUPPORTED"
    HLS_REQUIRES_FFMPEG = "HLS_REQUIRES_FFMPEG"
    HLS_FAILED = "HLS_FAILED"
    INTEGRITY_MISMATCH = "INTEGRITY_MISMATCH"
    DISK_FULL = "DISK_FULL"
    WRITE_ERROR = "WRITE_ERROR"
    CANCELLED = "CANCELLED"
    # ffmpeg needed for a conversion the user asked for, and a failed
    # conversion run (docs/technical/08 table is additive here).
    FFMPEG_REQUIRED = "FFMPEG_REQUIRED"
    TRANSCODE_FAILED = "TRANSCODE_FAILED"


#: Codes whose XdlError is retryable per-item (docs/technical/08 retry table).
_RETRYABLE = frozenset(
    {
        ErrorCode.LINK_EXPIRED,  # once, via orchestrator re-resolve
        ErrorCode.RATE_LIMITED,
        ErrorCode.UPSTREAM_ERROR,
        ErrorCode.NETWORK,
        ErrorCode.INTEGRITY_MISMATCH,  # one clean restart
    }
)


class XdlError(Exception):
    """Typed pipeline error: one enum, a user-facing detail, retry semantics."""

    def __init__(
        self,
        code: ErrorCode,
        detail: str = "",
        *,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(f"{code.value}: {detail}" if detail else code.value)
        self.code = code
        self.detail = detail
        self.retry_after = retry_after

    @property
    def retryable(self) -> bool:
        return self.code in _RETRYABLE


class CancelToken:
    """Cooperative Ctrl+C signal shared by CLI -> orchestrator -> engine.

    Plain (non-pydantic) mutable flag: the CLI's SIGINT handler sets it from
    the main thread between bytecode steps; async stages poll it at chunk /
    item granularity. First press = graceful drain, second = hard exit (the
    `.part` invariant makes the hard exit safe — docs/technical/03).
    """

    __slots__ = ("cancelled",)

    def __init__(self) -> None:
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True


# --------------------------------------------------------------------------- #
# Stage 1 — normalize
# --------------------------------------------------------------------------- #


class TweetRef(BaseModel):
    tweet_id: str  # numeric string, <=19 digits
    screen_name: str | None = None  # from URL if present; not authoritative
    canonical_url: str


# --------------------------------------------------------------------------- #
# Stage 2 — resolve
# --------------------------------------------------------------------------- #


class Author(BaseModel):
    screen_name: str
    display_name: str = ""
    verified: bool = False


class Variant(BaseModel):
    url: str
    bitrate: int | None = None  # bps; None for HLS/photos
    content_type: str = "video/mp4"
    width: int | None = None
    height: int | None = None


class MediaAsset(BaseModel):
    kind: Literal["video", "gif", "photo"]
    index: int  # 1-based position in the post
    variants: list[Variant] = []
    url_best: str = ""  # photo URL / video poster (media_url_https)
    poster_url: str | None = None


class ResolvedTweet(BaseModel):
    ref: TweetRef
    text: str = ""
    created_at: datetime
    author: Author
    media: list[MediaAsset]
    source: Literal["syndication", "fxtwitter", "graphql", "ytdlp"]


# --------------------------------------------------------------------------- #
# Stage 3 — select
# --------------------------------------------------------------------------- #


class QualityPolicy(BaseModel):
    mode: Literal["best", "worst", "max_height"] = "best"
    max_height: int | None = None  # only meaningful for mode == "max_height"

    @classmethod
    def parse(cls, raw: str | None) -> QualityPolicy:
        """'best' | 'worst' | a height number like '720'."""
        if raw is None or raw == "" or raw == "best":
            return cls(mode="best")
        if raw == "worst":
            return cls(mode="worst")
        if raw.isdigit():
            return cls(mode="max_height", max_height=int(raw))
        raise XdlError(ErrorCode.URL_UNRECOGNIZED, f"invalid --quality value: {raw!r}")


class Chosen(BaseModel):
    """One selected variant for one media asset (a download candidate)."""

    asset: MediaAsset
    variant: Variant


# --------------------------------------------------------------------------- #
# Results
# --------------------------------------------------------------------------- #


class MediaOutcome(BaseModel):
    index: int  # media position in the post
    status: Literal["ok", "skipped", "failed"]
    file: Path | None = None
    error: XdlError | None = None  # per-media failure reason

    model_config = ConfigDict(arbitrary_types_allowed=True)


class ItemResult(BaseModel):
    ref: TweetRef
    status: Literal["ok", "skipped", "partial", "failed"]
    media: list[MediaOutcome] = []  # one per downloadable media asset
    error: XdlError | None = None  # headline error for display (first failure)
    files: list[Path] = []  # convenience: the ok media files
    detail: str = ""  # human-readable, shown in summary

    model_config = ConfigDict(arbitrary_types_allowed=True)


class BatchReport(BaseModel):
    results: list[ItemResult] = []
    exit_code: int = 0  # 0/1/2 folding per docs/technical/07
    cancelled: bool = False  # Ctrl+C mid-run: CLI exits 130 instead (docs/tech/08)

    model_config = ConfigDict(arbitrary_types_allowed=True)

    @property
    def ok(self) -> int:
        return sum(r.status == "ok" for r in self.results)

    @property
    def skipped(self) -> int:
        return sum(r.status == "skipped" for r in self.results)

    @property
    def partial(self) -> int:
        return sum(r.status == "partial" for r in self.results)

    @property
    def failed(self) -> int:
        return sum(r.status == "failed" for r in self.results)


# --------------------------------------------------------------------------- #
# Batch request / storage rows
# --------------------------------------------------------------------------- #


class BatchRequest(BaseModel):
    """One batch invocation: raw inputs plus the resolved Settings.

    Raw inputs are unnormalized strings (URLs, t.co links, bare ids with --id);
    normalization and dedup happen inside run_batch.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    inputs: list[str]
    settings: Settings
    origin: Literal["cli", "web", "retry"] = "cli"
    id_mode: bool = False  # treat bare numeric arguments as tweet ids
    force: bool = False
    no_meta: bool = False
    quality: str | None = None  # 'best' | 'worst' | height number
    dry_run: bool = False  # resolve + print planned paths, download nothing
    audio_only: bool = False  # M5: extract MP3 instead of keeping the video
    gif: bool = False  # M5: convert X's mp4-as-gif to a real GIF


class TweetRow(BaseModel):
    tweet_id: str
    author: str | None
    media_count: int
    first_seen: str


class DownloadRow(BaseModel):
    tweet_id: str
    media_index: int
    batch_id: int | None = None
    variant_url: str | None = None
    file_path: Path
    size_bytes: int | None = None
    status: Literal["ok", "incomplete", "failed"] = "incomplete"
    error: str | None = None
    source: str | None = None
    downloaded_at: str = ""
    updated_at: str = ""
