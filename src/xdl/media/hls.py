"""HLS (m3u8) handling: master-playlist parse, rendition choose, DRM gate,
ffmpeg remux — docs/technical/04-hls-and-ffmpeg.md.

Pure no-network module: manifest text comes in via the args (the caller owns the
fetch), ffmpeg does the actual segment fetching inside the remux subprocess.
The orchestrator (app.py, Agent A) injects its http client as `fetch_text`;
tests inject a stub, so pytest never sees a real request.
"""

from __future__ import annotations

import asyncio
import re
import shutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin

from xdl.models import ErrorCode, QualityPolicy, XdlError

FFMPEG_UA = "curl/8.0.1"  # pre-verified 2026-10-17 for video.twimg.com GETs

FFMPEG_INSTALL_HINT = "install: winget install ffmpeg / choco install ffmpeg"

#: #EXT-X-KEY methods we accept transparently (docs: plain or AES-128 only).
_OK_KEY_METHODS = frozenset({"none", "aes-128"})


@dataclass(frozen=True)
class Rendition:
    """One #EXT-X-STREAM-INF entry of a master playlist."""

    url: str
    bandwidth: int | None = None
    width: int | None = None
    height: int | None = None
    codecs: str = ""


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #

_ATTR_CHUNK_RE = re.compile(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)')


def _attrs(line: str) -> dict[str, str]:
    """KEY=VALUE list after the `:` of an HLS tag (quoted values keep commas)."""
    at = line.find(":")
    if at < 0:
        return {}
    out: dict[str, str] = {}
    for key, value in _ATTR_CHUNK_RE.findall(line[at + 1 :]):
        out[key] = value[1:-1] if value.startswith('"') and value.endswith('"') else value
    return out


def parse_master(text: str, base_url: str) -> list[Rendition]:
    """Parse #EXT-X-STREAM-INF entries, resolving child URLs against `base_url`.

    I-frame playlists are skipped (they are not a full-video rendition). The
    child URI may come from a URI="..." attribute or the next non-comment line.
    """
    renditions: list[Rendition] = []
    pending: dict[str, str] | None = None
    for line in text.splitlines():
        line = line.strip()
        if not line or not line.startswith("#"):
            if pending is not None:
                renditions.append(_rendition(pending, line, base_url))
                pending = None
            continue
        if line.startswith("#EXT-X-STREAM-INF:"):
            pending = _attrs(line)
        elif line.startswith("#EXT-X-I-FRAME-STREAM-INF"):
            pending = None  # iframe sidecar never consumes a URI line
    if pending is not None:  # URI="..." present but nothing follows the line
        renditions.append(_rendition(pending, pending.get("URI") or "", base_url))
    return renditions


def _rendition(attrs: dict[str, str], uri: str, base_url: str) -> Rendition:
    bandwidth = int(attrs["BANDWIDTH"]) if attrs.get("BANDWIDTH", "").isdigit() else None
    width = height = None
    w, _, h = (attrs.get("RESOLUTION") or "").lower().partition("x")
    if w.isdigit() and h.isdigit():
        width, height = int(w), int(h)
    return Rendition(
        url=urljoin(base_url, attrs.get("URI") or uri),
        bandwidth=bandwidth,
        width=width,
        height=height,
        codecs=attrs.get("CODECS", ""),
    )


def choose_rendition(
    renditions: list[Rendition], policy: QualityPolicy | None = None
) -> Rendition:
    """Pick the rendition per policy (default `best`).

    BANDWIDTH is the only comparable a master publishes; RESOLUTION caps
    (`max_height`) filter first, then the highest BANDWIDTH under the cap wins.
    Nothing under the cap -> closest fit (smallest height above it).
    """
    if not renditions:
        raise XdlError(ErrorCode.HLS_FAILED, "master playlist has no renditions")
    policy = policy or QualityPolicy()
    if policy.mode == "worst":
        return min(renditions, key=lambda r: r.bandwidth or 0)
    if policy.mode == "max_height" and policy.max_height is not None:
        capped = [r for r in renditions if r.height is not None and r.height <= policy.max_height]
        if capped:
            return max(capped, key=lambda r: r.bandwidth or 0)
        above = [r for r in renditions if r.height is not None]
        return min(above or renditions, key=lambda r: (r.height or 10**9, -(r.bandwidth or 0)))
    return max(renditions, key=lambda r: r.bandwidth or 0)


def detect_drm(text: str) -> None:
    """Raise DRM_UNSUPPORTED for an EXT-X-KEY method we cannot handle
    (anything beyond plain and AES-128, e.g. SAMPLE-AES/FairPlay, Widevine)."""
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line.upper().startswith("#EXT-X-KEY"):
            continue
        method = _attrs(line).get("METHOD", "NONE").strip()
        if method.lower() not in _OK_KEY_METHODS:
            raise XdlError(
                ErrorCode.DRM_UNSUPPORTED,
                f"playlist uses {method} key method (only AES-128 supported)",
            )


# --------------------------------------------------------------------------- #
# ffmpeg
# --------------------------------------------------------------------------- #

def ffmpeg_argv(
    child_url: str,
    dest_part: Path,
    user_agent: str = FFMPEG_UA,
    ffmpeg_path: str = "ffmpeg",
    *,
    progress: bool = False,
) -> list[str]:
    """Exact argv of the lossless remux (docs/technical/04 preferred path).

    progress=True pipes `-progress pipe:1` key=value frames instead of `-stats`.
    `-f mp4` is explicit because the `.part` suffix defeats ffmpeg's
    extension-based output-format inference.
    """
    stats = ("-progress", "pipe:1", "-nostats") if progress else ("-stats",)
    return [
        ffmpeg_path,
        "-hide_banner", "-nostdin", "-loglevel", "error",
        *stats,
        "-user_agent", user_agent,
        "-allowed_extensions", "ALL",
        "-protocol_whitelist", "file,https,tls,tcp",
        "-i", child_url,
        "-map", "0:v:0", "-map", "0:a?",
        "-c", "copy",
        "-movflags", "+faststart",
        "-f", "mp4",
        "-y", str(dest_part),
    ]


_FRAME_LINE_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")


async def _pump_frames(
    stream: asyncio.StreamReader, frame_cb: Callable[[dict[str, str]], None]
) -> None:
    """Incrementally feed frame_cb while ffmpeg runs (flush on blank lines)."""
    current: dict[str, str] = {}
    while raw := await stream.readline():
        line = raw.decode("utf-8", "replace").strip()
        if not line:
            if current:
                frame_cb(dict(current))
                current = {}
        elif m := _FRAME_LINE_RE.match(line):
            current[m.group(1)] = m.group(2)
    if current:
        frame_cb(dict(current))


async def _drain_into(
    stream: asyncio.StreamReader, sink: bytearray, limit: int = 2000
) -> None:
    """Capture stderr for the HLS_FAILED detail line (tail-capped)."""
    while chunk := await stream.read(4096):
        sink.extend(chunk)
    del sink[:-limit]


def require_ffmpeg(ffmpeg: str | None = None) -> str:
    """Resolved ffmpeg path or `HLS_REQUIRES_FFMPEG` with the install hint."""
    path = ffmpeg if ffmpeg else shutil.which("ffmpeg") or ""
    if not path:
        raise XdlError(ErrorCode.HLS_REQUIRES_FFMPEG, FFMPEG_INSTALL_HINT)
    return path


async def download_hls(
    master_url: str,
    dest: Path,
    *,
    policy: QualityPolicy | None = None,
    user_agent: str = FFMPEG_UA,
    ffmpeg: str | None = None,
    timeout_min: int = 30,
    fetch_text: Callable[[str], Awaitable[str]],
    frame_cb: Callable[[dict[str, str]], None] | None = None,
    master_text: str | None = None,
) -> Path:
    """Parse master -> choose rendition -> DRM gate -> ffmpeg remux -> rename.

    `master_text` lets a caller that already fetched the master (the
    orchestrator's upgrade probe) hand it in instead of paying a second
    round-trip for the same bytes.

    Same atomic-path invariant as the engine: ffmpeg writes `dest + ".part"`,
    the final rename happens only on a clean exit code.
    """
    ffmpeg_path = require_ffmpeg(ffmpeg)

    master = master_text if master_text is not None else await fetch_text(master_url)
    rendition = choose_rendition(parse_master(master, master_url), policy)
    child = await fetch_text(rendition.url)
    detect_drm(child)

    part = dest.with_name(dest.name + ".part")
    part.parent.mkdir(parents=True, exist_ok=True)
    argv = ffmpeg_argv(
        rendition.url, part, user_agent, ffmpeg_path, progress=frame_cb is not None
    )

    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE if frame_cb is not None else asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    tail = bytearray()
    assert proc.stderr is not None
    err_task = asyncio.create_task(_drain_into(proc.stderr, tail))
    out_task: asyncio.Task[None] | None = None
    if frame_cb is not None:
        assert proc.stdout is not None
        out_task = asyncio.create_task(_pump_frames(proc.stdout, frame_cb))

    try:
        rc = await asyncio.wait_for(proc.wait(), timeout=timeout_min * 60)
        tasks = [t for t in (err_task, out_task) if t is not None]
        await asyncio.gather(*tasks)
    except TimeoutError:
        proc.kill()
        await asyncio.gather(proc.wait(), err_task, return_exceptions=True)
        raise XdlError(
            ErrorCode.HLS_FAILED, f"ffmpeg timed out after {timeout_min} min"
        ) from None
    except asyncio.CancelledError:
        proc.kill()
        await asyncio.gather(
            proc.wait(), err_task, *(out_task or [None]), return_exceptions=True
        )
        raise

    if rc != 0:
        raise XdlError(
            ErrorCode.HLS_FAILED,
            f"ffmpeg exited {rc}: {tail.decode('utf-8', 'replace').strip()}",
        )

    await asyncio.to_thread(part.replace, dest)
    return dest
