"""Single-file streaming download: .part, Range resume, verify, atomic rename.

docs/technical/03. The engine never imports a resolver; a media-URL 403 raises
`LinkExpiredError` and the ORCHESTRATOR recovers (layering rule 6).
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from pathlib import Path

import httpx

from xdl.engine.throttle import ThrottleState, parse_retry_after
from xdl.models import DownloadJob, ErrorCode, LinkExpiredError, XdlError
from xdl.urls import is_allowed_media_url

#: cb(delta_bytes, total_bytes_or_None)
ProgressFn = Callable[[int, int | None], None]

_CHUNK = 64 * 1024


async def fetch(
    job: DownloadJob,
    client: httpx.AsyncClient,
    *,
    cb: ProgressFn | None = None,
    throttle: ThrottleState | None = None,
) -> Path:
    """Stream one file to `job.dest` via a `.part` temp; returns final path.

    - Range-resume from an existing `.part` (206 -> total from Content-Range;
      200 to a ranged request -> restart from zero, never append a mismatch).
    - Unknown length tolerated (no false INTEGRITY_MISMATCH).
    - Known length verified exactly; mismatch deletes the `.part` and fails.
    - 403 on the media URL -> LinkExpiredError (orchestrator re-resolves once).
    """
    if not is_allowed_media_url(job.url):
        raise XdlError(ErrorCode.URL_UNRECOGNIZED, f"media URL host not allowed: {job.url}")

    dest = job.dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    offset = part.stat().st_size if part.exists() else 0

    if throttle is not None:
        await throttle.gate()

    headers = {"Range": f"bytes={offset}-"} if offset else {}
    try:
        async with client.stream("GET", job.url, headers=headers) as r:
            if r.status_code == 429:
                if throttle is not None:
                    throttle.report_429(r.headers.get("retry-after"))
                raise XdlError(
                    ErrorCode.RATE_LIMITED,
                    "download rate limited",
                    retry_after=_retry_after(r.headers.get("retry-after")),
                )
            if r.status_code == 403:
                raise LinkExpiredError("signed CDN URL expired mid-transfer")
            if offset and r.status_code == 200:
                offset = 0  # server ignored Range: restart clean, never append
            if r.status_code == 206:
                total = _total_from_content_range(r.headers.get("content-range"))
            else:
                if r.status_code >= 400:
                    raise _status_error(r.status_code)
                total = _total_from_length(r.headers.get("content-length"))

            with part.open("ab" if offset else "wb") as f:
                async for chunk in r.aiter_bytes(_CHUNK):
                    f.write(chunk)
                    if cb is not None:
                        cb(len(chunk), total)
                # fsync the write handle while we hold it (Windows _commit
                # fails with EBADF on read-only handles).
                await asyncio.to_thread(os.fsync, f.fileno())
    except httpx.TimeoutException as err:
        raise XdlError(ErrorCode.NETWORK, f"timeout: {err}") from err
    except httpx.TransportError as err:
        raise XdlError(ErrorCode.NETWORK, f"connection failed: {err}") from err

    size = part.stat().st_size
    if total is not None and size != total:
        part.unlink(missing_ok=True)
        raise XdlError(
            ErrorCode.INTEGRITY_MISMATCH,
            f"expected {total} bytes, got {size}",
        )
    # Indeterminate end (unknown length): stream ended cleanly + rename is the
    # integrity bar — never a mismatch verdict, never destroy the `.part`.

    await asyncio.to_thread(part.replace, dest)  # atomic on NTFS
    return dest


def _total_from_content_range(raw: str | None) -> int | None:
    """`Content-Range: bytes s-e/TOTAL` — the authoritative total on a 206
    (whose Content-Length is only the REMAINING bytes)."""
    if not raw or "/" not in raw:
        return None
    tail = raw.rsplit("/", 1)[1].strip()
    return int(tail) if tail.isdigit() else None


def _total_from_length(raw: str | None) -> int | None:
    if raw is None or not raw.isdigit():
        return None  # chunked/missing -> unknown length
    return int(raw)


def _status_error(status: int) -> XdlError:
    if status in (500, 502, 503, 504):
        return XdlError(ErrorCode.UPSTREAM_ERROR, f"HTTP {status} on media URL")
    if status == 404:
        return XdlError(ErrorCode.NOT_FOUND, "media URL returned 404")
    if status in (401,):
        return XdlError(ErrorCode.AUTH_REQUIRED, f"HTTP {status} on media URL")
    return XdlError(ErrorCode.UPSTREAM_ERROR, f"HTTP {status} on media URL")


def _retry_after(raw: str | None) -> float | None:
    return parse_retry_after(raw)
