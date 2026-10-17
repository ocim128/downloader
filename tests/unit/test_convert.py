"""media/convert: MP3 + GIF argv builders and the ffmpeg runner.

Hermetic: real subprocesses, but "ffmpeg" is a tiny python sim (same trick as
tests/unit/test_hls.py) — no live media, no real ffmpeg.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from xdl.media import convert
from xdl.models import ErrorCode, XdlError

SRC = Path("in.mp4")
PART = Path("out.part")

# sim-ffmpeg: writes a fake artifact, honors -progress frames, optional
# failure / hang modes selected via the injected interpreter code.
SIM_OK = (
    "import sys, pathlib\n"
    "dest = pathlib.Path(sys.argv[1])\n"
    "dest.write_bytes(b'converted-artifact')\n"
    "sys.stdout.write('total_size=100\\n\\nprogress=end\\n\\n')\n"
    "sys.stdout.flush()\n"
)
SIM_FAIL = (
    "import sys\n"
    "sys.stderr.write('boom: invalid data\\n')\n"
    "sys.exit(3)\n"
)
SIM_HANG = "import time; time.sleep(30)\n"


def _mp3(monkeypatch: pytest.MonkeyPatch, code: str) -> list[str]:
    def fake(src: Path, dest_part: Path, ffmpeg: str = "ffmpeg", *,
             progress: bool = False) -> list[str]:
        return [sys.executable, "-c", code, str(dest_part)]

    monkeypatch.setattr(convert, "mp3_argv", fake)
    return fake(SRC, PART)


def test_mp3_argv_exact() -> None:
    argv = convert.mp3_argv(SRC, PART, "ffmpeg.exe")
    assert argv == [
        "ffmpeg.exe", "-hide_banner", "-nostdin", "-loglevel", "error", "-stats",
        "-i", "in.mp4",
        "-vn", "-c:a", "libmp3lame", "-q:a", "0",
        "-y", "out.part",
    ]
    assert "-q:a" in argv and argv[argv.index("-q:a") + 1] == "0"  # V0


def test_mp3_argv_progress_swaps_stats() -> None:
    argv = convert.mp3_argv(SRC, PART, "ffmpeg", progress=True)
    assert "-progress" in argv and "pipe:1" in argv and "-nostats" in argv
    assert "-stats" not in argv


def test_gif_argv_two_pass_palette() -> None:
    argv = convert.gif_argv(SRC, PART, "ffmpeg")
    vf = argv[argv.index("-vf") + 1]
    assert vf == "fps=15,split[s0][s1];[s0]palettegen[p];[s1][p]paletteuse"


async def test_run_ffmpeg_success_writes_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    part = tmp_path / "audio.mp3.part"

    def fake(src: Path, dest_part: Path, ffmpeg: str = "ffmpeg", *,
             progress: bool = False) -> list[str]:
        return [sys.executable, "-c", SIM_OK, str(dest_part)]

    monkeypatch.setattr(convert, "mp3_argv", fake)
    frames: list[dict[str, str]] = []
    await convert.run_ffmpeg(convert.mp3_argv(SRC, part, "x"), timeout_min=1,
                             frame_cb=frames.append)
    assert part.read_bytes() == b"converted-artifact"
    assert frames and frames[-1].get("progress") == "end"


async def test_run_ffmpeg_failure_tail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake(src: Path, dest_part: Path, ffmpeg: str = "ffmpeg", *,
             progress: bool = False) -> list[str]:
        return [sys.executable, "-c", SIM_FAIL, str(dest_part)]

    monkeypatch.setattr(convert, "gif_argv", fake)
    with pytest.raises(XdlError) as excinfo:
        await convert.run_ffmpeg(convert.gif_argv(SRC, tmp_path / "g.part", "x"),
                                 timeout_min=1)
    err = excinfo.value
    assert err.code is ErrorCode.TRANSCODE_FAILED
    assert "boom: invalid data" in err.detail
    assert "ffmpeg exited 3" in err.detail


async def test_run_ffmpeg_timeout_kills(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake(src: Path, dest_part: Path, ffmpeg: str = "ffmpeg", *,
             progress: bool = False) -> list[str]:
        return [sys.executable, "-c", SIM_HANG, str(dest_part)]

    monkeypatch.setattr(convert, "mp3_argv", fake)
    with pytest.raises(XdlError) as excinfo:
        await convert.run_ffmpeg(convert.mp3_argv(SRC, tmp_path / "a.part", "x"),
                                 timeout_min=0)
    assert excinfo.value.code is ErrorCode.TRANSCODE_FAILED
    assert "timed out" in excinfo.value.detail
