"""Web UI tests (docs/technical/10), Agent B round 2.

Two sections:

1. **Real core** — happy-path/endpoint tests drive the REAL pipeline
   (`xdl.app.run_batch` + `xdl.urls.normalize` + `xdl.resolve.base.build_chain`)
   behind the UI's injected callables, with every remote call answered by a
   fixture-backed `httpx.MockTransport` world (`tests/ui/real_core.py`,
   recorded fixtures from `tests/fixtures/syndICATION/`). Hermetic: no
   network, no live ffmpeg, sqlite on tmp_path only.

2. **Stub fault injection** — cases that cannot be asserted reliably against
   an instantaneous mock core are kept on `StubOrchestrator` and clearly
   marked: second-job 409 while a batch is guaranteed mid-flight, the exact
   deterministic SSE event order (stub ticks), mid-batch crash, and a 429-style
   total-failure storm. Happy-path mapping is NOT re-tested here.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.ui.real_core import PHOTO_ID, VIDEO_ID, RealCoreRunner
from tests.ui.stub_orchestrator import StubOrchestrator
from xdl.config import EngineCfg, Settings, StorageCfg
from xdl.ui import app as ui_app
from xdl.ui.app import create_app

_BASE_URL = "http://127.0.0.1:7788"


def _settings(tmp_path: Path, *, delay: float = 0.0) -> Settings:
    return Settings(
        storage=StorageCfg(root=tmp_path / "downloads", db_path=tmp_path / "state.db"),
        engine=EngineCfg(delay=delay),
    )


@pytest.fixture()
def runner(tmp_path: Path) -> Iterator[RealCoreRunner]:
    inst = RealCoreRunner(_settings(tmp_path))
    yield inst
    asyncio.run(inst.aclose())


@pytest.fixture()
def client(tmp_path: Path, runner: RealCoreRunner) -> Iterator[TestClient]:
    app = create_app(
        _settings(tmp_path),
        runner.run_batch,
        analyze=runner.analyze,
        list_batches=None,  # hub history (this server's own jobs)
        item_files=runner.item_files,
    )
    with TestClient(app, base_url=_BASE_URL) as test_client:
        yield test_client


def _wait_done(client: TestClient, job_id: str, timeout: float = 10.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        detail = client.get(f"/jobs/{job_id}").json()
        if not detail.get("in_flight") and detail.get("status") != "running":
            return detail
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not finish")


# =========================================================================== #
# Section 1 — endpoint tests against the REAL core (fixture-backed world)
# =========================================================================== #


def test_index_has_csp_and_pastebox(client: TestClient) -> None:
    resp = client.get("/")
    assert resp.status_code == 200
    assert "default-src 'self'" in resp.headers["content-security-policy"]
    assert 'id="urls"' in resp.text and 'id="download"' in resp.text
    assert resp.headers["x-content-type-options"] == "nosniff"


def test_non_loopback_host_is_403(client: TestClient) -> None:
    resp = client.get("/", headers={"Host": "evil.example.com"})
    assert resp.status_code == 403


def test_post_jobs_parses_dedups_and_downloads(
    tmp_path: Path, client: TestClient, runner: RealCoreRunner
) -> None:
    payload = (
        f"https://x.com/reactiive_/status/{VIDEO_ID}\n"
        "# a comment line\n"
        "\n"
        f"https://x.com/reactiive_/status/{VIDEO_ID}\n"  # duplicate
    )
    resp = client.post("/jobs", json={"urls": payload})
    assert resp.status_code == 202
    body = resp.json()
    assert body["job_id"] == "1"
    assert body["accepted"] == 1
    assert body["duplicates"] == 1

    detail = _wait_done(client, body["job_id"])
    assert detail["status"] == "ok"
    counts = detail["counts"]
    assert (counts["ok"], counts["skipped"], counts["failed"]) == (1, 0, 0)

    item = detail["items"][0]
    assert item["tweet_id"] == VIDEO_ID
    assert item["author"] == "reactiive_"
    assert item["status"] == "ok"
    assert len(item["files"]) == 1
    for raw in item["files"]:
        assert Path(raw).is_file()
        assert Path(raw).with_name(Path(raw).name + ".meta.json").is_file()


def test_sse_stream_emits_documented_events_order(
    tmp_path: Path, runner: RealCoreRunner
) -> None:
    # Real core + per-gate pacing so the stream can subscribe mid-flight.
    app = create_app(
        _settings(tmp_path, delay=0.25),
        runner.run_batch,
        analyze=runner.analyze,
        item_files=runner.item_files,
    )
    with TestClient(app, base_url=_BASE_URL) as slow:
        resp = slow.post("/jobs", json={"urls": f"https://x.com/reactiive_/status/{VIDEO_ID}"})
        job_id = resp.json()["job_id"]
        with slow.stream("GET", f"/jobs/{job_id}/events") as stream:
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
                    if kinds and kinds[-1] == event["kind"]:
                        continue  # collapse repeated download_progress
                    kinds.append(event["kind"])
                    seqs.append(event["seq"])
    # Real-core stream: full resolve->download->item spine between
    # batch_start and batch_done. Progress-repeat events are collapsed so the
    # assertion doesn't depend on chunking (hub keeps the backlog — late
    # subscribers still see the whole run).
    assert kinds == [
        "batch_start", "resolve_start", "resolve_done", "download_start",
        "download_progress", "download_done", "item_done", "batch_done",
    ]
    assert seqs == sorted(seqs)


def test_history_detail_from_real_report(client: TestClient, runner: RealCoreRunner) -> None:
    resp = client.post("/jobs", json={"urls": f"https://x.com/reactiive_/status/{VIDEO_ID}"})
    job_id = resp.json()["job_id"]
    _wait_done(client, job_id)

    jobs = client.get("/jobs").json()
    assert jobs["in_flight"] is None
    summary = jobs["jobs"][0]
    assert summary["job_id"] == job_id
    assert summary["status"] == "ok"
    assert summary["counts"]["ok"] == 1

    again = client.get(f"/jobs/{job_id}").json()
    assert again["counts"]["total"] == 1
    assert again["items"][0]["tweet_id"] == VIDEO_ID


def test_skip_on_second_run(client: TestClient, runner: RealCoreRunner) -> None:
    url = f"https://x.com/reactiive_/status/{VIDEO_ID}"
    first = _wait_done(client, client.post("/jobs", json={"urls": url}).json()["job_id"])
    assert first["status"] == "ok"

    second = _wait_done(client, client.post("/jobs", json={"urls": url}).json()["job_id"])
    assert second["counts"] == {"total": 1, "ok": 0, "skipped": 1, "partial": 0, "failed": 0}
    assert second["items"][0]["status"] == "skipped"
    # Skip check fires before resolve: the mock world saw the id exactly once.
    assert runner.syndication_hits.count(VIDEO_ID) == 1


def test_mixed_batch_counts_and_statuses(client: TestClient) -> None:
    urls = f"https://x.com/reactiive_/status/{VIDEO_ID}\nhttps://x.com/i/status/{PHOTO_ID}"
    detail = _wait_done(client, client.post("/jobs", json={"urls": urls}).json()["job_id"])
    assert detail["status"] == "partial"
    counts = detail["counts"]
    assert counts == {"total": 2, "ok": 1, "skipped": 0, "partial": 0, "failed": 1}
    assert detail["items"][0]["status"] == "ok"
    failed = detail["items"][1]
    assert failed["status"] == "failed"
    assert "NO_VIDEO" in failed["detail"]


def test_retry_runs_only_failed_items(client: TestClient) -> None:
    urls = f"https://x.com/reactiive_/status/{VIDEO_ID}\nhttps://x.com/i/status/{PHOTO_ID}"
    first = client.post("/jobs", json={"urls": urls}).json()
    detail = _wait_done(client, first["job_id"])
    assert detail["counts"]["failed"] == 1

    resp = client.post(f"/jobs/{first['job_id']}/retry")
    assert resp.status_code == 202
    assert resp.json()["retried"] == 1
    retry_detail = _wait_done(client, resp.json()["job_id"])
    assert retry_detail["counts"]["total"] == 1
    # Photo-only posts never anchor media_count=0, so the retry re-runs and
    # fails NO_VIDEO again instead of being falsely skipped.
    assert retry_detail["items"][0]["status"] == "failed"


def test_retry_without_failures_is_noop(client: TestClient) -> None:
    detail = _wait_done(
        client,
        client.post(
            "/jobs", json={"urls": f"https://x.com/reactiive_/status/{VIDEO_ID}"}
        ).json()["job_id"],
    )
    assert detail["status"] == "ok"
    resp = client.post("/jobs/1/retry")
    assert resp.status_code == 200
    assert resp.json()["retried"] == 0


def test_options_quality_worst_downloads_lowest_bitrate(
    client: TestClient, runner: RealCoreRunner
) -> None:
    resp = client.post(
        "/jobs",
        json={"urls": f"https://x.com/reactiive_/status/{VIDEO_ID}",
              "options": {"quality": "worst"}},
    )
    detail = _wait_done(client, resp.json()["job_id"])
    assert detail["status"] == "ok"
    files = [Path(f) for f in detail["items"][0]["files"]]
    assert len(files) == 1
    # The 320x320 (432 kbps) variant is the lowest progressive bitrate recorded
    # in video_chibicode_13s.json.
    assert files[0].read_bytes() == b"media:eNzyT-nTzq41D80G.mp4"
    assert Path(files[0]).is_file()


def test_bad_concurrency_is_422(client: TestClient) -> None:
    resp = client.post("/jobs", json={
        "urls": f"https://x.com/reactiive_/status/{VIDEO_ID}",
        "options": {"concurrency": 50},
    })
    assert resp.status_code == 422  # pydantic ge/le validation


def test_urls_capped_at_500_and_empty_post_400(client: TestClient) -> None:
    many = "\n".join(f"https://x.com/u/status/{i}" for i in range(501))
    resp = client.post("/jobs", json={"urls": many})
    assert resp.status_code == 413
    assert "500" in resp.json()["detail"]
    empty = client.post("/jobs", json={"urls": "\n#only comments\n  \n"})
    assert empty.status_code == 400


def test_unknown_job_and_event_404(client: TestClient) -> None:
    assert client.get("/jobs/999").status_code == 404
    assert client.get("/jobs/999/events").status_code == 404


def test_analyze_real_chain(client: TestClient) -> None:
    payload = (
        f"https://x.com/reactiive_/status/{VIDEO_ID}\nhttps://x.com/i/status/{PHOTO_ID}"
    )
    resp = client.post("/analyze", json={"urls": payload})
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 2
    card = items[0]
    assert card["tweet_id"] == VIDEO_ID
    assert card["author"] == "reactiive_"
    assert card["poster"].startswith("https://pbs.twimg.com/")
    assert card["canonical_url"].endswith(f"/status/{VIDEO_ID}")
    # No per-item format surface: /analyze cards describe the post only.
    assert "formats" not in card
    assert items[1]["tweet_id"] == PHOTO_ID  # photo post resolves; only download fails
    assert "error" not in items[1]


def test_reveal_via_real_state(
    tmp_path: Path, client: TestClient, runner: RealCoreRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    detail = _wait_done(
        client,
        client.post(
            "/jobs", json={"urls": f"https://x.com/reactiive_/status/{VIDEO_ID}"}
        ).json()["job_id"],
    )
    assert detail["status"] == "ok"
    revealed: list[Path] = []
    monkeypatch.setattr(ui_app, "_reveal_one", lambda p: revealed.append(Path(p)))
    resp = client.post(f"/items/{VIDEO_ID}/reveal")
    assert resp.status_code == 200
    assert len(revealed) == 1
    assert revealed[0].is_file()
    assert revealed[0].suffix == ".mp4"


def test_analyze_reveal_503_when_not_wired(tmp_path: Path, runner: RealCoreRunner) -> None:
    app = create_app(_settings(tmp_path), runner.run_batch)  # bare: no optional callables
    with TestClient(app, base_url=_BASE_URL) as bare:
        url = f"https://x.com/reactiive_/status/{VIDEO_ID}"
        assert bare.post("/analyze", json={"urls": url}).status_code == 503
        assert bare.post("/items/1/reveal").status_code == 503


# =========================================================================== #
# Section 2 — fault injection on the STUB (kept deliberately; see module doc)
# =========================================================================== #


@pytest.fixture()
def stub_client(tmp_path: Path, stub: StubOrchestrator) -> Iterator[TestClient]:
    app = create_app(
        _settings(tmp_path),
        stub.run_batch,
        analyze=stub.analyze,
        list_batches=stub.list_batches,
        item_files=stub.item_files,
    )
    with TestClient(app, base_url=_BASE_URL) as test_client:
        yield test_client


@pytest.fixture()
def stub() -> StubOrchestrator:
    return StubOrchestrator()


def test_stub_second_job_while_running_is_409(stub_client: TestClient) -> None:
    # Fault case: the stub's tick pacing keeps one batch guaranteed in flight
    # (against the real core the mock world finishes before the 2nd POST lands).
    many = "\n".join(f"https://x.com/u/status/{1000 + i}" for i in range(30))
    first = stub_client.post("/jobs", json={"urls": many})
    assert first.status_code == 202
    second = stub_client.post("/jobs", json={"urls": "https://x.com/u/status/99"})
    assert second.status_code == 409
    assert "already running" in second.json()["detail"]
    _wait_done(stub_client, first.json()["job_id"])


def test_stub_sse_exact_tick_order(stub_client: TestClient) -> None:
    # Fault case: deterministic tick sequence incl. 4 progress events, which
    # the chunked real world cannot promise.
    resp = stub_client.post("/jobs", json={"urls": "https://x.com/u/status/51"})
    job_id = resp.json()["job_id"]
    with stub_client.stream("GET", f"/jobs/{job_id}/events") as stream:
        assert stream.status_code == 200
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


def test_stub_sse_closes_immediately_for_finished_job(stub_client: TestClient) -> None:
    # Regression: attaching to an ALREADY-finished job used to replay the backlog
    # and then block on an empty queue for the full 15 s ping timeout.
    resp = stub_client.post("/jobs", json={"urls": "https://x.com/u/status/51"})
    job_id = resp.json()["job_id"]
    _wait_done(stub_client, job_id)  # job is done before we subscribe

    started = time.monotonic()
    with stub_client.stream("GET", f"/jobs/{job_id}/events") as stream:
        body = "".join(stream.iter_text())
    elapsed = time.monotonic() - started

    assert "batch_done" in body  # the backlog was still delivered
    assert elapsed < 5.0, f"stream idled for {elapsed:.1f}s on a finished job"


def test_stub_mid_batch_crash_marks_job_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Fault case: run_batch raises mid-batch → job failed, hub slot released.
    crashing = StubOrchestrator(crash_after_resolve=True)
    app = create_app(_settings(tmp_path), crashing.run_batch)
    with TestClient(app, base_url=_BASE_URL) as test_client:
        resp = test_client.post("/jobs", json={"urls": "https://x.com/u/status/51"})
        assert resp.status_code == 202
        detail = _wait_done(test_client, resp.json()["job_id"])
        assert detail["status"] == "failed"
        assert "RuntimeError" in detail.get("error", "")
        # hub released the slot: a follow-up job is accepted
        again = test_client.post("/jobs", json={"urls": "https://x.com/u/status/52"})
        assert again.status_code == 202
        _wait_done(test_client, again.json()["job_id"])


def test_stub_429_storm_fails_all_items_then_retry(tmp_path: Path) -> None:
    # Fault case: every item fails RATE_LIMITED → job "failed", retry re-runs
    # the whole report-aligned input set.
    storm = StubOrchestrator(fail_code="RATE_LIMITED")
    app = create_app(_settings(tmp_path), storm.run_batch)
    with TestClient(app, base_url=_BASE_URL) as test_client:
        urls = "\n".join(f"https://x.com/u/status/{i}" for i in (1, 2, 3))
        resp = test_client.post("/jobs", json={"urls": urls})
        detail = _wait_done(test_client, resp.json()["job_id"])
        assert detail["status"] == "failed"
        assert detail["counts"] == {
            "total": 3, "ok": 0, "skipped": 0, "partial": 0, "failed": 3,
        }
        retry = test_client.post(f"/jobs/{resp.json()['job_id']}/retry")
        assert retry.status_code == 202
        assert retry.json()["retried"] == 3
        retry_detail = _wait_done(test_client, retry.json()["job_id"])
        assert retry_detail["counts"]["failed"] == 3


# --------------------------------------------------------------------- #
# State-backed history (regression: non-empty history crashed the page)
# --------------------------------------------------------------------- #


def test_state_history_renders_on_index(tmp_path: Path) -> None:
    """Round-2 integration filing: with real state rows behind list_batches,
    the index page must render — state rows previously lacked `started_at`
    (template raised UndefinedError) and lack `counts` (must degrade, not
    crash). Found live: any machine with batch history got HTTP 500 on /."""
    from xdl.storage.state import StateStore
    from xdl.ui.__main__ import list_state_batches

    db = tmp_path / "state.db"
    store = StateStore(db)
    try:
        batch_id = store.new_batch("cli", 3)
        store.finish_batch(batch_id, "partial")
    finally:
        store.close()

    rows = list_state_batches(db)
    assert rows and rows[0]["job_id"] == batch_id
    assert isinstance(rows[0]["started_at"], float) and rows[0]["started_at"] > 0
    assert rows[0]["total"] == 3 and rows[0]["origin"] == "cli"
    assert "counts" not in rows[0]

    async def _list_batches(limit: int = 50) -> list[dict[str, Any]]:
        return list_state_batches(db, limit)

    async def _unused_run(req: Any, **kw: Any) -> Any:  # pragma: no cover
        raise AssertionError("run_batch must not be called here")

    app = create_app(_settings(tmp_path), _unused_run, list_batches=_list_batches)
    with TestClient(app, base_url=_BASE_URL) as test_client:
        page = test_client.get("/")
        assert page.status_code == 200
        assert f"#{batch_id}" in page.text
        assert "item(s)" in page.text  # counts-free degradation visible
        jobs = test_client.get("/jobs").json()["jobs"]
        assert jobs[0]["started_at"] > 0


# --------------------------------------------------------------------------- #
# Proxy absolute-form request targets (RFC 9112 §3.2.2)
# --------------------------------------------------------------------------- #

def _scope(target: str) -> dict[str, Any]:
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": target,
        "raw_path": target.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"127.0.0.1:7788")],
        "client": ("127.0.0.1", 50000),
        "server": ("127.0.0.1", 7788),
    }


def test_absolute_form_target_is_normalized() -> None:
    scope = _scope("http://127.0.0.1:7788/jobs?x=1")
    ui_app._normalize_absolute_target(scope)
    assert scope["path"] == "/jobs"
    assert scope["raw_path"] == b"/jobs"
    assert scope["query_string"] == b"x=1"


def test_absolute_form_without_path_means_root() -> None:
    scope = _scope("http://localhost:7788")
    ui_app._normalize_absolute_target(scope)
    assert scope["path"] == "/"


def test_absolute_form_foreign_host_is_not_rewritten() -> None:
    scope = _scope("http://evil.example/jobs")
    ui_app._normalize_absolute_target(scope)
    assert scope["path"] == "http://evil.example/jobs"


def test_origin_form_is_untouched() -> None:
    scope = _scope("/jobs")
    ui_app._normalize_absolute_target(scope)
    assert scope["path"] == "/jobs"


async def test_absolute_form_request_reaches_the_router(tmp_path: Path) -> None:
    """The whole point: an absolute-form target must not 404.

    A forward proxy forwards `GET http://127.0.0.1:7788/ HTTP/1.1`; uvicorn's
    h11 parser leaves the URI in scope["path"], so without the middleware the
    index route misses and the page 404s.
    """
    async def _run(req: Any, **kw: Any) -> Any:  # pragma: no cover
        raise AssertionError("run_batch must not be called here")

    app = create_app(_settings(tmp_path), _run)
    messages: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    await app(_scope("http://127.0.0.1:7788/"), receive, send)
    assert messages[0]["type"] == "http.response.start"
    assert messages[0]["status"] == 200


# =========================================================================== #
# Section 3 — job hub retention (a long-lived server must not grow unbounded)
# =========================================================================== #


def test_progress_backlog_is_bounded_and_counters_stay_exact() -> None:
    from xdl.report.events import EventKind, ProgressEvent
    from xdl.ui.app import _MAX_PROGRESS_REPLAY, _counts, _Job

    job = _Job(id="1", inputs=["a"], quality=None, output=None, concurrency=None)
    for i in range(_MAX_PROGRESS_REPLAY * 5):
        job.emit(ProgressEvent(EventKind.download_progress, tweet_id="1",
                               media_index=1, offset=i))
    job.emit(ProgressEvent(EventKind.item_done, tweet_id="1", status="ok"))

    assert len(job.progress) == _MAX_PROGRESS_REPLAY  # ring buffer, not a list
    assert len(job.events) == 1  # lifecycle kept in full
    backlog = job.replay()
    assert len(backlog) == _MAX_PROGRESS_REPLAY + 1
    assert backlog[-1]["kind"] == "item_done"  # chronological, lifecycle last
    assert _counts(job)["ok"] == 1  # O(1) tally matches the events


def test_hub_evicts_oldest_finished_jobs() -> None:
    from xdl.ui.app import _MAX_JOBS, _Hub, _Job

    hub = _Hub()
    for _ in range(_MAX_JOBS + 10):
        job = hub.create(_Job(id="", inputs=[], quality=None, output=None,
                              concurrency=None))
        hub.finish(job)
    assert len(hub.jobs) == _MAX_JOBS
    assert len(hub.order) == _MAX_JOBS
