"""Full-pipeline integration over a mock world (hermetic; docs/technical/09).

The MockTransport serves cdn.syndication.twimg.com, video.twimg.com and
t.co; run_batch is driven end-to-end with real Settings/StateStore on tmp
dirs.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx

from xdl.app import run_batch
from xdl.config import Settings
from xdl.models import BatchRequest
from xdl.storage.state import StateStore

VIDEO_ID = "1750111111111111111"
PHOTO_ID = "1750222222222222222"
MULTI_ID = "1750333333333333333"
GONE_ID = "1750444444444444444"

MP4_A = b"\x00\x01\x02VIDEO-A" * 50
MP4_B = b"\x03\x04VIDEO-B" * 40
MP4_C = b"\x05VIDEO-C" * 30

# {date} is local-tz by spec — compute expectations with the same conversion.
DATE = datetime(2026, 9, 30, 21, 14, 3, tzinfo=UTC).astimezone().strftime("%Y-%m-%d")


def _video_tweet(tweet_id: str, user: str = "someuser") -> dict[str, Any]:
    return {
        "__typename": "Tweet",
        "id_str": tweet_id,
        "text": f"video post {tweet_id}",
        "created_at": "2026-09-30T21:14:03.000Z",
        "user": {"screen_name": user, "name": user.title()},
        "mediaDetails": [
            {
                "type": "video",
                "media_url_https": f"https://pbs.twimg.com/thumb_{tweet_id}.jpg",
                "original_info": {"width": 1280, "height": 720},
                "video_info": {
                    "variants": [
                        {"url": f"https://video.twimg.com/{tweet_id}_320.mp4",
                         "bitrate": 320000, "content_type": "video/mp4"},
                        {"url": f"https://video.twimg.com/{tweet_id}_720.mp4",
                         "bitrate": 2176000, "content_type": "video/mp4"},
                    ]
                },
            }
        ],
    }


def _multi_video_tweet() -> dict[str, Any]:
    def media(i: int) -> dict[str, Any]:
        return {
            "type": "video",
            "media_url_https": f"https://pbs.twimg.com/thumb_{i}.jpg",
            "original_info": {"width": 1280, "height": 720},
            "video_info": {
                "variants": [
                    {"url": f"https://video.twimg.com/{MULTI_ID}_{i}.mp4",
                     "bitrate": 1000000 + i, "content_type": "video/mp4"},
                ]
            },
        }
    payload = _video_tweet(MULTI_ID, "multiuser")
    payload["mediaDetails"] = [media(1), media(2), media(3)]
    return payload


def _photo_tweet() -> dict[str, Any]:
    payload = _video_tweet(PHOTO_ID, "photouser")
    payload["mediaDetails"] = [
        {"type": "photo",
         "media_url_https": "https://pbs.twimg.com/photo_1.jpg",
         "sizes": {"large": {"w": 900, "h": 1600}}}
    ]
    return payload


def make_world(
    *,
    video: bool = True,
    photo: bool = True,
    multi: bool = True,
    tombstone: bool = True,
    tco: bool = False,
    fail_media: set[str] | None = None,
    expire_media_once: set[str] | None = None,
) -> tuple[httpx.AsyncClient, dict[str, Any]]:
    """Build a MockTransport world + a call-log dict."""
    log: dict[str, Any] = {"media_gets": [], "resolve_calls": 0, "tco_hops": 0}
    fail_media = fail_media or set()
    expire_media_once = expire_media_once or set()
    expired: set[str] = set()

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if host == "t.co":
            log["tco_hops"] += 1
            return httpx.Response(301, headers={"location": f"https://x.com/someuser/status/{VIDEO_ID}"})
        if host == "cdn.syndication.twimg.com":
            log["resolve_calls"] += 1
            tweet_id = request.url.params["id"]
            if tweet_id == VIDEO_ID and video:
                return httpx.Response(200, json=_video_tweet(VIDEO_ID))
            if tweet_id == PHOTO_ID and photo:
                return httpx.Response(200, json=_photo_tweet())
            if tweet_id == MULTI_ID and multi:
                return httpx.Response(200, json=_multi_video_tweet())
            if tweet_id == GONE_ID and tombstone:
                return httpx.Response(200, json={"__typename": "TweetTombstone"})
            return httpx.Response(404)
        if host == "video.twimg.com":
            url = str(request.url)
            log["media_gets"].append(url)
            name = url.rsplit("/", 1)[1]
            if name in fail_media:
                return httpx.Response(404)
            if name in expire_media_once and name not in expired:
                expired.add(name)
                return httpx.Response(403)  # signed URL expired mid-flight
            bodies = {
                f"{VIDEO_ID}_720.mp4": MP4_A,
                f"{VIDEO_ID}_320.mp4": b"low",
                f"{MULTI_ID}_1.mp4": MP4_A,
                f"{MULTI_ID}_2.mp4": MP4_B,
                f"{MULTI_ID}_3.mp4": MP4_C,
            }
            body = bodies.get(name, b"generic")
            return httpx.Response(200, content=body,
                                  headers={"content-length": str(len(body))})
        return httpx.Response(404)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), log


def request_for(urls: list[str], settings: Settings, **kw: Any) -> BatchRequest:
    return BatchRequest(inputs=urls, settings=settings, **kw)


EXPECTED_VIDEO_PATH_PARTS = ("x", "someuser", f"{DATE}_1750111111111111111.mp4")


async def test_single_url_happy_path(settings: Settings) -> None:
    client, _log = make_world()
    async with client:
        report = await run_batch(
            request_for([f"https://x.com/someuser/status/{VIDEO_ID}"], settings),
            client=client,
        )

    assert report.exit_code == 0
    assert report.ok == 1
    (result,) = report.results
    assert result.status == "ok"
    dest = result.files[0]
    assert dest.parts[-3:] == EXPECTED_VIDEO_PATH_PARTS
    assert dest.read_bytes() == MP4_A  # best bitrate (720p) chosen
    assert dest.with_name(dest.name + ".meta.json").is_file()
    # state row + sidecar written
    state = StateStore(settings.db_path)
    try:
        rows = state.lookup(VIDEO_ID)
        assert len(rows) == 1 and rows[0].status == "ok"
        assert rows[0].size_bytes == len(MP4_A)
        assert state.batches()[0]["origin"] == "cli"
    finally:
        state.close()


async def test_second_run_skips_without_network(settings: Settings) -> None:
    client, _ = make_world()
    async with client:
        await run_batch(
            request_for([f"https://x.com/someuser/status/{VIDEO_ID}"], settings),
            client=client,
        )
    # second run with syndication DOWN: skip check fires before any resolve
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("network is gone")

    down = httpx.AsyncClient(transport=httpx.MockTransport(refuse))
    async with down:
        report = await run_batch(
            request_for([f"https://x.com/someuser/status/{VIDEO_ID}"], settings),
            client=down,
        )
    assert report.exit_code == 0
    assert report.skipped == 1
    assert "already downloaded" in report.results[0].detail


async def test_photo_only_fails_again_on_rerun(settings: Settings) -> None:
    """Round-2 B-lane filing: a NO_VIDEO post must never false-complete.

    A media_count=0 tweets row made seen_complete() report 0 == 0, turning
    every re-run of a photo-only post into 'skipped/complete'."""
    client, _ = make_world()
    async with client:
        for report in (
            await run_batch(
                request_for([f"https://x.com/photouser/status/{PHOTO_ID}"], settings),
                client=client,
            ),
            await run_batch(
                request_for([f"https://x.com/photouser/status/{PHOTO_ID}"], settings),
                client=client,
            ),
        ):
            assert report.exit_code == 2  # single-item batch, all failed
            assert report.results[0].status == "failed"
            assert "NO_VIDEO" in report.results[0].detail
    state = StateStore(settings.db_path)
    try:
        assert state.get_tweet(PHOTO_ID) is None  # no nothing-downloadable anchor
    finally:
        state.close()


async def test_mixed_batch_summary_and_exit_codes(settings: Settings) -> None:
    client, _ = make_world()
    urls = [
        f"https://x.com/someuser/status/{VIDEO_ID}",      # ok
        f"https://x.com/photouser/status/{PHOTO_ID}",     # NO_VIDEO
        f"https://x.com/i/status/{GONE_ID}",              # NOT_EMBEDDABLE -> failed
        "https://instagram.com/p/xyz",                    # URL_UNRECOGNIZED
    ]
    async with client:
        report = await run_batch(request_for(urls, settings), client=client)
    assert report.exit_code == 1  # mixed: >=1 ok, >=1 failed
    statuses = [r.status for r in report.results]
    assert statuses == ["ok", "failed", "failed", "failed"]
    assert "NO_VIDEO" in report.results[1].detail
    assert "NOT_EMBEDDABLE" in report.results[2].detail
    assert "URL_UNRECOGNIZED" in report.results[3].detail


async def test_all_failed_exit_code_2(settings: Settings) -> None:
    client, _ = make_world(video=False)
    async with client:
        report = await run_batch(
            request_for(
                [f"https://x.com/someuser/status/{VIDEO_ID}", "garbage input"],
                settings,
            ),
            client=client,
        )
    assert report.exit_code == 2
    assert report.failed == 2


async def test_duplicate_inputs_deduped(settings: Settings) -> None:
    client, log = make_world()
    async with client:
        report = await run_batch(
            request_for(
                [
                    f"https://x.com/someuser/status/{VIDEO_ID}",
                    f"https://twitter.com/someuser/status/{VIDEO_ID}",
                    f"https://fxtwitter.com/someuser/status/{VIDEO_ID}",
                ],
                settings,
            ),
            client=client,
        )
    assert report.exit_code == 0
    assert report.ok == 1 and report.skipped == 2
    assert log["resolve_calls"] == 1  # one resolve per tweet_id
    for r in report.results[1:]:
        assert "SKIPPED_DUP_INPUT" in r.detail


async def test_tco_input_expanded(settings: Settings) -> None:
    client, log = make_world(tco=True)
    async with client:
        report = await run_batch(request_for(["https://t.co/AbCdEf123"], settings),
                                 client=client)
    assert log["tco_hops"] == 1
    assert report.ok == 1


async def test_dry_run_touches_nothing(settings: Settings) -> None:
    client, _ = make_world()
    async with client:
        report = await run_batch(
            request_for([f"https://x.com/someuser/status/{VIDEO_ID}"], settings,
                        dry_run=True),
            client=client,
        )
    assert report.exit_code == 0
    assert not any(settings.storage.root.rglob("*"))  # zero bytes written
    state = StateStore(settings.db_path)
    try:
        rows = state.lookup(VIDEO_ID)
        assert rows == [] or all(r.status != "ok" for r in rows)
    finally:
        state.close()


async def test_multi_video_partial_completion_regression(settings: Settings) -> None:
    """The audit-round-2 regression: a 3-video post with 1 file already ok
    downloads exactly the missing 2 — never skips, never re-downloads #1."""
    # Run 1: media 2 and 3 fail (CDN 404) -> partial
    client, _ = make_world(fail_media={f"{MULTI_ID}_2.mp4", f"{MULTI_ID}_3.mp4"})
    async with client:
        report1 = await run_batch(
            request_for([f"https://x.com/multiuser/status/{MULTI_ID}"], settings),
            client=client,
        )
    assert report1.exit_code == 1
    assert report1.results[0].status == "partial"
    assert report1.results[0].detail.startswith("1/3 media ok")
    files1 = {p.name for p in report1.results[0].files}
    assert files1 == {f"{DATE}_{MULTI_ID}.mp4"}

    # Run 2: CDN healthy -> exactly the missing 2 download, #1 untouched
    client2, log2 = make_world()
    async with client2:
        report2 = await run_batch(
            request_for([f"https://x.com/multiuser/status/{MULTI_ID}"], settings),
            client=client2,
        )
    assert report2.results[0].status == "ok"
    fetched = {u.rsplit("/", 1)[1] for u in log2["media_gets"]}
    assert fetched == {f"{MULTI_ID}_2.mp4", f"{MULTI_ID}_3.mp4"}  # NOT #1
    # ItemResult.files holds ok outcomes only; the skipped #1 exists on disk
    dests = {p.name for p in settings.storage.root.rglob("*.mp4")}
    assert dests == {
        f"{DATE}_{MULTI_ID}.mp4",
        f"{DATE}_{MULTI_ID}_v2.mp4",
        f"{DATE}_{MULTI_ID}_v3.mp4",
    }
    # one sidecar per media file
    sidecars = list(settings.storage.root.rglob("*.meta.json"))
    assert len(sidecars) == 3

    # Run 3: everything recorded -> plain skip
    client3, _ = make_world()
    async with client3:
        report3 = await run_batch(
            request_for([f"https://x.com/multiuser/status/{MULTI_ID}"], settings),
            client=client3,
        )
    assert report3.results[0].status == "skipped"
    assert "3 media" in report3.results[0].detail


async def test_link_expired_recovers_via_re_resolve(settings: Settings) -> None:
    """Media-URL 403 (LINK_EXPIRED) -> orchestrator re-resolves once, item ok."""
    client, log = make_world(
        expire_media_once={f"{VIDEO_ID}_720.mp4"},
    )
    async with client:
        report = await run_batch(
            request_for([f"https://x.com/someuser/status/{VIDEO_ID}"], settings),
            client=client,
        )
    assert report.exit_code == 0 and report.ok == 1
    assert log["resolve_calls"] == 2  # original + recovery re-resolve
    assert report.results[0].files[0].read_bytes() == MP4_A


async def test_force_redownloads(settings: Settings) -> None:
    client, _ = make_world()
    async with client:
        await run_batch(
            request_for([f"https://x.com/someuser/status/{VIDEO_ID}"], settings),
            client=client,
        )
        report = await run_batch(
            request_for([f"https://x.com/someuser/status/{VIDEO_ID}"], settings,
                        force=True),
            client=client,
        )
    assert report.ok == 1 and report.skipped == 0


async def test_retry_failed_picks_up_failures(settings: Settings) -> None:
    # Run 1: video 404s -> item failed, recorded in state
    client, _ = make_world(fail_media={f"{VIDEO_ID}_720.mp4", f"{VIDEO_ID}_320.mp4"})
    async with client:
        report1 = await run_batch(
            request_for([f"https://x.com/someuser/status/{VIDEO_ID}"], settings),
            client=client,
        )
    assert report1.failed == 1

    state = StateStore(settings.db_path)
    try:
        refs = state.failed_items()
        assert [r.tweet_id for r in refs] == [VIDEO_ID]
        retry_urls = [r.canonical_url for r in refs]
    finally:
        state.close()

    client2, _ = make_world()
    async with client2:
        report2 = await run_batch(
            BatchRequest(inputs=retry_urls, settings=settings, origin="retry"),
            client=client2,
        )
    assert report2.ok == 1 and report2.exit_code == 0


async def test_no_meta_skips_sidecars(settings: Settings) -> None:
    client, _ = make_world()
    async with client:
        report = await run_batch(
            request_for([f"https://x.com/someuser/status/{VIDEO_ID}"], settings,
                        no_meta=True),
            client=client,
        )
    assert report.ok == 1
    assert list(settings.storage.root.rglob("*.meta.json")) == []
    assert report.results[0].files[0].is_file()  # media itself landed


async def test_progress_events_stream(settings: Settings) -> None:
    events: list[tuple[str, str | None, int | None]] = []
    from xdl.report.events import EventKind

    def on_event(event: object) -> None:
        events.append((event.kind.value, event.tweet_id, event.media_index))

    client, _ = make_world()
    async with client:
        await run_batch(
            request_for([f"https://x.com/someuser/status/{VIDEO_ID}"], settings),
            client=client,
            on_event=on_event,
        )
    kinds = [e[0] for e in events]
    for expected in (
        EventKind.batch_start.value,
        EventKind.resolve_start.value,
        EventKind.resolve_done.value,
        EventKind.download_start.value,
        EventKind.download_progress.value,
        EventKind.download_done.value,
        EventKind.item_done.value,
        EventKind.batch_done.value,
    ):
        assert expected in kinds, f"missing {expected} event"
