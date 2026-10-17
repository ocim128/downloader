# tests/ui — web UI test suite (Agent B, round 2)

Two tiers, both hermetic (no network, no live ffmpeg, sqlite on `tmp_path`):

1. **Real core** (`test_serve.py` "Section 1") — the endpoint/happy-path tests
   drive the REAL pipeline through the UI's injected callables:
   - `tests/ui/real_core.py::RealCoreRunner` wraps the real
     `xdl.app.run_batch(req, *, client=..., on_event=...)` — the mock
     `httpx.AsyncClient` (Agent A's documented injection kwarg) is built by
     `_mock_world()` from **recorded fixtures** in `tests/fixtures/syndication/*.json`
     (envelope `response` payloads: `video_chibicode_13s`, `video_f1_8s`,
     `photo_single_cwsports`; unknown ids get the live-shaped 404 dog page).
   - `analyze` is the real Flow-B composition (`xdl.urls.normalize` +
     `xdl.resolve.base.build_chain`) — same shape as `python -m xdl.ui`, minus
     the network (mock client injected, nothing passed through).
   - `item_files` reads the real `StateStore` (wired to the test tmp db), so
     `/items/<id>/reveal` is exercised against genuine state rows on disk.
   - The SSE order test uses `EngineCfg(delay=0.25)` so the stream can
     subscribe while the batch is mid-flight; progress-repeating events are
     collapsed before comparison (chunk counts are not deterministic).
2. **Stub fault injection** (`test_serve.py` "Section 2") — cases that cannot
   be asserted reliably against an instantaneous mock core live on
   `StubOrchestrator` (fault knobs: `crash_after_resolve`, `fail_code`):
   second-job **409** while a batch is guaranteed mid-flight, the **exact
   deterministic SSE tick order** (4 progress events), **mid-batch crash**
   (job marked failed, hub slot released), and a **429-style storm** (all
   items failed → retry realigns inputs). Happy-path mapping is not
   re-tested on the stub.

Run:

```powershell
python -m pytest tests/ui -q            # the UI tier
python -m pytest -q                     # full suite
```

## Boot smoke (manual, not pytest)

`scripts/ui_smoke.py` boots a **real uvicorn server on loopback** wired to the
real core (same composition as `python -m xdl.ui`), with every remote call
served by the fixture-backed MockTransport world, then drives one real batch:

```powershell
python scripts/ui_smoke.py --port 7799
# expected: PASS  GET / -> 200 ... ; exact SSE spine batch_start..batch_done;
#           PASS  summary ok {'total': 1, 'ok': 1, ...}; exit code 0 / SMOKE PASS
```

Plain CLI boot check (real network, manual only — never during pytest):

```powershell
python -m xdl.ui --no-open --port 7801
# then GET http://127.0.0.1:7801/ must be 200 with the CSP header
```

## Files

| file | lane |
|---|---|
| `real_core.py` | fixture-backed MockTransport world + real-core runner (B, round 2) |
| `stub_orchestrator.py` | fault-injection stub only (was the round-1 full stub) |
| `test_serve.py` | both tiers, sections marked |

## Resolved filings (kept as regression notes)

- Retrying a failed NO_VIDEO item used to report `skipped`: `app.py` upserted
  `tweets.media_count=0` for photo-only posts, so `seen_complete()` counted
  0 ok-rows == 0 as a false complete. Fixed — a `tweets` row is written only
  after `select()` succeeds; `test_retry_runs_only_failed_items` asserts the
  retry reports `failed`.
- Per-item `quality`/`format` choices were collected by the UI but only applied
  batch-level. The phantom surface is gone: `/analyze` cards describe the post
  only, and `quality: "mp3"` is a batch-level audio-only choice.
