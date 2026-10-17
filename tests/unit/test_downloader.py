"""Engine downloader: .part, resume, Content-Range totals, atomic rename.

All HTTP through MockTransport — hermetic (docs/technical/09).
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from xdl.engine.downloader import fetch
from xdl.models import DownloadJob, ErrorCode, LinkExpiredError, TweetRef, XdlError

MEDIA_URL = "https://video.twimg.com/ext_tw_video/1/vid/avc1/720x1280/abc.mp4"
BODY = b"\x00\x01\x02" * 100  # 300 bytes


def job(tmp_path: Path, url: str = MEDIA_URL) -> DownloadJob:
    return DownloadJob(
        ref=TweetRef(tweet_id="1750123456789012352", screen_name="u",
                     canonical_url="https://x.com/u/status/1750123456789012352"),
        media_index=1,
        url=url,
        dest=tmp_path / "x" / "user" / "video.mp4",
    )


def client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_happy_path_writes_file_and_no_part(tmp_path: Path) -> None:
    seen_ranges: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_ranges.append(request.headers.get("range"))
        return httpx.Response(200, content=BODY,
                              headers={"content-length": str(len(BODY))})

    async with client(handler) as c:
        dest = await fetch(job(tmp_path), c)

    assert dest.read_bytes() == BODY
    assert not dest.with_name(dest.name + ".part").exists()
    assert seen_ranges == [None]  # no offset -> no Range header


async def test_progress_callback_totals(tmp_path: Path) -> None:
    calls: list[tuple[int, int | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=BODY)

    async with client(handler) as c:
        await fetch(job(tmp_path), c, cb=lambda d, t: calls.append((d, t)))

    assert sum(d for d, _ in calls) == len(BODY)
    assert all(t == 300 for _, t in calls)


async def test_resume_sends_range_and_merges(tmp_path: Path) -> None:
    dest = tmp_path / "x" / "user" / "video.mp4"
    dest.parent.mkdir(parents=True)
    part = dest.with_name(dest.name + ".part")
    part.write_bytes(BODY[:100])  # partial from a previous run

    requests_seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(request)
        assert request.headers["range"] == "bytes=100-"
        return httpx.Response(
            206,
            content=BODY[100:],
            headers={
                "content-range": f"bytes 100-{len(BODY) - 1}/{len(BODY)}",
                "content-length": str(len(BODY) - 100),
            },
        )

    async with client(handler) as c:
        result = await fetch(job(tmp_path), c)

    assert result.read_bytes() == BODY  # prefix + suffix merged
    assert not part.exists()


async def test_206_total_parsed_from_content_range_not_length(tmp_path: Path) -> None:
    # round-2 fix: a 206's Content-Length is only the REMAINING bytes
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            206,
            content=BODY,
            headers={"content-range": f"bytes 0-{len(BODY) - 1}/{len(BODY)}"},
        )

    j = job(tmp_path)
    j = j.model_copy(update={"dest": tmp_path / "x" / "user" / "f.mp4"})
    (tmp_path / "x" / "user").mkdir(parents=True)
    (tmp_path / "x" / "user" / "f.mp4.part").write_bytes(b"")  # offset 0 w/ .part

    async with client(handler) as c:
        dest = await fetch(j, c)
    assert dest.read_bytes() == BODY


async def test_200_on_ranged_request_restarts_clean(tmp_path: Path) -> None:
    dest = tmp_path / "x" / "user" / "video.mp4"
    dest.parent.mkdir(parents=True)
    part = dest.with_name(dest.name + ".part")
    part.write_bytes(b"GARBAGE-PREFIX-FROM-ANOTHER-FILE")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=BODY)  # server ignores Range

    async with client(handler) as c:
        result = await fetch(job(tmp_path), c)

    assert result.read_bytes() == BODY  # garbage prefix never appended


async def test_unknown_length_no_mismatch(tmp_path: Path) -> None:
    # chunked response (no content-length): clean stream end is the bar
    async def chunks():  # async generator -> chunked transfer, no length header
        yield BODY[:150]
        yield BODY[150:]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=chunks())

    async with client(handler) as c:
        dest = await fetch(job(tmp_path), c)
    assert dest.read_bytes() == BODY


async def test_size_mismatch_raises_and_deletes_part(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=BODY[:-5],
                              headers={"content-length": str(len(BODY))})

    async with client(handler) as c:
        with pytest.raises(XdlError) as exc:
            await fetch(job(tmp_path), c)
    assert exc.value.code is ErrorCode.INTEGRITY_MISMATCH
    j = job(tmp_path)
    assert not j.dest.with_name(j.dest.name + ".part").exists()


async def test_403_media_url_raises_link_expired(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403)

    async with client(handler) as c:
        with pytest.raises(LinkExpiredError):
            await fetch(job(tmp_path), c)


async def test_429_raises_rate_limited(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"retry-after": "17"})

    async with client(handler) as c:
        with pytest.raises(XdlError) as exc:
            await fetch(job(tmp_path), c)
    assert exc.value.code is ErrorCode.RATE_LIMITED
    assert exc.value.retry_after == 17.0


async def test_disallowed_media_host_rejected(tmp_path: Path) -> None:
    async with client(lambda r: httpx.Response(200)) as c:
        with pytest.raises(XdlError) as exc:
            await fetch(job(tmp_path, url="https://evil.net/video.mp4"), c)
    assert exc.value.code is ErrorCode.URL_UNRECOGNIZED


async def test_5xx_maps_to_upstream_error(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    async with client(handler) as c:
        with pytest.raises(XdlError) as exc:
            await fetch(job(tmp_path), c)
    assert exc.value.code is ErrorCode.UPSTREAM_ERROR


async def test_network_error_maps_to_network(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    async with client(handler) as c:
        with pytest.raises(XdlError) as exc:
            await fetch(job(tmp_path), c)
    assert exc.value.code is ErrorCode.NETWORK
