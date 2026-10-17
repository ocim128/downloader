"""M0 smoke tests: package imports, version, settings defaults."""

from __future__ import annotations

import httpx
import pytest

import xdl
from xdl.config import Settings
from xdl.models import BatchReport, ErrorCode, ItemResult, TweetRef, XdlError


def test_version() -> None:
    assert xdl.__version__ == "0.1.0"


def test_error_taxonomy_shapes() -> None:
    err = XdlError(ErrorCode.NO_VIDEO, "photos only")
    assert err.code is ErrorCode.NO_VIDEO
    assert not err.retryable
    assert XdlError(ErrorCode.NETWORK, "down").retryable


def test_item_result_defaults() -> None:
    ref = TweetRef(tweet_id="20", screen_name="jack", canonical_url="https://x.com/jack/status/20")
    result = ItemResult(ref=ref, status="ok", detail="done")
    assert result.media == [] and result.files == []
    report = BatchReport(results=[result])
    assert report.ok == 1 and report.exit_code == 0


def test_settings_defaults() -> None:
    s = Settings()
    assert s.storage.layout == "by-author"
    assert s.engine.concurrency == 3
    assert s.resolver.fallback == "auto"


def test_settings_env_and_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDL_ENGINE__RETRIES", "9")
    monkeypatch.setenv("XDL_OUTPUT", "D:/Media/x")
    s = Settings.load({"storage.layout": "flat"})
    assert s.engine.retries == 9
    assert str(s.storage.root) == "D:\\Media\\x" or s.storage.root.as_posix() == "D:/Media/x"
    assert s.storage.layout == "flat"


def test_mock_transport_available() -> None:
    # sanity: the hermetic-test workhorse is wired
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert client.get("https://example.test/").json() == {"ok": True}
