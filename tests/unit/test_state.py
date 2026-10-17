"""State store: three-table schema, seen_complete semantics, retry-failed,
rebuild_from_disk (docs/technical/05)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from xdl.models import DownloadRow
from xdl.storage.state import StateStore


@pytest.fixture
def state(tmp_path: Path) -> StateStore:
    store = StateStore(tmp_path / "state.db")
    yield store
    store.close()


def _row(tweet_id: str, index: int, status: str = "ok", path: Path | None = None) -> DownloadRow:
    return DownloadRow(
        tweet_id=tweet_id,
        media_index=index,
        file_path=path or Path(f"/downloads/{tweet_id}_{index}.mp4"),
        status=status,  # type: ignore[arg-type]
        size_bytes=1234,
        source="syndication",
        downloaded_at="2026-10-17T00:00:00Z",
        updated_at="2026-10-17T00:00:00Z",
    )


def test_schema_tables_exist(state: StateStore, tmp_path: Path) -> None:
    tables = {
        r[0]
        for r in state._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert {"batches", "tweets", "downloads"} <= tables


def test_batch_lifecycle(state: StateStore) -> None:
    batch_id = state.new_batch("cli", total_items=5)
    assert batch_id >= 1
    state.finish_batch(batch_id, "partial")
    rows = state.batches()
    assert rows[0]["status"] == "partial"
    assert rows[0]["origin"] == "cli"
    assert rows[0]["total_items"] == 5


def test_seen_complete_counts_not_any_row(state: StateStore) -> None:
    # the round-2 regression: 3-video post with 1 ok row must NOT be complete
    state.upsert_tweet("1750", "someuser", media_count=3)
    state.record(_row("1750", 1))
    assert not state.seen_complete("1750")
    state.record(_row("1750", 2))
    assert not state.seen_complete("1750")
    state.record(_row("1750", 3))
    assert state.seen_complete("1750")


def test_seen_complete_unknown_tweet(state: StateStore) -> None:
    state.record(_row("9999", 1))
    assert not state.seen_complete("9999")  # no tweets row -> no media_count anchor


def test_seen_complete_ignores_incomplete_rows(state: StateStore) -> None:
    state.upsert_tweet("1750", "u", media_count=2)
    state.record(_row("1750", 1))
    state.record(_row("1750", 2, status="incomplete"))
    assert not state.seen_complete("1750")


def test_record_upserts_on_pk(state: StateStore) -> None:
    state.record(_row("1750", 1, status="incomplete"))
    state.record(_row("1750", 1, status="ok", path=Path("/d/x.mp4")))
    rows = state.lookup("1750")
    assert len(rows) == 1 and rows[0].status == "ok" and rows[0].file_path == Path("/d/x.mp4")


def test_mark_incomplete_then_retry_cycle(state: StateStore) -> None:
    state.upsert_tweet("1750", "u", 2)
    state.mark_incomplete("1750", 1, Path("/d/1.mp4"))
    rows = state.lookup("1750")
    assert rows[0].status == "incomplete"
    refs = state.failed_items()
    assert len(refs) == 1 and refs[0].tweet_id == "1750"
    assert refs[0].canonical_url == "https://x.com/u/status/1750"
    # after ok, no failed items remain
    state.record(_row("1750", 1, path=Path("/d/1.mp4")))
    assert state.failed_items() == []


def test_failed_items_distinct(state: StateStore) -> None:
    state.upsert_tweet("a1", "u1", 2)
    state.record(_row("a1", 1, status="failed"))
    state.record(_row("a1", 2, status="incomplete"))
    assert len(state.failed_items()) == 1


def test_get_tweet(state: StateStore) -> None:
    state.upsert_tweet("1750", "someuser", 4)
    row = state.get_tweet("1750")
    assert row is not None
    assert row.author == "someuser" and row.media_count == 4
    assert state.get_tweet("nope") is None


def test_rebuild_from_disk_round_trip(state: StateStore, tmp_path: Path) -> None:
    from xdl.models import Author, Chosen, MediaAsset, ResolvedTweet, TweetRef, Variant
    from xdl.storage.naming import render_path
    from xdl.storage.sidecar import write_sidecar

    tweet = ResolvedTweet(
        ref=TweetRef(tweet_id="1750", screen_name="someuser",
                     canonical_url="https://x.com/someuser/status/1750"),
        text="hello",
        created_at=datetime(2026, 9, 30, 21, 14, tzinfo=UTC),
        author=Author(screen_name="someuser", display_name="Some User"),
        media=[MediaAsset(kind="video", index=1, poster_url="https://p/1.jpg")],
        source="syndication",
    )
    chosen = Chosen(
        asset=tweet.media[0],
        variant=Variant(url="https://video.twimg.com/1.mp4", bitrate=1000,
                        width=1280, height=720),
    )
    media_file = render_path(tmp_path, tweet, 1)
    media_file.parent.mkdir(parents=True, exist_ok=True)
    media_file.write_bytes(b"videobytes")
    import asyncio

    from xdl.storage.sidecar import build_payload

    payload = build_payload(tweet, chosen, media_file, size_bytes=9)
    asyncio.run(write_sidecar(media_file, payload))

    count = state.rebuild_from_disk(tmp_path)
    assert count == 1
    assert state.seen_complete("1750")
    (row,) = state.lookup("1750")
    assert row.file_path == media_file
    assert row.status == "ok" and row.size_bytes == 9


def test_wal_mode(state: StateStore, tmp_path: Path) -> None:
    mode = state._conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert str(mode).lower() == "wal"
