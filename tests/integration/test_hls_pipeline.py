"""M5 pipeline: HLS wiring (MP4 fast-path, sub-720p upgrade, no-ffmpeg
degradation) and --audio-only / --gif conversion flow. Hermetic —
hls.download_hls and convert.run_ffmpeg are monkeypatched at the app's call
sites; ffmpeg presence is faked via shutil.which.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from xdl.app import run_batch
from xdl.config import Settings
from xdl.media import hls
from xdl.models import BatchRequest, ErrorCode
from xdl.storage.state import StateStore

ID = "1750777777777777001"
URL = f"https://x.com/someuser/status/{ID}"

MASTER_1080 = (
    "#EXTM3U\n"
    "#EXT-X-STREAM-INF:BANDWIDTH=2176237,RESOLUTION=1920x1080\n"
    f"/{ID}/pl/1080p/index.m3u8\n"
    "#EXT-X-STREAM-INF:BANDWIDTH=503215,RESOLUTION=640x360\n"
    f"/{ID}/pl/360p/index.m3u8\n"
)
MASTER_360 = (
    "#EXTM3U\n"
    "#EXT-X-STREAM-INF:BANDWIDTH=503215,RESOLUTION=640x360\n"
    f"/{ID}/pl/360p/index.m3u8\n"
)


def payload(
    tweet_id: str = ID,
    *,
    height: int = 720,
    variants: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if variants is None:
        variants = [
            {"url": f"https://video.twimg.com/{tweet_id}_720.mp4",
             "bitrate": 2176000, "content_type": "video/mp4"},
            {"url": f"https://video.twimg.com/{tweet_id}_320.mp4",
             "bitrate": 320000, "content_type": "video/mp4"},
        ]
    return {
        "__typename": "Tweet",
        "id_str": tweet_id,
        "text": "post",
        "created_at": "2026-09-30T21:14:03.000Z",
        "user": {"screen_name": "someuser", "name": "Some User"},
        "mediaDetails": [
            {
                "type": "video",
                "media_url_https": f"https://pbs.twimg.com/thumb_{tweet_id}.jpg",
                "original_info": {"width": 1280, "height": height},
                "video_info": {"variants": variants},
            }
        ],
    }


def make_world(
    tweet_payload: dict[str, Any], *, playlists: dict[str, str] | None = None
) -> tuple[Any, dict[str, Any]]:
    """Returns (handler, shared-log). Build a fresh AsyncClient per run —
    httpx clients cannot be reopened once closed."""
    log: dict[str, Any] = {"media_gets": [], "playlists": []}
    playlists = playlists or {}

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if host == "cdn.syndication.twimg.com":
            return httpx.Response(200, json=tweet_payload)
        if host == "video.twimg.com":
            path = request.url.path
            if path in playlists:
                log["playlists"].append(path)
                return httpx.Response(200, text=playlists[path])
            log["media_gets"].append(path)
            body = f"body:{path}".encode()
            return httpx.Response(200, content=body,
                                  headers={"content-length": str(len(body))})
        return httpx.Response(404)

    return handler, log


def world_client(handler: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def request_for(settings: Settings, **kw: Any) -> BatchRequest:
    return BatchRequest(inputs=[URL], settings=settings, **kw)


@pytest.fixture
def fake_ffmpeg(monkeypatch: pytest.MonkeyPatch) -> str:
    path = "C:/fake-bin/ffmpeg.exe"
    monkeypatch.setattr("xdl.app.shutil.which", lambda name: path)
    return path


@pytest.fixture
def no_ffmpeg(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("xdl.app.shutil.which", lambda name: None)


@pytest.fixture
def hls_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> list[dict[str, Any]]:
    """Replace hls.download_hls with a recorder that 'remuxes' one file."""
    calls: list[dict[str, Any]] = []

    async def fake_download(
        master_url: str,
        dest: Path,
        *,
        policy: Any = None,
        user_agent: str = hls.FFMPEG_UA,
        ffmpeg: str | None = None,
        timeout_min: int = 30,
        fetch_text: Any = None,
        frame_cb: Any = None,
    ) -> Path:
        calls.append({"master_url": master_url, "dest": dest, "ffmpeg": ffmpeg})
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"remuxed-via-ffmpeg")
        return dest

    monkeypatch.setattr(hls, "download_hls", fake_download)
    return calls


# --------------------------------------------------------------------------- #
# HLS acquisition (m3u8-only posts) — the M5 swap-in at app._download_media
# --------------------------------------------------------------------------- #


async def test_hls_only_post_remuxes_when_ffmpeg_present(
    settings: Settings, fake_ffmpeg: str, hls_calls: list[dict[str, Any]]
) -> None:
    tweet = payload(variants=[
        {"url": f"https://video.twimg.com/{ID}/pl/master.m3u8",
         "bitrate": None, "content_type": "application/x-mpegURL"},
    ])
    handler, log = make_world(tweet)
    async with world_client(handler) as client:
        report = await run_batch(request_for(settings), client=client)

    assert report.ok == 1
    (call,) = hls_calls
    assert call["master_url"] == f"https://video.twimg.com/{ID}/pl/master.m3u8"
    assert call["ffmpeg"] == fake_ffmpeg
    dest = report.results[0].files[0]
    assert dest.suffix == ".mp4" and dest.read_bytes() == b"remuxed-via-ffmpeg"
    assert dest.with_name(dest.name + ".meta.json").is_file()  # sidecar written
    assert log["media_gets"] == []  # segments are ffmpeg's job, not ours


async def test_hls_only_without_ffmpeg_fails_with_install_hint(
    settings: Settings, no_ffmpeg: None, hls_calls: list[dict[str, Any]]
) -> None:
    tweet = payload(variants=[
        {"url": f"https://video.twimg.com/{ID}/pl/master.m3u8",
         "bitrate": None, "content_type": "application/x-mpegURL"},
    ])
    handler, _ = make_world(tweet)
    async with world_client(handler) as client:
        report = await run_batch(request_for(settings), client=client)

    assert report.failed == 1
    error = report.results[0].error
    assert error is not None and error.code is ErrorCode.HLS_REQUIRES_FFMPEG
    assert "winget install ffmpeg" in error.detail
    assert hls_calls == []  # never launched


# --------------------------------------------------------------------------- #
# MP4 fast-path + sub-720p upgrade (docs/technical/04 banner policy)
# --------------------------------------------------------------------------- #


async def test_720p_mp4_never_considers_hls(
    settings: Settings, fake_ffmpeg: str, hls_calls: list[dict[str, Any]]
) -> None:
    tweet = payload(variants=[
        {"url": f"https://video.twimg.com/{ID}_720.mp4",
         "bitrate": 2176000, "content_type": "video/mp4"},
        {"url": f"https://video.twimg.com/{ID}/pl/master.m3u8",
         "bitrate": None, "content_type": "application/x-mpegURL"},
    ])
    handler, log = make_world(tweet, playlists={f"/{ID}/pl/master.m3u8": MASTER_1080})
    async with world_client(handler) as client:
        report = await run_batch(request_for(settings), client=client)

    assert report.ok == 1
    assert hls_calls == []  # MP4 fast-path wins
    assert log["playlists"] == []  # master not even fetched
    assert log["media_gets"] == [f"/{ID}_720.mp4"]


async def test_sub720p_mp4_upgrades_when_playlist_genuinely_taller(
    settings: Settings, fake_ffmpeg: str, hls_calls: list[dict[str, Any]]
) -> None:
    tweet = payload(height=480, variants=[
        {"url": f"https://video.twimg.com/{ID}_480.mp4",
         "bitrate": 750000, "content_type": "video/mp4"},
        {"url": f"https://video.twimg.com/{ID}/pl/master.m3u8",
         "bitrate": None, "content_type": "application/x-mpegURL"},
    ])
    handler, log = make_world(tweet, playlists={f"/{ID}/pl/master.m3u8": MASTER_1080})
    async with world_client(handler) as client:
        report = await run_batch(request_for(settings), client=client)

    assert report.ok == 1
    assert log["playlists"] == [f"/{ID}/pl/master.m3u8"]  # the decision fetch
    (call,) = hls_calls  # best rendition (1080) beats the 480 mp4
    assert call["master_url"] == f"https://video.twimg.com/{ID}/pl/master.m3u8"
    assert log["media_gets"] == []  # mp4 not downloaded


async def test_sub720p_keeps_mp4_when_playlist_not_taller(
    settings: Settings, fake_ffmpeg: str, hls_calls: list[dict[str, Any]]
) -> None:
    tweet = payload(height=480, variants=[
        {"url": f"https://video.twimg.com/{ID}_480.mp4",
         "bitrate": 750000, "content_type": "video/mp4"},
        {"url": f"https://video.twimg.com/{ID}/pl/master.m3u8",
         "bitrate": None, "content_type": "application/x-mpegURL"},
    ])
    handler, _ = make_world(tweet, playlists={f"/{ID}/pl/master.m3u8": MASTER_360})
    async with world_client(handler) as client:
        report = await run_batch(request_for(settings), client=client)

    assert report.ok == 1
    assert hls_calls == []  # best rendition (360) <= 480 -> keep the MP4
    assert report.results[0].files[0].read_bytes().startswith(b"body:")


async def test_sub720p_without_ffmpeg_degrades_to_mp4(
    settings: Settings, no_ffmpeg: None, hls_calls: list[dict[str, Any]]
) -> None:
    tweet = payload(height=480, variants=[
        {"url": f"https://video.twimg.com/{ID}_480.mp4",
         "bitrate": 750000, "content_type": "video/mp4"},
        {"url": f"https://video.twimg.com/{ID}/pl/master.m3u8",
         "bitrate": None, "content_type": "application/x-mpegURL"},
    ])
    handler, _ = make_world(tweet, playlists={f"/{ID}/pl/master.m3u8": MASTER_1080})
    async with world_client(handler) as client:
        report = await run_batch(request_for(settings), client=client)

    assert report.ok == 1  # docs: use best MP4, no failure
    assert hls_calls == []


# --------------------------------------------------------------------------- #
# --audio-only (MP3) and --gif conversion flow
# --------------------------------------------------------------------------- #


@pytest.fixture
def convert_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> list[dict[str, Any]]:
    """Replace convert.run_ffmpeg with a recorder that writes the .part."""
    calls: list[dict[str, Any]] = []

    async def fake_run(
        argv: list[str], *, timeout_min: int = 30, frame_cb: Any = None
    ) -> None:
        calls.append({"argv": argv})
        part = Path(argv[-1])
        part.write_bytes(b"converted-artifact")
        if frame_cb is not None:
            frame_cb({"total_size": "18", "progress": "continue"})

    monkeypatch.setattr("xdl.media.convert.run_ffmpeg", fake_run)
    return calls


async def test_audio_only_produces_mp3(
    settings: Settings, fake_ffmpeg: str, convert_calls: list[dict[str, Any]]
) -> None:
    handler, _ = make_world(payload())
    async with world_client(handler) as client:
        report = await run_batch(request_for(settings, audio_only=True),
                                 client=client)

    assert report.ok == 1
    dest = report.results[0].files[0]
    assert dest.suffix == ".mp3" and dest.read_bytes() == b"converted-artifact"
    assert dest.with_name(dest.name + ".meta.json").is_file()

    (call,) = convert_calls
    argv = call["argv"]
    assert "-vn" in argv and "libmp3lame" in argv and argv[-1].endswith(".mp3.part")
    src = Path(argv[argv.index("-i") + 1])
    assert src.name.endswith(".src.mp4")  # transient container, not the final
    assert not src.exists()  # cleaned up after the transcode

    state = StateStore(settings.db_path)
    try:
        (row,) = state.ok_rows(ID)
        assert Path(row.file_path) == dest
    finally:
        state.close()


async def test_gif_conversion_two_pass_palette(
    settings: Settings, fake_ffmpeg: str, convert_calls: list[dict[str, Any]]
) -> None:
    handler, _ = make_world(payload())
    async with world_client(handler) as client:
        report = await run_batch(request_for(settings, gif=True), client=client)

    assert report.ok == 1
    dest = report.results[0].files[0]
    assert dest.suffix == ".gif"
    (call,) = convert_calls
    argv = call["argv"]
    vf = argv[argv.index("-vf") + 1]
    assert "palettegen" in vf and "paletteuse" in vf


async def test_audio_only_without_ffmpeg_fails_fast(
    settings: Settings, no_ffmpeg: None, convert_calls: list[dict[str, Any]]
) -> None:
    handler, log = make_world(payload())
    async with world_client(handler) as client:
        report = await run_batch(request_for(settings, audio_only=True),
                                 client=client)

    assert report.failed == 1
    error = report.results[0].error
    assert error is not None and error.code is ErrorCode.FFMPEG_REQUIRED
    assert "winget install ffmpeg" in error.detail
    assert convert_calls == [] and log["media_gets"] == []  # nothing fetched


async def test_audio_only_resumes_from_staged_container(
    settings: Settings, fake_ffmpeg: str, convert_calls: list[dict[str, Any]]
) -> None:
    """A crashed earlier attempt (container staged as .src.mp4, no .mp3 yet)
    resumes at the transcode step — the container is NOT re-downloaded."""
    handler, log = make_world(payload())
    async with world_client(handler) as client:
        report = await run_batch(request_for(settings, audio_only=True),
                                 client=client)
    dest = report.results[0].files[0]

    # simulate the crash window: mp3 gone, state back to incomplete, the
    # transient container still on disk
    dest.unlink()
    state = StateStore(settings.db_path)
    try:
        (row,) = state.ok_rows(ID)
        state.record(row.model_copy(update={"status": "incomplete"}))
    finally:
        state.close()
    src = dest.with_name(dest.stem + ".src.mp4")
    src.write_bytes(b"staged-container")

    fetches_before = len(log["media_gets"])
    async with world_client(handler) as client2:
        report2 = await run_batch(request_for(settings, audio_only=True),
                                  client=client2)
    assert report2.ok == 1
    assert len(log["media_gets"]) == fetches_before  # no re-fetch
    assert len(convert_calls) == 2
    assert dest.read_bytes() == b"converted-artifact"
    assert not src.exists()  # transient container cleaned up
