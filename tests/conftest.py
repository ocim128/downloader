"""Shared fixtures: hermetic Settings, tmp dirs, httpx MockTransport helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from xdl.config import Settings


@pytest.fixture
def tmp_root(tmp_path: Path) -> Path:
    return tmp_path / "downloads"


@pytest.fixture
def settings(tmp_root: Path) -> Settings:
    """Hermetic settings: everything under pytest's tmp dir, no APPDATA use."""
    return Settings(
        storage={
            "root": tmp_root,
            "layout": "by-author",
            "filename_template": "{date}_{id}",
        }
    )
