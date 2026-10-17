"""Boot smoke for `xdl serve`/`python -m xdl.ui` against the REAL core (M6+).

Hermetic on purpose: a real uvicorn server is served on 127.0.0.1:<port>, but
every remote call is answered by an httpx.MockTransport world built from
Agent B's recorded fixtures (tests/fixtures/syndication/*.json). It drives ONE
real batch through localhost and verifies the SSE event order + the summary
and on-disk outputs, printing PASS/FAIL per check.

Usage (from the repo root):
    python scripts/ui_smoke.py [--port 7799]

Exit code 0 = all checks passed. Never run inside pytest (it binds a socket).
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import threading
import time
import urllib.parse
from pathlib import Path

import httpx

from xdl.config import EngineCfg, Settings, StorageCfg

_VIDEO_ID = "2040511285998313827"
_FIXTURE = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "syndication"


def _payload() -> dict[str, str]:
    envelope = json.loads(
        (_FIXTURE / "video_chibicode_13s.json").read_text(encoding="utf-8")
    )
    return envelope["response"]


def _mock_client() -> httpx.AsyncClient:
    pl = _payload()
    bodies: dict[tuple[str, str], bytes] = {}
    for detail in pl.get("mediaDetails", []):
        for variant in (detail.get("video_info") or {}).get("variants", []) or []:
            if variant.get("content_type") != "video/mp4":
                continue
            url: str = variant["url"]
            parts = urllib.parse.urlsplit(url)
            filename = url.rsplit("/", 1)[-1].split("?")[0]
            bodies[(parts.hostname or "", parts.path)] = f"media:{filename}".encode()

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host or ""
        if host == "cdn.syndication.twimg.com":
            if request.url.params.get("id") == _VIDEO_ID:
                return httpx.Response(200, json=pl)
            return httpx.Response(404, text="<html class='dog'>gone</html>")
        if host.endswith(".twimg.com"):
            body = bodies.get((host, request.url.path))
            if body is None:
                return httpx.Response(404)
            return httpx.Response(
                200, content=body, headers={"content-length": str(len(body))}
            )
        return httpx.Response(404)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def build_app(port: int):  # type: ignore[no-untyped-def]
    # The real wiring lives in xdl.ui.__main__.build; we mirror it but inject
    # the fixture-backed mock client into run_batch so the smoke stays
    # offline. analyze/item_files below are the same real compositions.
    tmp = tempfile.TemporaryDirectory(prefix="xdl-smoke-")
    settings = Settings(
        storage=StorageCfg(
            root=Path(tmp.name) / "downloads",
            db_path=Path(tmp.name) / "state.db",
        ),
        engine=EngineCfg(delay=0.2),  # pacing: the SSE stream must see ticks
    )

    import xdl.app as core
    from xdl.ui.app import create_app

    client = _mock_client()

    def run_batch(req, *, on_event=None):  # type: ignore[no-untyped-def]
        return core.run_batch(req, client=client, on_event=on_event)

    return create_app(settings, run_batch), tmp


def drive(base: str) -> int:
    failures = 0
    # trust_env=False: a corporate/system HTTP(S)_PROXY turns loopback requests
    # into absolute-form ones, which the app's router answers 404.
    with httpx.Client(base_url=base, timeout=30, trust_env=False) as http:
        post = http.post("/jobs", json={"urls": f"https://x.com/reactiive_/status/{_VIDEO_ID}"})
        assert post.status_code == 202, post.text
        job_id: str = post.json()["job_id"]

        kinds: list[str] = []
        with http.stream("GET", f"/jobs/{job_id}/events") as stream:
            for line in stream.iter_lines():
                if not line.startswith("data:"):
                    continue
                event = json.loads(line[5:].strip())
                kind = event["kind"]
                if not kinds or kinds[-1] != kind:  # collapse progress repeats
                    print(f"    SSE {kind}")
                    kinds.append(kind)

        expected = [
            "batch_start", "resolve_start", "resolve_done", "download_start",
            "download_progress", "download_done", "item_done", "batch_done",
        ]
        collapse = [k for i, k in enumerate(kinds) if i == 0 or kinds[i - 1] != k]
        if collapse != expected:
            print(f"FAIL  SSE order: {kinds}")
            failures += 1
        else:
            print("PASS  SSE order")

        deadline = time.monotonic() + 15
        detail: dict[str, str] = {}
        while time.monotonic() < deadline:
            detail = http.get(f"/jobs/{job_id}").json()
            if not detail.get("in_flight"):
                break
            time.sleep(0.1)
        ok = detail.get("status") == "ok" and detail.get("counts", {}).get("ok") == 1
        print(f"PASS  summary {detail.get('status')} {detail.get('counts')}" if ok
              else f"FAIL  summary {detail}")
        failures += 0 if ok else 1
        for item in detail.get("items", []):
            for file in item.get("files", []):
                assert Path(file).is_file(), file
        return failures


def main() -> int:
    import uvicorn

    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=7799)
    args = parser.parse_args()

    app, tmpdir = build_app(args.port)
    config = uvicorn.Config(app, host="127.0.0.1", port=args.port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.05)
    if not server.started:
        print("FAIL  server did not start")
        return 2
    base = f"http://127.0.0.1:{args.port}"
    with httpx.Client(base_url=base, timeout=10, trust_env=False) as http:
        page = http.get("/")
        print(f"PASS  GET / -> {page.status_code}, CSP: "
              f"{page.headers.get('content-security-policy', '')[:40]}...")
        if page.status_code != 200:
            return 2
    code = drive(base)
    server.should_exit = True
    thread.join(timeout=5)
    tmpdir.cleanup()
    print("SMOKE PASS" if code == 0 else "SMOKE FAIL")
    return code


if __name__ == "__main__":
    sys.exit(main())
