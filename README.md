# downloader

Private, local batch downloader for X (Twitter) video posts — inspired by
[x-downloader.com](https://x-downloader.com/en/), but self-hosted on your own
machine: no web service, no third-party servers, no accounts.

**Status (2026-10-17, round 2): M0–M3 shipped, M6 config/logging shipped.**
`xdl <url>` downloads videos with metadata sidecars and SQLite history;
batches, dedup, skip-per-post and retry-failed work; the local web UI runs
(`python -m xdl.ui`) driven by the real core; config precedence
(CLI > `XDL_*` env > `config.toml` > defaults) and the rotating file log are
in. Resilience work (M4) and the HLS/ffmpeg/MP3 media paths (M5) are landing
next on `feat/core-2` — see the
[milestone plan](docs/05-implementation-plan.md) and
[CHANGELOG.md](CHANGELOG.md).

## Quickstart

Requires Python 3.12+ (Windows first-class, works everywhere Python does).

```bash
pip install -e ".[dev]"        # or: uv pip install -e ".[dev]"
xdl --version
```

```bash
# one post (any URL form: x.com, twitter.com, t.co, fxtwitter mirrors, …)
xdl "https://x.com/someuser/status/1750123456789012352"

# a batch: arguments, a file, or stdin — '#' comments and blanks are fine
xdl url1 url2 url3
xdl -f urls.txt
cat urls.txt | xdl -f -

# control it
xdl -o D:/Media/x --quality 720 --concurrency 4 url1 url2
xdl --dry-run url1            # resolve + print planned paths, download nothing
xdl retry-failed              # re-run items whose last run failed

# the local web UI (127.0.0.1 only; `xdl serve` alias pending)
python -m xdl.ui              # opens a browser automatically
python -m xdl.ui --no-open    # or just print the URL
```

Per post you get: the best-bitrate MP4 at
`downloads/x/<author>/<id>.mp4` (deterministic, Windows-safe — the filename
is exactly the status id from the URL), a
`<name>.meta.json` sidecar (caption, author, source URL, dims, size), and a
row in the local SQLite state store so re-runs **skip** already-downloaded
posts and `retry-failed` picks up the rest. Multi-video posts produce
`_v2`/`_v3` files; a post that half-finished is re-run for exactly the
missing media. HLS-only best variants currently stop with a clear
`HLS_REQUIRES_FFMPEG` message until the M5 remux wiring lands.

Exit codes: `0` all ok/skipped · `1` partial · `2` everything failed.

## Configuration

Precedence: **CLI flags > `XDL_*` env vars > `config.toml` > defaults.**
Env mapping is mechanical — `XDL_<SECTION>__<FIELD>` (e.g. `XDL_ENGINE__RETRIES=9`)
plus the conveniences `XDL_OUTPUT`, `XDL_CONCURRENCY`, `XDL_RESOLVE_CONCURRENCY`,
`XDL_FALLBACK`, `XDL_LOG_LEVEL`. File location: `%LOCALAPPDATA%\xdl\config.toml`
(`~/.config/xdl/config.toml` on Linux); a missing file is fine, unknown keys warn.
Copy the commented template `docs/config-template.toml` there as a starting point.
Logs: console via `-v/-vv`; always-on rotating file (5 MB × 3) at
`%LOCALAPPDATA%\xdl\logs\xdl.log` (values that look like cookies are redacted).

## Development

```bash
ruff check . && mypy && pytest        # all hermetic; add `-m "not live"` default is that
python scripts/ui_smoke.py            # localhost boot smoke vs the real core
python scripts/record_fixture.py <url-or-id> <name>   # re-record a syndication fixture
```

Tests never touch the network (`httpx.MockTransport` + recorded fixtures);
live checks are opt-in (`pytest -m live`) and the manual Windows matrix lives in
[docs/technical/09-testing.md](docs/technical/09-testing.md). Web UI tests drive the
real orchestrator via an injected mock client — see `tests/ui/README.md`.

## The plan

The full blueprint lives in [`docs/`](docs/README.md) — 7 planning documents plus
10 technical deep-dives covering goals, architecture, tech stack (Python 3.12 +
asyncio + httpx + rich), and implementation milestones:

- Start here → [`docs/README.md`](docs/README.md) (document map)
- What we're building → [`docs/01-goals-and-scope.md`](docs/01-goals-and-scope.md)
- Build order → [`docs/05-implementation-plan.md`](docs/05-implementation-plan.md)
  (M0 scaffold → M1 spike + single-URL → M2 batch/state → **M3 local web UI**
  (`python -m xdl.ui`) → M4 resilience → M5 media completeness → M6 polish)

## Snapshot of key decisions

- Single Python package, one process, CLI (`xdl`) + localhost web UI sharing one core.
- Resolver chain: syndication fast-path → yt-dlp adapter (auto, bundled);
  no X account needed by default. Token algorithm is a byte-exact V8 port
  (verified 3000-id sample vs Node 22) — see COORDINATION.md for the decision.
- Deterministic Windows-safe filenames, JSON metadata sidecars, SQLite history
  with skip/retry-failed semantics.
- Everything local — nothing phones home; the web UI binds 127.0.0.1 only and
  hard-fails on any other host.
