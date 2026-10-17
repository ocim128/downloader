"""ffmpeg conversions: `--audio-only` MP3 and `--gif` real GIF
(docs/technical/04 "ffmpeg integration details").

Same subprocess discipline as the HLS remux in `media/hls.py`: `-nostdin`,
DEVNULL stdin, stderr tail captured for the failure detail, `.part` +
atomic-rename invariant owned by the caller.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from pathlib import Path

from xdl.models import ErrorCode, XdlError

#: frame_cb gets one dict per ffmpeg -progress frame (total_size, ...).
FrameFn = Callable[[dict[str, str]], None]

_FRAME_LINE_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")


def mp3_argv(
    src: Path, dest_part: Path, ffmpeg: str = "ffmpeg", *, progress: bool = False
) -> list[str]:
    """`-vn -c:a libmp3lame -q:a 0` — V0, the best-possible VBR transcode of
    the source. Honest quality note (docs/technical/04): X serves lossy AAC;
    this is a best-possible transcode, not a lossless extraction. `-f mp3`
    explicit: the `.part` suffix defeats extension inference."""
    stats = ("-progress", "pipe:1", "-nostats") if progress else ("-stats",)
    return [
        ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error",
        *stats,
        "-i", str(src),
        "-vn", "-c:a", "libmp3lame", "-q:a", "0",
        "-f", "mp3",
        "-y", str(dest_part),
    ]


def gif_argv(
    src: Path, dest_part: Path, ffmpeg: str = "ffmpeg", *, progress: bool = False
) -> list[str]:
    """Two-pass palette in one filter graph (docs/technical/04): 15 fps,
    source-specific palettegen/paletteuse. Output is large — opt-in by
    design. `-f gif` explicit (see mp3_argv)."""
    stats = ("-progress", "pipe:1", "-nostats") if progress else ("-stats",)
    return [
        ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error",
        *stats,
        "-i", str(src),
        "-vf", "fps=15,split[s0][s1];[s0]palettegen[p];[s1][p]paletteuse",
        "-f", "gif",
        "-y", str(dest_part),
    ]


async def run_ffmpeg(
    argv: list[str],
    *,
    timeout_min: int = 30,
    frame_cb: FrameFn | None = None,
) -> None:
    """Run one conversion subprocess. rc != 0 -> TRANSCODE_FAILED with the
    stderr tail; timeout kills the process; -progress frames feed frame_cb."""
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE if frame_cb is not None
        else asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    tail = bytearray()
    assert proc.stderr is not None
    err_task = asyncio.create_task(_drain(proc.stderr, tail))
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
            ErrorCode.TRANSCODE_FAILED, f"ffmpeg timed out after {timeout_min} min"
        ) from None
    except asyncio.CancelledError:
        proc.kill()
        await asyncio.gather(
            proc.wait(), err_task, *(out_task or [None]), return_exceptions=True
        )
        raise

    if rc != 0:
        tail_text = tail.decode("utf-8", "replace").strip()
        if "does not contain any stream" in tail_text:
            # e.g. --audio-only on an audio-less video: nothing to encode.
            raise XdlError(
                ErrorCode.TRANSCODE_FAILED,
                "source has no encodable stream (audio-less video with "
                "--audio-only?)",
            )
        raise XdlError(
            ErrorCode.TRANSCODE_FAILED,
            f"ffmpeg exited {rc}: {tail_text}",
        )


async def _drain(stream: asyncio.StreamReader, sink: bytearray, limit: int = 2000) -> None:
    """Capture stderr for the TRANSCODE_FAILED detail line (tail-capped)."""
    while chunk := await stream.read(4096):
        sink.extend(chunk)
    del sink[:-limit]


async def _pump_frames(
    stream: asyncio.StreamReader, frame_cb: FrameFn
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
