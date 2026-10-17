"""State store: three-table schema, seen_complete semantics, retry-failed
(docs/technical/05)."""

from __future__ import annotations

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


def test_wal_mode(state: StateStore, tmp_path: Path) -> None:
    mode = state._conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert str(mode).lower() == "wal"


# --------------------------------------------------------------------------- #
# circuit-breaker marker (docs/technical/08)
# --------------------------------------------------------------------------- #


def test_mark_tweet_incomplete_if_untracked_inserts(state: StateStore) -> None:
    state.mark_tweet_incomplete_if_untracked("99001")
    rows = state.lookup("99001")
    assert len(rows) == 1
    assert rows[0].status == "incomplete"
    assert rows[0].error == "SKIPPED_CIRCUIT_OPEN"
    # retry-failed surfaces it
    assert [r.tweet_id for r in state.failed_items()] == ["99001"]


def test_marker_never_clobbers_existing_history(state: StateStore) -> None:
    from xdl.models import DownloadRow

    state.record(DownloadRow(tweet_id="99002", media_index=1,
                             file_path=Path("a.mp4"), status="ok"))
    state.mark_tweet_incomplete_if_untracked("99002")
    (row,) = state.lookup("99002")
    assert row.status == "ok"  # the ok row survived untouched
