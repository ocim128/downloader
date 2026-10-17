"""M4 resilience over a mock world: retries/backoff, 429 global pause +
escalation, circuit breaker, graceful cancel, --force .bak, LINK_EXPIRED
recovery (policy + object-identity). Hermetic — MockTransport everywhere,
sleeps faked (docs/technical/08, 09).
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

import xdl.engine.downloader as dl
from xdl.app import run_batch
from xdl.config import Settings
from xdl.models import BatchRequest, CancelToken, ErrorCode
from xdl.storage.state import StateStore

ID = "1750123456789012352"


# --------------------------------------------------------------------------- #
# mock world
# --------------------------------------------------------------------------- #


class World:
    """Scriptable MockTransport world: syndication payloads + media steps."""

    def __init__(self) -> None:
        self.payloads: dict[str, dict[str, Any]] = {}
        self.payload_seq: dict[str, list[dict[str, Any]]] = {}
        self.media_seq: dict[str, list[tuple[str, Any]]] = {}
        self.bodies: dict[str, bytes] = {}
        self.playlists: dict[str, str] = {}
        self.media_gets: list[str] = []
        self.resolve_calls: list[str] = []

    # scripting API ------------------------------------------------------- #

    def resolve(self, tweet_id: str, *seq: dict[str, Any]) -> None:
        if seq:
            self.payload_seq[tweet_id] = list(seq)
            self.payloads[tweet_id] = seq[-1]
        else:
            self.payloads[tweet_id] = video_payload(tweet_id)

    def media(self, path: str, *steps: tuple[str, Any], body: bytes | None = None) -> None:
        if steps:
            self.media_seq[path] = list(steps)
        if body is not None:
            self.bodies[path] = body
    # transport ------------------------------------------------------------ #

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self._handler))

    def _handler(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host or ""
        if host == "cdn.syndication.twimg.com":
            tweet_id = str(request.url.params["id"])
            self.resolve_calls.append(tweet_id)
            payload = self._next_payload(tweet_id)
            if payload is None:
                return httpx.Response(404)
            return httpx.Response(200, json=payload)
        if host == "video.twimg.com":
            path = request.url.path
            if path in self.playlists:
                return httpx.Response(200, text=self.playlists[path])
            self.media_gets.append(path)
            kind, arg = self._next_media_step(path)
            if kind == "429":
                return httpx.Response(
                    429, headers={"retry-after": str(arg) if arg is not None else "1"}
                )
            if kind == "403":
                return httpx.Response(403)
            if kind == "500":
                return httpx.Response(500)
            assert kind == "200"
            body = arg if isinstance(arg, bytes) else self.bodies.get(
                path, f"body:{path}".encode()
            )
            rng = request.headers.get("range")
            if rng:
                start = int(rng.split("=", 1)[1].split("-", 1)[0])
                return httpx.Response(
                    206,
                    content=body[start:],
                    headers={
                        "content-range": f"bytes {start}-{len(body) - 1}/{len(body)}",
                        "content-length": str(len(body) - start),
                    },
                )
            return httpx.Response(200, content=body,
                                  headers={"content-length": str(len(body))})
        return httpx.Response(404)

    def _next_payload(self, tweet_id: str) -> dict[str, Any] | None:
        seq = self.payload_seq.get(tweet_id)
        if seq:
            if len(seq) > 1:
                return seq.pop(0)
            return seq[0]
        return self.payloads.get(tweet_id)

    def _next_media_step(self, path: str) -> tuple[str, Any]:
        """Scripted steps are consumed in order; afterwards the default is a
        plain 200 of the registered body — tests express "fails N times, then
        the CDN behaves"."""
        seq = self.media_seq.get(path)
        if seq:
            return seq.pop(0)
        return ("200", None)


def video_payload(
    tweet_id: str,
    *,
    user: str = "someuser",
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
        "text": f"post {tweet_id}",
        "created_at": "2026-09-30T21:14:03.000Z",
        "user": {"screen_name": user, "name": user.title()},
        "mediaDetails": [
            {
                "type": "video",
                "media_url_https": f"https://pbs.twimg.com/thumb_{tweet_id}.jpg",
                "original_info": {"width": 1280, "height": height},
                "video_info": {"variants": variants},
            }
        ],
    }


def url_for(tweet_id: str) -> str:
    return f"https://x.com/someuser/status/{tweet_id}"


def request_for(urls: list[str], settings: Settings, **kw: Any) -> BatchRequest:
    return BatchRequest(inputs=urls, settings=settings, **kw)


@pytest.fixture
def fake_backoff(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Replace the app's backoff sleep with a recorder (hermetic, instant)."""
    sleeps: list[float] = []

    async def fake_sleep(seconds: float, cancel: CancelToken | None = None) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr("xdl.app._sleep", fake_sleep)
    return sleeps


@pytest.fixture
def fake_throttle_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Make ThrottleState.gate's cooldown sleeps instant + recorded (the 429
    floor of 30 s would otherwise be a real sleep in tests)."""
    waited: list[float] = []

    async def instant(seconds: float) -> None:
        waited.append(seconds)

    import types

    monkeypatch.setattr(
        "xdl.engine.throttle.asyncio", types.SimpleNamespace(sleep=instant)
    )
    return waited


# --------------------------------------------------------------------------- #
# per-item retries (backoff 1,2,4,8,16 s x jitter, cap --retries attempts)
# --------------------------------------------------------------------------- #


async def test_media_5xx_retries_with_backoff_then_succeeds(
    settings: Settings, fake_backoff: list[float]
) -> None:
    settings.engine.retries = 5
    world = World()
    world.resolve(ID)
    world.media(f"/{ID}_720.mp4", ("500", None), ("500", None))  # then default 200

    async with world.client() as client:
        report = await run_batch(request_for([url_for(ID)], settings), client=client)

    assert report.exit_code == 0 and report.ok == 1
    assert len(world.media_gets) == 3  # 2 failures + success
    assert len(fake_backoff) == 2  # backoff after failure 1 and 2
    for delay in fake_backoff:
        assert 0.75 <= delay <= 2 * 1.25  # first steps: 1 s and 2 s x jitter


async def test_backoff_progression_and_jitter_bounds(
    fake_backoff: list[float],
) -> None:
    from xdl.app import _backoff_delay

    for attempt, base in enumerate((1.0, 2.0, 4.0, 8.0, 16.0, 32.0), start=1):
        for _ in range(20):
            assert base * 0.75 <= _backoff_delay(attempt) <= base * 1.25
    assert _backoff_delay(20) <= 300.0 * 1.25  # capped


async def test_retries_exhausted_fails_item(
    settings: Settings, fake_backoff: list[float]
) -> None:
    settings.engine.retries = 3
    world = World()
    world.resolve(ID)
    world.media(f"/{ID}_720.mp4", ("500", None), ("500", None), ("500", None))

    async with world.client() as client:
        report = await run_batch(request_for([url_for(ID)], settings), client=client)

    assert report.failed == 1 and report.exit_code == 2
    assert len(world.media_gets) == 3  # exactly --retries attempts, no more
    assert "gave up after 3 attempts" in report.results[0].detail


async def test_non_retryable_errors_fail_immediately(
    settings: Settings, fake_backoff: list[float]
) -> None:
    # syndication 404 -> NOT_EMBEDDABLE (terminal, not retryable)
    world = World()
    async with world.client() as client:
        report = await run_batch(
            request_for([url_for("1750999999999999999")], settings), client=client
        )
    assert report.failed == 1
    assert fake_backoff == []  # no backoff for non-retryable codes
    assert report.results[0].error is not None
    assert report.results[0].error.code is ErrorCode.NOT_EMBEDDABLE


# --------------------------------------------------------------------------- #
# 429: global pause + Retry-After + escalation
# --------------------------------------------------------------------------- #


async def test_media_429_pauses_everyone_then_recovers(
    settings: Settings, fake_throttle_sleep: list[float], fake_backoff: list[float]
) -> None:
    world = World()
    world.resolve(ID)
    world.media(f"/{ID}_720.mp4", ("429", 5))  # Retry-After: 5 -> 30 s floor

    async with world.client() as client:
        report = await run_batch(request_for([url_for(ID)], settings), client=client)

    assert report.ok == 1
    assert len(world.media_gets) == 2  # limited, then retried
    assert max(fake_throttle_sleep) >= 29.0  # the 30 s floor paused the retry


async def test_resolve_429_reports_global_pause(
    settings: Settings, fake_throttle_sleep: list[float], fake_backoff: list[float]
) -> None:
    world = World()
    world.resolve(ID)  # registers the media bodies too
    resolve_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal resolve_count
        if request.url.host == "cdn.syndication.twimg.com":
            resolve_count += 1
            if resolve_count == 1:
                return httpx.Response(429, headers={"retry-after": "7"})
            return httpx.Response(200, json=world.payloads[ID])
        return world._handler(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        report = await run_batch(request_for([url_for(ID)], settings), client=client)

    assert report.ok == 1
    assert max(fake_throttle_sleep) >= 29.0  # resolve 429 -> global 30 s floor


async def test_429_escalation_after_three(
    settings: Settings, fake_throttle_sleep: list[float], fake_backoff: list[float]
) -> None:
    settings.engine.retries = 5
    world = World()
    world.resolve(ID)
    world.media(f"/{ID}_720.mp4", ("429", 1), ("429", 1), ("429", 1))  # then 200

    async with world.client() as client:
        report = await run_batch(request_for([url_for(ID)], settings), client=client)

    assert report.ok == 1
    # 3 x 429 within 60 s -> pause escalates to >= 120 s
    assert max(fake_throttle_sleep) >= 119.0


# --------------------------------------------------------------------------- #
# circuit breaker (8 consecutive item failures, 0 successes between)
# --------------------------------------------------------------------------- #


async def test_circuit_breaker_drains_batch(
    settings: Settings, fake_backoff: list[float]
) -> None:
    settings.engine.retries = 1  # one attempt per item -> deterministic count
    settings.engine.resolve_concurrency = 1  # strictly serial items

    ids = [f"1750555555555555{i:03d}" for i in range(10)]
    resolve_calls: list[str] = []

    def fail_all(request: httpx.Request) -> httpx.Response:
        if request.url.host == "cdn.syndication.twimg.com":
            resolve_calls.append(str(request.url.params["id"]))
            return httpx.Response(500)  # UPSTREAM_ERROR, but retries=1
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(fail_all)) as client:
        report = await run_batch(request_for([url_for(i) for i in ids], settings),
                                 client=client)

    assert len(resolve_calls) == 8  # items 9+10 never started
    statuses = [r.status for r in report.results]
    assert statuses == ["failed"] * 8 + ["skipped"] * 2
    assert "SKIPPED_CIRCUIT_OPEN" in report.results[8].detail
    assert report.exit_code == 1  # mixed, not all-failed

    # skipped items recorded incomplete -> retry-failed picks them up
    state = StateStore(settings.db_path)
    try:
        retriable = {r.tweet_id for r in state.failed_items()}
    finally:
        state.close()
    assert set(ids[8:]) <= retriable


async def test_breaker_resets_on_interleaved_success(
    settings: Settings, fake_backoff: list[float]
) -> None:
    settings.engine.retries = 1
    settings.engine.resolve_concurrency = 1

    ids = [f"1750666666666666{i:03d}" for i in range(10)]
    world = World()
    for i, tweet_id in enumerate(ids):
        if i % 2 == 0:
            world.resolve(tweet_id)  # ok item resets the consecutive count
        else:
            world.payloads[tweet_id] = video_payload(tweet_id)
            world.media(f"/{tweet_id}_720.mp4", ("500", None))
            world.media(f"/{tweet_id}_320.mp4", ("500", None))

    async with world.client() as client:
        report = await run_batch(request_for([url_for(i) for i in ids], settings),
                                 client=client)

    # 5 failures interleaved with 5 successes: breaker never opens (needs 8)
    assert not any("SKIPPED_CIRCUIT_OPEN" in r.detail for r in report.results)
    assert report.failed == 5 and report.ok == 5


# --------------------------------------------------------------------------- #
# graceful Ctrl+C (cancel token)
# --------------------------------------------------------------------------- #


async def test_cancelled_before_start_reports_130_shape(
    settings: Settings,
) -> None:
    world = World()
    world.resolve(ID)
    token = CancelToken()
    token.cancel()

    async with world.client() as client:
        report = await run_batch(request_for([url_for(ID)], settings),
                                 client=client, cancel=token)

    assert report.cancelled is True
    assert world.resolve_calls == []  # nothing started
    (result,) = report.results
    assert result.status == "failed"
    assert result.error is not None and result.error.code is ErrorCode.CANCELLED


async def test_cancelled_mid_download_keeps_part_and_incomplete_row(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(dl, "_CHUNK", 10)
    body = b"x" * 120
    world = World()
    world.resolve(ID)
    world.media(f"/{ID}_720.mp4", body=body)
    world.media(f"/{ID}_320.mp4", body=b"low")

    token = CancelToken()

    def on_event(event: Any) -> None:
        if event.kind.value == "download_progress":
            token.cancel()  # SIGINT lands at a chunk boundary

    async with world.client() as client:
        report = await run_batch(request_for([url_for(ID)], settings),
                                 client=client, on_event=on_event, cancel=token)

    assert report.cancelled is True
    (result,) = report.results
    assert result.status == "failed"

    parts = list(settings.storage.root.rglob("*.mp4.part"))
    assert len(parts) == 1  # .part retained for resume
    assert parts[0].stat().st_size < len(body)

    state = StateStore(settings.db_path)
    try:
        rows = state.lookup(ID)
        assert rows and rows[0].status == "incomplete"  # NOT failed
    finally:
        state.close()


# --------------------------------------------------------------------------- #
# --force: .bak collision (last bak wins) + stale .part discard
# --------------------------------------------------------------------------- #


async def test_force_moves_existing_to_bak(settings: Settings) -> None:
    world = World()
    world.resolve(ID)
    world.media(f"/{ID}_720.mp4", body=b"RUN-1")
    async with world.client() as client:
        await run_batch(request_for([url_for(ID)], settings), client=client)

        world.media(f"/{ID}_720.mp4", body=b"RUN-2")
        report = await run_batch(request_for([url_for(ID)], settings, force=True),
                                 client=client)
        (result,) = report.results
        dest = result.files[0]
        assert dest.read_bytes() == b"RUN-2"
        assert dest.with_name(dest.name + ".bak").read_bytes() == b"RUN-1"

        world.media(f"/{ID}_720.mp4", body=b"RUN-3")
        await run_batch(request_for([url_for(ID)], settings, force=True),
                        client=client)
        assert dest.read_bytes() == b"RUN-3"
        assert dest.with_name(dest.name + ".bak").read_bytes() == b"RUN-2"  # last wins


async def test_force_discards_stale_part(settings: Settings) -> None:
    world = World()
    world.resolve(ID)
    async with world.client() as client:
        report1 = await run_batch(request_for([url_for(ID)], settings),
                                  client=client)
    dest = report1.results[0].files[0]
    part = dest.with_name(dest.name + ".part")
    part.write_bytes(b"STALE-BYTES-FROM-ANOTHER-EPOCH")

    world.media(f"/{ID}_720.mp4", body=b"FRESH")
    async with world.client() as client:
        await run_batch(request_for([url_for(ID)], settings, force=True),
                        client=client)
    assert dest.read_bytes() == b"FRESH"  # stale .part never appended
    assert not part.exists()


# --------------------------------------------------------------------------- #
# LINK_EXPIRED recovery: batch policy honored, object identity decides resume
# --------------------------------------------------------------------------- #


async def test_recovery_uses_batch_policy_not_default_best(
    settings: Settings,
) -> None:
    world = World()
    world.resolve(ID)
    # policy=worst chose the 320 variant; its URL 403s once (expired signature)
    world.media(f"/{ID}_320.mp4", ("403", None), body=b"low-320")
    world.media(f"/{ID}_720.mp4", body=b"high-720")

    async with world.client() as client:
        report = await run_batch(
            request_for([url_for(ID)], settings, quality="worst"), client=client
        )

    assert report.ok == 1
    # recovery re-fetched the SAME (worst) variant — the round-1 bug would
    # have re-selected best (720) under a default policy on re-resolve
    assert world.media_gets == [f"/{ID}_320.mp4", f"/{ID}_320.mp4"]
    assert report.results[0].files[0].read_bytes() == b"low-320"


async def test_recovery_same_object_resumes_part(
    settings: Settings, tmp_path: Any
) -> None:
    body = b"A" * 100
    world = World()
    world.resolve(ID)
    world.media(f"/{ID}_720.mp4", body=body)

    # run once to learn the deterministic dest
    async with world.client() as client:
        report = await run_batch(request_for([url_for(ID)], settings), client=client)
    dest = report.results[0].files[0]
    part = dest.with_name(dest.name + ".part")

    # simulate an interrupted attempt of the SAME object: no final, 10-byte
    # .part, fresh state db (no ok rows -> no skip)
    dest.unlink()
    part.write_bytes(body[:10])
    fresh = Settings(
        storage={"root": settings.storage.root,
                 "db_path": tmp_path / "state2" / "state.db"},
        resolver={"fallback": "none"},
        engine={"delay": 0.001},
    )

    # 403 once (expired signature) -> recovery with the SAME object path must
    # RESUME the .part, not restart from zero
    world.media(f"/{ID}_720.mp4", ("403", None), body=body)
    async with world.client() as client:
        report2 = await run_batch(request_for([url_for(ID)], fresh), client=client)
    assert report2.ok == 1
    assert report2.results[0].files[0].read_bytes() == body
    assert not part.exists()  # completed + renamed


async def test_recovery_changed_rendition_restarts_clean(settings: Settings) -> None:
    world = World()
    # first resolve: rendition path A; second resolve (re-resolve): path B
    payload_a = video_payload(ID, variants=[
        {"url": f"https://video.twimg.com/{ID}_rendA.mp4",
         "bitrate": 2176000, "content_type": "video/mp4"}])
    payload_b = video_payload(ID, variants=[
        {"url": f"https://video.twimg.com/{ID}_rendB.mp4",
         "bitrate": 2176000, "content_type": "video/mp4"}])
    world.resolve(ID, payload_a, payload_b)
    world.media(f"/{ID}_rendA.mp4", ("403", None), body=b"OLD")
    world.media(f"/{ID}_rendB.mp4", body=b"NEW")

    async with world.client() as client:
        report = await run_batch(request_for([url_for(ID)], settings), client=client)

    assert report.ok == 1
    assert report.results[0].files[0].read_bytes() == b"NEW"
    # no .part left behind anywhere (clean restart on changed object)
    assert list(settings.storage.root.rglob("*.part")) == []
