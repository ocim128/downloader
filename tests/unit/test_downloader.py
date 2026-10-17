"""Engine downloader: .part, resume, Content-Range totals, atomic rename.

All HTTP through MockTransport — hermetic (docs/technical/09).
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from xdl.engine.downloader import fetch
from xdl.models import ErrorCode, XdlError

MEDIA_URL = "https://video.twimg.com/ext_tw_video/1/vid/avc1/720x1280/abc.mp4"
BODY = b"\x00\x01\x02" * 100  # 300 bytes


def dest(tmp_path: Path, name: str = "video.mp4") -> Path:
    return tmp_path / "x" / "user" / name


def client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_happy_path_writes_file_and_no_part(tmp_path: Path) -> None:
    seen_ranges: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_ranges.append(request.headers.get("range"))
        return httpx.Response(200, content=BODY,
                              headers={"content-length": str(len(BODY))})

    async with client(handler) as c:
        target = await fetch(MEDIA_URL, dest(tmp_path), c)

    assert target.read_bytes() == BODY
    assert not target.with_name(target.name + ".part").exists()
    assert seen_ranges == [None]  # no offset -> no Range header


async def test_progress_callback_totals(tmp_path: Path) -> None:
    calls: list[tuple[int, int | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=BODY)

    async with client(handler) as c:
        await fetch(MEDIA_URL, dest(tmp_path), c, cb=lambda d, t: calls.append((d, t)))

    assert sum(d for d, _ in calls) == len(BODY)
    assert all(t == 300 for _, t in calls)


async def test_resume_sends_range_and_merges(tmp_path: Path) -> None:
    target = dest(tmp_path)
    target.parent.mkdir(parents=True)
    part = target.with_name(target.name + ".part")
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
        result = await fetch(MEDIA_URL, target, c)

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

    (tmp_path / "x" / "user").mkdir(parents=True)
    (tmp_path / "x" / "user" / "f.mp4.part").write_bytes(b"")  # offset 0 w/ .part

    async with client(handler) as c:
        target = await fetch(MEDIA_URL, dest(tmp_path, "f.mp4"), c)
    assert target.read_bytes() == BODY


async def test_200_on_ranged_request_restarts_clean(tmp_path: Path) -> None:
    target = dest(tmp_path)
    target.parent.mkdir(parents=True)
    part = target.with_name(target.name + ".part")
    part.write_bytes(b"GARBAGE-PREFIX-FROM-ANOTHER-FILE")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=BODY)  # server ignores Range

    async with client(handler) as c:
        result = await fetch(MEDIA_URL, target, c)

    assert result.read_bytes() == BODY  # garbage prefix never appended


async def test_unknown_length_no_mismatch(tmp_path: Path) -> None:
    # chunked response (no content-length): clean stream end is the bar
    async def chunks():  # async generator -> chunked transfer, no length header
        yield BODY[:150]
        yield BODY[150:]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=chunks())

    async with client(handler) as c:
        target = await fetch(MEDIA_URL, dest(tmp_path), c)
    assert target.read_bytes() == BODY


async def test_size_mismatch_raises_and_deletes_part(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=BODY[:-5],
                              headers={"content-length": str(len(BODY))})

    target = dest(tmp_path)
    async with client(handler) as c:
        with pytest.raises(XdlError) as exc:
            await fetch(MEDIA_URL, target, c)
    assert exc.value.code is ErrorCode.INTEGRITY_MISMATCH
    assert not target.with_name(target.name + ".part").exists()


async def test_403_media_url_raises_link_expired(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403)

    async with client(handler) as c:
        with pytest.raises(XdlError) as exc:
            await fetch(MEDIA_URL, dest(tmp_path), c)
    assert exc.value.code is ErrorCode.LINK_EXPIRED


async def test_429_raises_rate_limited(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"retry-after": "17"})

    async with client(handler) as c:
        with pytest.raises(XdlError) as exc:
            await fetch(MEDIA_URL, dest(tmp_path), c)
    assert exc.value.code is ErrorCode.RATE_LIMITED
    assert exc.value.retry_after == 17.0


async def test_disallowed_media_host_rejected(tmp_path: Path) -> None:
    async with client(lambda r: httpx.Response(200)) as c:
        with pytest.raises(XdlError) as exc:
            await fetch("https://evil.net/video.mp4", dest(tmp_path), c)
    assert exc.value.code is ErrorCode.URL_UNRECOGNIZED


async def test_5xx_maps_to_upstream_error(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    async with client(handler) as c:
        with pytest.raises(XdlError) as exc:
            await fetch(MEDIA_URL, dest(tmp_path), c)
    assert exc.value.code is ErrorCode.UPSTREAM_ERROR


async def test_network_error_maps_to_network(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    async with client(handler) as c:
        with pytest.raises(XdlError) as exc:
            await fetch(MEDIA_URL, dest(tmp_path), c)
    assert exc.value.code is ErrorCode.NETWORK


# --------------------------------------------------------------------------- #
# cancellation (docs/technical/03: checked per chunk; .part retained)
# --------------------------------------------------------------------------- #


async def test_cancel_before_request_touches_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from xdl.models import CancelToken

    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, content=BODY)

    token = CancelToken()
    token.cancel()
    async with client(handler) as c:
        with pytest.raises(XdlError) as exc:
            await fetch(MEDIA_URL, dest(tmp_path), c, cancel=token)
    assert exc.value.code is ErrorCode.CANCELLED
    assert calls == []  # checked before the request goes out
    assert not (tmp_path / "x" / "user" / "video.mp4.part").exists()


async def test_cancel_midstream_keeps_part(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import xdl.engine.downloader as dl
    from xdl.models import CancelToken

    monkeypatch.setattr(dl, "_CHUNK", 50)  # BODY is 300 bytes -> 6 chunks

    token = CancelToken()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=BODY,
                              headers={"content-length": str(len(BODY))})

    def cb(delta: int, total: int | None) -> None:
        token.cancel()  # SIGINT lands after the first chunk boundary

    target = dest(tmp_path)
    async with client(handler) as c:
        with pytest.raises(XdlError) as exc:
            await fetch(MEDIA_URL, target, c, cb=cb, cancel=token)
    assert exc.value.code is ErrorCode.CANCELLED
    part = target.with_name(target.name + ".part")
    assert part.exists() and part.read_bytes() == BODY[:50]  # resumable
    assert not target.exists()  # never a half-file posing as complete


async def test_disk_write_errors_map_to_write_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A raw OSError from the write path must stay at item level (WRITE_ERROR),
    never crash the batch (docs/technical/08 taxonomy)."""
    from pathlib import Path as P

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=BODY)

    real_open = P.open

    def bad_open(self: P, *args: object, **kw: object) -> object:
        if self.name.endswith(".part"):
            raise PermissionError(13, "permission denied")
        return real_open(self, *args, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(P, "open", bad_open)
    async with client(handler) as c:
        with pytest.raises(XdlError) as exc:
            await fetch(MEDIA_URL, dest(tmp_path), c)
    assert exc.value.code is ErrorCode.WRITE_ERROR
    assert "permission denied" in exc.value.detail
