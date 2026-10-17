"""SQLite state store: batches + tweets + downloads (docs/technical/05).

WAL mode, `check_same_thread=False` with single-writer discipline (all writes
funnel through the orchestrator; ops are micro-fast inline).

`synchronous=NORMAL` is safe under WAL: a power/OS crash can lose the last
transaction(s) but never corrupts the database. For this app that is free — the
orchestrator's idempotent-replay path already treats "file present, no state
row" as a normal condition and re-finalizes without re-downloading. It takes
commits from ~1.05 ms to ~0.02 ms (measured), and every write here runs on the
event-loop thread, so that is directly subtracted from batch throughput.

The completeness check is `COUNT(ok rows) == tweets.media_count` — NOT an
any-row check (audit round 2: multi-video posts whose later videos failed
must not be skipped).
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from xdl.models import DownloadRow, TweetRef, TweetRow

_SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    batch_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  TEXT    NOT NULL,
    origin      TEXT    NOT NULL,
    total_items INTEGER NOT NULL,
    status      TEXT    NOT NULL
);
CREATE TABLE IF NOT EXISTS tweets (
    tweet_id    TEXT PRIMARY KEY,
    author      TEXT,
    media_count INTEGER NOT NULL,
    first_seen  TEXT    NOT NULL
);
CREATE TABLE IF NOT EXISTS downloads (
    tweet_id      TEXT    NOT NULL,
    media_index   INTEGER NOT NULL,
    batch_id      INTEGER REFERENCES batches(batch_id),
    variant_url   TEXT,
    file_path     TEXT    NOT NULL,
    size_bytes    INTEGER,
    status        TEXT    NOT NULL,
    error         TEXT,
    source        TEXT,
    downloaded_at TEXT    NOT NULL,
    updated_at    TEXT    NOT NULL,
    PRIMARY KEY (tweet_id, media_index)
);
CREATE INDEX IF NOT EXISTS idx_status ON downloads(status);
CREATE INDEX IF NOT EXISTS idx_batch  ON downloads(batch_id);
"""


def _now() -> str:
    return datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class StateStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")  # WAL-safe; see module docstring
        self._conn.execute("PRAGMA busy_timeout=5000")  # explicit > driver default
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # ------------------------------------------------------------------ #
    # batches
    # ------------------------------------------------------------------ #

    def new_batch(self, origin: str, total_items: int) -> int:
        cur = self._conn.execute(
            "INSERT INTO batches (created_at, origin, total_items, status)"
            " VALUES (?, ?, ?, 'running')",
            (_now(), origin, total_items),
        )
        self._conn.commit()
        return int(cur.lastrowid or 0)

    def finish_batch(self, batch_id: int, status: str) -> None:
        self._conn.execute(
            "UPDATE batches SET status = ? WHERE batch_id = ?", (status, batch_id)
        )
        self._conn.commit()

    def batches(self, limit: int = 50) -> list[sqlite3.Row]:
        return list(
            self._conn.execute(
                "SELECT * FROM batches ORDER BY batch_id DESC LIMIT ?", (limit,)
            )
        )

    # ------------------------------------------------------------------ #
    # tweets
    # ------------------------------------------------------------------ #

    def upsert_tweet(self, tweet_id: str, author: str | None, media_count: int) -> None:
        self._conn.execute(
            "INSERT INTO tweets (tweet_id, author, media_count, first_seen)"
            " VALUES (?, ?, ?, ?)"
            " ON CONFLICT(tweet_id) DO UPDATE SET author = excluded.author",
            (tweet_id, author, media_count, _now()),
        )
        self._conn.commit()

    def get_tweet(self, tweet_id: str) -> TweetRow | None:
        row = self._conn.execute(
            "SELECT * FROM tweets WHERE tweet_id = ?", (tweet_id,)
        ).fetchone()
        if row is None:
            return None
        return TweetRow(
            tweet_id=row["tweet_id"],
            author=row["author"],
            media_count=row["media_count"],
            first_seen=row["first_seen"],
        )

    # ------------------------------------------------------------------ #
    # downloads
    # ------------------------------------------------------------------ #

    def lookup(self, tweet_id: str) -> list[DownloadRow]:
        rows = self._conn.execute(
            "SELECT * FROM downloads WHERE tweet_id = ? ORDER BY media_index",
            (tweet_id,),
        )
        return [_row_to_model(r) for r in rows]

    def ok_rows(self, tweet_id: str) -> list[DownloadRow]:
        return [r for r in self.lookup(tweet_id) if r.status == "ok"]

    def seen_complete(self, tweet_id: str) -> bool:
        """count(ok rows) == tweets.media_count — the round-2 complete-skip.

        One statement: the media_count anchor and the ok-count are fetched in the
        same query (a missing tweets row means no anchor -> not complete).
        """
        row = self._conn.execute(
            "SELECT t.media_count,"
            " (SELECT COUNT(*) FROM downloads d"
            "   WHERE d.tweet_id = t.tweet_id AND d.status = 'ok') AS ok_count"
            " FROM tweets t WHERE t.tweet_id = ?",
            (tweet_id,),
        ).fetchone()
        return row is not None and int(row["ok_count"]) == int(row["media_count"])

    def record(self, row: DownloadRow) -> None:
        now = _now()
        self._conn.execute(
            """
            INSERT INTO downloads (tweet_id, media_index, batch_id, variant_url,
                                   file_path, size_bytes, status, error,
                                   source, downloaded_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(tweet_id, media_index) DO UPDATE SET
                batch_id = excluded.batch_id,
                variant_url = excluded.variant_url,
                file_path = excluded.file_path,
                size_bytes = excluded.size_bytes,
                status = excluded.status,
                error = excluded.error,
                source = excluded.source,
                updated_at = excluded.updated_at
            """,
            (
                row.tweet_id,
                row.media_index,
                row.batch_id,
                row.variant_url,
                str(row.file_path),
                row.size_bytes,
                row.status,
                row.error,
                row.source,
                row.downloaded_at or now,
                now,
            ),
        )
        self._conn.commit()

    def mark_incomplete(
        self,
        tweet_id: str,
        media_index: int,
        path: Path,
        *,
        batch_id: int | None = None,
        variant_url: str | None = None,
    ) -> None:
        now = _now()
        self._conn.execute(
            """
            INSERT INTO downloads (tweet_id, media_index, batch_id, variant_url,
                                   file_path, status, downloaded_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 'incomplete', ?, ?)
            ON CONFLICT(tweet_id, media_index) DO UPDATE SET
                batch_id = excluded.batch_id,
                variant_url = excluded.variant_url,
                file_path = excluded.file_path,
                status = 'incomplete',
                updated_at = excluded.updated_at
            """,
            (tweet_id, media_index, batch_id, variant_url, str(path), now, now),
        )
        self._conn.commit()

    def mark_tweet_incomplete_if_untracked(self, tweet_id: str) -> None:
        """Circuit-breaker marker (docs/technical/08): record the tweet as
        incomplete so `xdl retry-failed` picks it up. Only inserts when the
        tweet has NO downloads rows — never clobbers an ok/partial history
        (PRIMARY KEY (tweet_id, media_index) would overwrite an ok row)."""
        cur = self._conn.execute(
            "SELECT 1 FROM downloads WHERE tweet_id = ? LIMIT 1", (tweet_id,)
        )
        if cur.fetchone() is not None:
            return
        self.record(
            DownloadRow(
                tweet_id=tweet_id,
                media_index=1,
                file_path=Path(f"{tweet_id}.unresolved"),
                status="incomplete",
                error="SKIPPED_CIRCUIT_OPEN",
            )
        )

    def failed_items(self, since: str | None = None) -> list[TweetRef]:
        """Distinct refs for rows with status IN (failed, incomplete) — powers
        `xdl retry-failed`."""
        query = (
            "SELECT DISTINCT d.tweet_id, t.author FROM downloads d"
            " LEFT JOIN tweets t ON t.tweet_id = d.tweet_id"
            " WHERE d.status IN ('failed', 'incomplete')"
        )
        params: tuple[object, ...] = ()
        if since is not None:
            query += " AND d.updated_at >= ?"
            params = (since,)
        refs = []
        for row in self._conn.execute(query, params):
            user = row["author"] or "i"
            refs.append(
                TweetRef(
                    tweet_id=row["tweet_id"],
                    screen_name=row["author"],
                    canonical_url=f"https://x.com/{user}/status/{row['tweet_id']}",
                )
            )
        return refs


def _row_to_model(row: sqlite3.Row) -> DownloadRow:
    return DownloadRow(
        tweet_id=row["tweet_id"],
        media_index=row["media_index"],
        batch_id=row["batch_id"],
        variant_url=row["variant_url"],
        file_path=Path(row["file_path"]),
        size_bytes=row["size_bytes"],
        status=row["status"],
        error=row["error"],
        source=row["source"],
        downloaded_at=row["downloaded_at"],
        updated_at=row["updated_at"],
    )
