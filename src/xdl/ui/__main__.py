"""`python -m xdl.ui` — the `xdl serve` runner (docs/technical/10).

Frontend wiring only: loads Settings, imports the real orchestrator from
`xdl.app` (frontend -> app is layering-legal), injects `run_batch`/`analyze`/
history/reveal callables, then uvicorn. `xdl serve` (cli.py, Agent A) will
alias this entry; it stays runnable standalone so the UI is testable against
both the stub and the real pipeline.

SECURITY: hard-fails unless the bind is loopback (private tool by design).
"""

from __future__ import annotations

import argparse
import asyncio
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import uvicorn

from xdl.config import Settings

_PORT_DEFAULT = 7788


def list_state_batches(db_path: Path, limit: int = 50) -> list[dict[str, Any]]:
    """State-store batch rows in the UI hub's job shape (history table + JS).

    Per-status counts are not reconstructable from state (skipped items leave
    no rows), so `counts` is omitted — template/JS degrade to totals."""
    from xdl.storage.state import StateStore

    store = StateStore(db_path)
    try:
        rows: list[dict[str, Any]] = []
        for r in store.batches(limit):
            try:
                started = datetime.fromisoformat(r["created_at"]).timestamp()
            except ValueError:
                started = 0.0
            rows.append({
                "job_id": r["batch_id"],
                "status": r["status"],
                "started_at": started,
                "inputs": r["total_items"],
                "total": r["total_items"],
                "origin": r["origin"],
            })
        return rows
    finally:
        store.close()


def build(port: int) -> Any:
    """Create the app with REAL core wiring (no stubs)."""
    import xdl.app as core
    from xdl.models import TweetRef
    from xdl.resolve.base import build_chain
    from xdl.storage.state import StateStore
    from xdl.ui.app import create_app
    from xdl.urls import normalize

    settings = Settings.load()

    async def run_batch(req: Any, *, on_event: Any = None) -> Any:
        return await core.run_batch(req, on_event=on_event)

    async def analyze(raw_urls: list[str]) -> list[dict[str, Any]]:
        """Flow B cards: resolve-only, no downloads (docs technical/10).

        # TODO(merge): move behind an xdl.app-exposed helper when Agent A adds
        # one; composing normalize+chain here is interim frontend wiring,
        # flagged in COORDINATION.md.
        """
        cards: list[dict[str, Any]] = []
        async with httpx.AsyncClient(
            http2=False, timeout=httpx.Timeout(connect=10, read=60, write=30, pool=15)
        ) as client:
            chain = build_chain(settings, client)
            for line in raw_urls:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                try:
                    ref: TweetRef = await normalize(client, line)
                    tweet = await chain.resolve(ref)
                except Exception as err:
                    cards.append({"input": line, "error": f"{type(err).__name__}: {err}"})
                    continue
                videos = [m for m in tweet.media if m.kind in ("video", "gif")]
                cards.append(
                    {
                        "input": line,
                        "tweet_id": tweet.ref.tweet_id,
                        "author": tweet.author.screen_name,
                        "text": tweet.text[:120],
                        "poster": next(
                            (m.poster_url or m.url_best for m in tweet.media), None
                        ),
                        "canonical_url": tweet.ref.canonical_url,
                        "media_kinds": sorted({m.kind for m in tweet.media}),
                        "available_bitrates": sorted(
                            {v.bitrate for m in videos for v in m.variants if v.bitrate},
                            reverse=True,
                        ),
                        # policy labels: honest caps fed into BatchRequest.quality
                        "formats": ["Best MP4", "MP3"],
                    }
                )
        return cards

    def _list_batches_sync(limit: int) -> list[dict[str, Any]]:
        return list_state_batches(settings.db_path, limit)

    async def list_batches(limit: int) -> list[dict[str, Any]]:
        return list(await asyncio.to_thread(_list_batches_sync, limit))

    def _item_files_sync(tweet_id: str) -> list[str]:
        store = StateStore(settings.db_path)
        try:
            return [str(r.file_path) for r in store.ok_rows(tweet_id)]
        finally:
            store.close()

    async def item_files(tweet_id: str) -> list[str]:
        return list(await asyncio.to_thread(_item_files_sync, tweet_id))

    app = create_app(
        settings,
        run_batch,
        analyze=analyze,
        list_batches=list_batches,
        item_files=item_files,
    )

    @app.on_event("startup")
    async def _open_tab() -> None:
        await asyncio.to_thread(webbrowser.open, f"http://127.0.0.1:{port}/")

    return app


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="xdl serve",
        description="local-download UI on loopback (docs/technical/10)",
    )
    parser.add_argument("--port", type=int, default=_PORT_DEFAULT)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--no-open", action="store_true",
                        help="do not open a browser tab")
    args = parser.parse_args()

    if args.host not in ("127.0.0.1", "localhost"):
        parser.error(
            "refusing to bind a non-loopback host: this UI has no auth by "
            "design; remote access is an explicit re-decision (docs/technical/10)"
        )
    app = build(port=args.port)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
