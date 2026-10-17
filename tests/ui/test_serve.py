"""Web UI tests (docs/technical/10): TestClient + StubOrchestrator, hermetic.

No network (all remote calls happen inside the stub), no subprocesses beyond
TestClient's portal, no sqlite (stub keeps history in memory).
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.ui.stub_orchestrator import StubOrchestrator
from xdl.config import LogCfg, Settings, StorageCfg
from xdl.ui import app as ui_app
from xdl.ui.app import create_app


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        storage=StorageCfg(root=tmp_path / "downloads", db_path=tmp_path / "state.db"),
        log=LogCfg(file=False),
    )


@pytest.fixture()
def stub() -> StubOrchestrator:
    return StubOrchestrator()


@pytest.fixture()
def client(tmp_path: Path, stub: StubOrchestrator) -> Iterator[TestClient]:
    app = create_app(
        _settings(tmp_path),
        stub.run_batch,
        analyze=stub.analyze,
        list_batches=stub.list_batches,
        item_files=stub.item_files,
    )
    with TestClient(app, base_url="http://127.0.0.1:7788") as test:
        yield test


def _wait_done(client: TestClient, job_id: str, timeout: float = 8.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        detail = client.get(f"/jobs/{job_id}").json()
        if not detail.get("in_flight") and detail.get("status") != "running":
            return detail
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not finish")


# --------------------------------------------------------------------------- #
# security posture
# --------------------------------------------------------------------------- #

def test_index_has_csp_and_pastebox(client: TestClient) -> None:
    resp = client.get("/")
    assert resp.status_code == 200
    assert "default-src 'self'" in resp.headers["content-security-policy"]
    body = resp.text
    assert 'id="urls"' in body and 'id="download"' in body
    assert resp.headers["x-content-type-options"] == "nosniff"


def test_non_loopback_host_is_403(client: TestClient) -> None:
    resp = client.get("/", headers={"Host": "evil.example.com"})
    assert resp.status_code == 403


# --------------------------------------------------------------------------- #
# Flow A
# --------------------------------------------------------------------------- #

def test_post_jobs_parses_dedups_and_runs(client: TestClient) -> None:
    payload = (
        "https://x.com/u/status/101\n"
        "# a comment line\n"
        "\n"
        "https://x.com/u/status/100\n"
        "https://x.com/u/status/101\n"  # duplicate
    )
    resp = client.post("/jobs", json={"urls": payload})
    assert resp.status_code == 202
    body = resp.json()
    assert body["job_id"] == "1"
    assert body["accepted"] == 2
    assert body["duplicates"] == 1

    detail = _wait_done(client, "1")
    assert detail["status"] == "ok"
    assert detail["counts"] == {"total": 2, "ok": 2, "skipped": 0, "partial": 0, "failed": 0}
    assert len(detail["items"]) == 2


def test_second_job_while_running_is_409(client: TestClient) -> None:
    many = "\n".join(f"https://x.com/u/status/{1000 + i}" for i in range(30))
    first = client.post("/jobs", json={"urls": many})
    assert first.status_code == 202
    second = client.post("/jobs", json={"urls": "https://x.com/u/status/99"})
    assert second.status_code == 409
    assert "already running" in second.json()["detail"]
    _wait_done(client, first.json()["job_id"])


def test_urls_capped_at_500(client: TestClient) -> None:
    many = "\n".join(f"https://x.com/u/status/{i}" for i in range(501))
    resp = client.post("/jobs", json={"urls": many})
    assert resp.status_code == 413
    assert "500" in resp.json()["detail"]


def test_empty_post_is_400(client: TestClient) -> None:
    resp = client.post("/jobs", json={"urls": "\n#only comments\n  \n"})
    assert resp.status_code == 400


# --------------------------------------------------------------------------- #
# history + detail + retry
# --------------------------------------------------------------------------- #

def test_get_jobs_history(client: TestClient) -> None:
    client.post("/jobs", json={"urls": "https://x.com/u/status/11"})
    _wait_done(client, "1")
    jobs = client.get("/jobs").json()
    assert jobs["in_flight"] is None
    assert jobs["jobs"][0]["batch_id"] == 1
    assert jobs["jobs"][0]["ok"] == 1
    assert jobs["jobs"][0]["status"] == "done"


def test_retry_runs_only_failed_items(client: TestClient) -> None:
    # Stub fails (batch_id + idx) % 10 == 9 -> with batch 1, the 9th input fails.
    ten = "\n".join(f"https://x.com/u/status/{2_000 + i}" for i in range(10))
    client.post("/jobs", json={"urls": ten})
    detail = _wait_done(client, "1")
    assert detail["counts"]["failed"] == 1

    resp = client.post("/jobs/1/retry")
    assert resp.status_code == 202
    body = resp.json()
    assert body["retried"] == 1
    retry_detail = _wait_done(client, body["job_id"])
    assert retry_detail["counts"]["total"] == 1
    assert retry_detail["status"] == "ok"


def test_retry_with_no_failures_is_noop(client: TestClient) -> None:
    client.post("/jobs", json={"urls": "https://x.com/u/status/31\nhttps://x.com/u/status/32"})
    _wait_done(client, "1")
    resp = client.post("/jobs/1/retry")
    assert resp.status_code == 200  # noop: no new job spun up
    assert resp.json()["retried"] == 0


def test_unknown_job_404(client: TestClient) -> None:
    assert client.get("/jobs/999").status_code == 404


# --------------------------------------------------------------------------- #
# SSE
# --------------------------------------------------------------------------- #

def test_sse_stream_emits_documented_events(client: TestClient) -> None:
    resp = client.post("/jobs", json={"urls": "https://x.com/u/status/51"})
    job_id = resp.json()["job_id"]
    with client.stream("GET", f"/jobs/{job_id}/events") as stream:
        assert stream.status_code == 200
        assert stream.headers["content-type"].startswith("text/event-stream")
        kinds: list[str] = []
        seqs: list[int] = []
        for chunk in stream.iter_text():
            for line in chunk.split("\n\n"):
                line = line.strip()
                if not line.startswith("data:"):
                    continue
                event: dict[str, Any] = json.loads(line[5:].strip())
                kinds.append(event["kind"])
                seqs.append(event["seq"])
    assert kinds == [
        "batch_start", "resolve_start", "resolve_done", "download_start",
        "download_progress", "download_progress", "download_progress",
        "download_progress", "download_done", "item_done", "batch_done",
    ]
    assert seqs == sorted(set(seqs))


def test_sse_unknown_job_404(client: TestClient) -> None:
    assert client.get("/jobs/9999/events").status_code == 404


# --------------------------------------------------------------------------- #
# Flow B + reveal
# --------------------------------------------------------------------------- #

def test_analyze_returns_cards(client: TestClient) -> None:
    resp = client.post(
        "/analyze", json={"urls": "https://x.com/u/status/61\nhttps://x.com/u/status/62"}
    )
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 2
    for item in items:
        assert item["poster"].startswith("https://pbs.twimg.com/")
        assert item["author"].startswith("user")
        assert "Best MP4" in item["formats"]


def test_analyze_capped_and_empty(client: TestClient) -> None:
    assert client.post("/analyze", json={"urls": "#x"}).status_code == 400
    too_many = [f"https://x.com/u/status/{i}" for i in range(501)]
    resp = client.post("/analyze", json={"urls": too_many})
    assert resp.status_code == 413


def test_analyze_503_when_not_wired(tmp_path: Path) -> None:
    stub_minimal = StubOrchestrator()
    app = create_app(_settings(tmp_path), stub_minimal.run_batch)
    with TestClient(app, base_url="http://127.0.0.1:7788") as bare:
        resp = bare.post("/analyze", json={"urls": "https://x.com/u/status/71"})
        assert resp.status_code == 503


def test_reveal_invokes_os_reveal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # create_app captures callables at build time: wire the on-disk item_files
    # directly instead of mutating a stub after construction.
    async def on_disk(tweet_id: str) -> list[str]:
        return [str(tmp_path / f"{tweet_id}.mp4")]

    (tmp_path / "2005413641112408576.mp4").write_bytes(b"mp4")
    monkeypatch.setattr(ui_app, "_reveal_one", lambda p: called.append(Path(p)))
    called: list[Path] = []
    stub = StubOrchestrator()
    app = create_app(_settings(tmp_path), stub.run_batch, item_files=on_disk)
    with TestClient(app, base_url="http://127.0.0.1:7788") as bespoke:
        resp = bespoke.post("/items/2005413641112408576/reveal")
    assert resp.status_code == 200
    assert called == [tmp_path / "2005413641112408576.mp4"]


def test_reveal_without_file_404(client: TestClient) -> None:
    resp = client.post("/items/000/reveal")
    assert resp.status_code == 404


def test_reveal_503_when_not_wired(tmp_path: Path) -> None:
    stub_minimal = StubOrchestrator()
    app = create_app(_settings(tmp_path), stub_minimal.run_batch)
    with TestClient(app, base_url="http://127.0.0.1:7788") as bare:
        assert bare.post("/items/1/reveal").status_code == 503


# --------------------------------------------------------------------------- #
# options mapping
# --------------------------------------------------------------------------- #

def test_options_shape_maps_to_batch_request(client: TestClient, stub: StubOrchestrator) -> None:
    resp = client.post("/jobs", json={
        "urls": "https://x.com/u/status/81",
        "options": {"quality": "720", "output": None, "concurrency": 5},
        "items": [{"url": "https://x.com/u/status/81", "format": None}],
    })
    assert resp.status_code == 202
    bad = client.post("/jobs", json={
        "urls": "https://x.com/u/status/82",
        "options": {"concurrency": 50},
    })
    assert bad.status_code == 422  # pydantic ge/le validation
