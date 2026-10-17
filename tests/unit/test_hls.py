"""HLS module tests (docs/technical/04): offline parsing + argv asserts.

Hermetic: no network (manifests come from tests/fixtures/hls/*.m3u8), no live
ffmpeg (the subprocess is a tiny Python "sim-ffmpeg" shim), no live media.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from xdl.media import hls
from xdl.models import ErrorCode, QualityPolicy, XdlError

FIX = Path(__file__).resolve().parent.parent / "fixtures" / "hls"
BASE = "https://video.twimg.com/amplify_video/2058755555980681488/pl/master.m3u8"
SIM_FAIL = 'import sys; sys.stderr.write("boom: invalid data\\n"); sys.exit(12)'
SIM_TIMEOUT = "import time; time.sleep(30); sys.exit(0)"
SIM = '''import sys, time
out, mode = sys.argv[1], sys.argv[2]
if mode == "fail":
    sys.stderr.write("boom: invalid data\\n")
    sys.exit(12)
if mode == "timeout":
    time.sleep(30)
    sys.exit(0)
if mode == "progress":
    for _ in range(2):
        print("frame=1", flush=True)
        print("out_time_ms=1000", flush=True)
        print(flush=True)
with open(out, "wb") as f:
    f.write(b"remuxed\\0bytes")
'''


def _read(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# parse_master
# --------------------------------------------------------------------------- #

def test_parse_master_multi_bitrate_resolves_relative_and_absolute() -> None:
    rend = hls.parse_master(_read("master_multi.m3u8"), BASE)
    assert [r.bandwidth for r in rend] == [2_176_237, 503_215, 1_880_000]
    first, second, third = rend
    assert (first.width, first.height) == (1280, 720)
    assert first.codecs == "avc1.640028,mp4a.40.2"
    assert first.url.endswith("/720p/index.m3u8")
    assert second.url == (
        "https://video.twimg.com/amplify_video/2058755555980681488/pl/360p/index.m3u8"
    )
    assert (third.width, third.height) == (None, None)
    assert third.url == "https://example.com/twvid/ext/audio-only/index.m3u8"


def test_parse_master_uri_attribute() -> None:
    rend = hls.parse_master(_read("master_uri_attr.m3u8"), BASE)
    assert len(rend) == 1
    assert rend[0].url.endswith("/seg/only.m3u8")
    assert (rend[0].width, rend[0].height) == (1920, 1080)


def test_parse_master_skips_iframe_playlists() -> None:
    rend = hls.parse_master(_read("master_iframe.m3u8"), BASE)
    assert len(rend) == 1
    assert rend[0].url.endswith("/video/index.m3u8")
    assert rend[0].bandwidth == 3_000_000


def test_parse_master_empty_input() -> None:
    assert hls.parse_master("#EXTM3U\n", BASE) == []


# --------------------------------------------------------------------------- #
# choose_rendition
# --------------------------------------------------------------------------- #

def _mk(bw: int, height: int | None, url: str = "c.m3u8") -> hls.Rendition:
    return hls.Rendition(url=url, bandwidth=bw, width=height, height=height)


def test_choose_best_prefers_bandwidth() -> None:
    rend = [_mk(1, 360), _mk(9, 720), _mk(5, 1080)]
    assert hls.choose_rendition(rend) is rend[1]


def test_choose_worst() -> None:
    rend = [_mk(9, None), _mk(1, None), _mk(5, None)]
    assert hls.choose_rendition(rend, QualityPolicy(mode="worst")) is rend[1]


def test_choose_max_height_under_cap() -> None:
    rend = [_mk(1, 360), _mk(9, 720), _mk(5, 1080)]
    got = hls.choose_rendition(rend, QualityPolicy(mode="max_height", max_height=720))
    assert got is rend[1]


def test_choose_max_height_closest_above() -> None:
    rend = [_mk(9, 1080, "u1080.m3u8"), _mk(1, 360, "u360.m3u8")]
    got = hls.choose_rendition(rend, QualityPolicy(mode="max_height", max_height=240))
    assert got.url == "u360.m3u8"


def test_choose_max_height_without_resolution_falls_back_to_bandwidth() -> None:
    rend = [_mk(5, None), _mk(9, None)]
    got = hls.choose_rendition(rend, QualityPolicy(mode="max_height", max_height=720))
    assert got is rend[1]


def test_choose_empty_raises_hls_failed() -> None:
    with pytest.raises(XdlError) as excinfo:
        hls.choose_rendition([])
    assert excinfo.value.code is ErrorCode.HLS_FAILED


# --------------------------------------------------------------------------- #
# DRM gate
# --------------------------------------------------------------------------- #

def test_detect_drm_accepts_no_keys_and_aes128() -> None:
    hls.detect_drm("")
    hls.detect_drm(_read("child_aes128.m3u8"))


@pytest.mark.parametrize(("file", "method"),
                         [("child_widevine.m3u8", "Widevine"),
                          ("child_sample_aes.m3u8", "SAMPLE-AES")])
def test_detect_drm_rejects(file: str, method: str) -> None:
    with pytest.raises(XdlError) as excinfo:
        hls.detect_drm(_read(file))
    assert excinfo.value.code is ErrorCode.DRM_UNSUPPORTED
    assert method in excinfo.value.detail


# --------------------------------------------------------------------------- #
# ffmpeg argv (assert on exact argv arrays — never live runs, per docs)
# --------------------------------------------------------------------------- #

def test_ffmpeg_argv_matches_spec(tmp_path: Path) -> None:
    part = tmp_path / "out.mp4.part"
    argv = hls.ffmpeg_argv("https://x/child.m3u8", part)
    assert argv == [
        "ffmpeg",
        "-hide_banner", "-nostdin", "-loglevel", "error", "-stats",
        "-user_agent", hls.FFMPEG_UA,
        "-allowed_extensions", "ALL",
        "-protocol_whitelist", "file,https,tls,tcp",
        "-i", "https://x/child.m3u8",
        "-map", "0:v:0", "-map", "0:a?",
        "-c", "copy",
        "-movflags", "+faststart",
        "-f", "mp4",  # .part suffix defeats extension inference
        "-y", str(part),
    ]


def test_ffmpeg_argv_progress_mode(tmp_path: Path) -> None:
    argv = hls.ffmpeg_argv(
        "u", tmp_path / "o.mp4.part", "UA/1.0", "C:/bin/ffmpeg.exe", progress=True
    )
    assert "-stats" not in argv
    assert argv.index("-progress") == argv.index("-loglevel") + 2
    assert argv.index("pipe:1") == argv.index("-progress") + 1
    assert argv.index("UA/1.0") == argv.index("-user_agent") + 1
    assert argv.index("-nostats") == argv.index("pipe:1") + 1


# --------------------------------------------------------------------------- #
# require_ffmpeg
# --------------------------------------------------------------------------- #

def test_require_ffmpeg_found(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hls.shutil, "which", lambda name: "C:/bin/ffmpeg.exe")
    assert hls.require_ffmpeg() == "C:/bin/ffmpeg.exe"
    assert hls.require_ffmpeg("explicit.exe") == "explicit.exe"


def test_require_ffmpeg_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hls.shutil, "which", lambda name: None)
    with pytest.raises(XdlError) as excinfo:
        hls.require_ffmpeg()
    assert excinfo.value.code is ErrorCode.HLS_REQUIRES_FFMPEG
    assert "winget install ffmpeg" in excinfo.value.detail


# --------------------------------------------------------------------------- #
# download_hls — hermetic subprocess (a python sim-ffmpeg), stubbed manifests
# --------------------------------------------------------------------------- #

class StubFetch:
    """Serves master text once, then child text for all later calls."""

    def __init__(self, master: str, child: str) -> None:
        self.urls: list[str] = []
        self._master = master
        self._child = child

    async def __call__(self, url: str) -> str:
        self.urls.append(url)
        if len(self.urls) == 1:
            return self._master
        return self._child


def _replace_argv(monkeypatch: pytest.MonkeyPatch, code: str, mode: str) -> None:
    def fake(child_url: str, dest_part: Path, user_agent: str, ffmpeg_path: str,
             *, progress: bool) -> list[str]:
        return [sys.executable, "-c", code, str(dest_part), mode]

    monkeypatch.setattr(hls, "ffmpeg_argv", fake)


async def test_download_hls_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _replace_argv(monkeypatch, SIM, "ok")
    dest = tmp_path / "video.mp4"
    fetch = StubFetch(_read("master_multi.m3u8"), _read("child_plain.m3u8"))
    got = await hls.download_hls(
        BASE, dest, timeout_min=5, fetch_text=fetch, ffmpeg="unused-by-sim"
    )
    assert got == dest
    assert dest.read_bytes().startswith(b"remuxed")
    assert not dest.with_name(dest.name + ".part").exists()
    # Manifest fetches: master first, then the chosen child playlist.
    assert fetch.urls == [
        BASE,
        "https://video.twimg.com/amplify_video/2058755555980681488/pl/720p/index.m3u8",
    ]


async def test_download_hls_no_ffmpeg_fails_fast(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(hls.shutil, "which", lambda name: None)
    fetch = StubFetch(_read("master_multi.m3u8"), _read("child_plain.m3u8"))
    with pytest.raises(XdlError) as excinfo:
        await hls.download_hls(BASE, tmp_path / "v.mp4", fetch_text=fetch)
    assert excinfo.value.code is ErrorCode.HLS_REQUIRES_FFMPEG
    assert fetch.urls == []  # nothing fetched when the toolchain is absent


async def test_download_hls_drm_gate(tmp_path: Path) -> None:
    fetch = StubFetch(_read("master_multi.m3u8"), _read("child_widevine.m3u8"))
    with pytest.raises(XdlError) as excinfo:
        await hls.download_hls(
            BASE, tmp_path / "v.mp4", ffmpeg="unused-by-sim", fetch_text=fetch
        )
    assert excinfo.value.code is ErrorCode.DRM_UNSUPPORTED


async def test_download_hls_failure_tail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _replace_argv(monkeypatch, SIM_FAIL, "fail")
    dest = tmp_path / "v.mp4"
    fetch = StubFetch(_read("master_multi.m3u8"), _read("child_plain.m3u8"))
    with pytest.raises(XdlError) as excinfo:
        await hls.download_hls(BASE, dest, timeout_min=2, fetch_text=fetch)
    err = excinfo.value
    assert err.code is ErrorCode.HLS_FAILED
    assert "boom: invalid data" in err.detail
    assert not dest.exists()


async def test_download_hls_timeout_kills(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _replace_argv(monkeypatch, SIM_TIMEOUT, "timeout")
    fetch = StubFetch(_read("master_multi.m3u8"), _read("child_plain.m3u8"))
    with pytest.raises(XdlError) as excinfo:
        await hls.download_hls(BASE, tmp_path / "v.mp4", timeout_min=0, fetch_text=fetch)
    assert excinfo.value.code is ErrorCode.HLS_FAILED
    assert "timed out" in excinfo.value.detail


async def test_download_hls_progress_frames_flowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _replace_argv(monkeypatch, SIM, "progress")
    dest = tmp_path / "v.mp4"
    got: list[dict[str, str]] = []
    fetch = StubFetch(_read("master_multi.m3u8"), _read("child_plain.m3u8"))
    await hls.download_hls(
        BASE, dest, ffmpeg="unused-by-sim", fetch_text=fetch, frame_cb=got.append
    )
    assert len(got) == 2
    assert all(f.get("out_time_ms") == "1000" for f in got)


# --------------------------------------------------------------------------- #
# Opt-in live smoke (run explicitly with: pytest -m live). Deselected by
# default — everything above is hermetic.
# --------------------------------------------------------------------------- #

@pytest.mark.live
async def test_live_smoke_parse_real_master_playlist() -> None:
    """Refresh a fixture tweet's HLS master URL from the syndication CDN and
    parse it offline-style: >= 1 rendition, argv-buildable child URL."""
    import httpx

    env = json.loads(
        Path("tests/fixtures/syndication/video_nascar_65s.json").read_text(encoding="utf-8")
    )
    params: dict[str, str] = {
        "id": env["tweet_id"],
        "lang": "en",
        "features": env["request_features"],
        "token": env["request_token"],
    }
    resp = httpx.get("https://cdn.syndication.twimg.com/tweet-result",
                     params=params, headers={"User-Agent": "curl/8.0.1"}, timeout=30)
    resp.raise_for_status()
    video = next(m for m in resp.json()["mediaDetails"] if m.get("video_info"))
    master_url = next(
        v["url"] for v in video["video_info"]["variants"]
        if v["content_type"] == "application/x-mpegURL"
    )
    master = httpx.get(master_url, timeout=30).text
    renditions = hls.parse_master(master, master_url)
    assert len(renditions) >= 1
    chosen = hls.choose_rendition(renditions, QualityPolicy())
    argv = hls.ffmpeg_argv(chosen.url, Path("out.mp4.part"))
    assert argv[argv.index("-c") + 1] == "copy"
    assert "-movflags" in argv and "+faststart" in argv[argv.index("-movflags") + 1]
    assert chosen.url.startswith("https://")
