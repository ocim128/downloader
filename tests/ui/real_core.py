"""Real-core test support for the web UI (Agent B round 2).

Builds the REAL pipeline (`xdl.app.run_batch`, `xdl.urls.normalize`,
`xdl.resolve.base.build_chain`) behind the UI's injected callables, with every
remote call served by an `httpx.MockTransport` world that answers straight from
Agent B's recorded fixtures (`tests/fixtures/syndication/*.json`). Fully
hermetic: no network, no subprocesses, no live ffmpeg.

Keep every part of this module on injected callables — the UI layer under test
(`xdl.ui.app`) must never import the core directly.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from xdl.app import analyze as core_analyze
from xdl.app import run_batch as core_run_batch
from xdl.models import BatchReport
from xdl.report.events import ProgressEvent
from xdl.storage.state import StateStore
from xdl.ui.app import to_card

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "syndication"

#: fixture file -> recorded tweet id (kept explicit so the world is auditable)
FIXTURES: tuple[tuple[str, str], ...] = (
    ("video_chibicode_13s.json", "2040511285998313827"),
    ("video_f1_8s.json", "2110047410852680127"),
    ("photo_single_cwsports.json", "2106066240427147458"),
)

VIDEO_ID = FIXTURES[0][1]
F1_ID = FIXTURES[1][1]
PHOTO_ID = FIXTURES[2][1]
UNKNOWN_ID = "1750444444444444000"  # served the 404 dog page by the world


def _fixture_payload(name: str) -> dict[str, Any]:
    envelope = json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))
    return envelope["response"]  # type: ignore[no-any-return]


class RealCoreRunner:
    """The documented UI contract wired to the REAL orchestrator.

    `run_batch(req, *, on_event) -> BatchReport` delegates to
    `xdl.app.run_batch(req, *, client, on_event)` with the mock world's client
    injected (Agent A's round-1 note documents the kwarg). `analyze` is the
    real Flow-B composition (normalize + fallback chain) from `python -m
    xdl.ui`, minus the network. `item_files` reads the real StateStore.
    """

    def __init__(self, settings: Any) -> None:
        self.settings = settings
        self.syndication_hits: list[str] = []
        self.media_hits: list[str] = []
        self.client = _mock_world(self.syndication_hits, self.media_hits)
        self.runs = 0

    async def run_batch(
        self, req: Any, *, on_event: Callable[[ProgressEvent], None] | None = None
    ) -> BatchReport:
        self.runs += 1
        return await core_run_batch(req, client=self.client, on_event=on_event)

    async def analyze(self, raw_urls: list[str]) -> list[dict[str, Any]]:
        """The real Flow-B path: `xdl.app.analyze` + the single `to_card`
        shaper — same code `python -m xdl.ui` runs, minus the network."""
        results = await core_analyze(raw_urls, self.settings, client=self.client)
        return [to_card(raw, r) for raw, r in zip(raw_urls, results, strict=True)]

    async def item_files(self, tweet_id: str) -> list[str]:
        def _sync() -> list[str]:
            store = StateStore(self.settings.db_path)
            try:
                return [str(r.file_path) for r in store.ok_rows(tweet_id)]
            finally:
                store.close()

        return list(await asyncio.to_thread(_sync))

    async def aclose(self) -> None:
        await self.client.aclose()


def _mock_world(
    syndication_hits: list[str], media_hits: list[str]
) -> httpx.AsyncClient:
    """MockTransport world built from recorded fixtures.

    - `cdn.syndication.twimg.com/tweet-result?id=<id>` -> recorded payload (or
      a 404 dog page for unknown ids, mirroring the live shape).
    - every recorded mp4 variant URL -> deterministic unique bytes.
    """
    payloads: dict[str, Any] = {}
    bodies: dict[tuple[str, str], bytes] = {}
    for name, tid in FIXTURES:
        payload = _fixture_payload(name)
        payloads[tid] = payload
        for detail in payload.get("mediaDetails", []):
            for variant in (detail.get("video_info") or {}).get("variants", []) or []:
                if variant.get("content_type") != "video/mp4":
                    continue
                url: str = variant["url"]
                key = _url_path(url)
                filename = url.rsplit("/", 1)[-1].split("?")[0]
                bodies[key] = f"media:{filename}".encode()

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host or ""
        if host == "cdn.syndication.twimg.com":
            tid = request.url.params.get("id", "")
            syndication_hits.append(tid)
            payload = payloads.get(tid)
            if payload is None:
                return httpx.Response(404, text="<html class='dog'>gone</html>")
            return httpx.Response(200, json=payload)
        if host.endswith(".twimg.com"):
            media_hits.append(str(request.url))
            body = bodies.get((host, request.url.path))
            if body is None:
                return httpx.Response(404)
            return httpx.Response(
                200, content=body, headers={"content-length": str(len(body))}
            )
        return httpx.Response(404)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _url_path(url: str) -> tuple[str, str]:
    import urllib.parse

    parts = urllib.parse.urlsplit(url)
    return (parts.hostname or "", parts.path)
