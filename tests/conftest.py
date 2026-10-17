"""Shared fixtures: hermetic Settings, tmp dirs, httpx MockTransport helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from xdl.config import Settings


@pytest.fixture
def tmp_root(tmp_path: Path) -> Path:
    return tmp_path / "downloads"


@pytest.fixture
def settings(tmp_root: Path, tmp_path: Path) -> Settings:
    """Hermetic settings: everything under pytest's tmp dir, no APPDATA use.

    - fallback=none keeps the chain syndication-only: a syndication 404/
      tombstone must NOT fall through to the REAL yt-dlp adapter (bundled
      dependency, would hit the network in tests).
    - delay=0.001 replaces the jittered 0.2-0.5s resolve pacing with a fixed
      1ms floor so gated requests don't slow the suite down.
    """
    return Settings(
        storage={
            "root": tmp_root,
            "layout": "by-author",
            "filename_template": "{date}_{id}",
            "db_path": tmp_path / "state" / "state.db",
        },
        resolver={"fallback": "none"},
        engine={"delay": 0.001},
    )
