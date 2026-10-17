# downloader

Private, local batch downloader for X (Twitter) video posts — inspired by
[x-downloader.com](https://x-downloader.com/en/), but self-hosted on your own
machine: no web service, no third-party servers, no accounts.

**Status: core pipeline implemented (M0–M2).** `xdl <url>` downloads videos
with metadata sidecars and SQLite history; batches, dedup, skip and
retry-failed work. The local web UI (`xdl serve`, M3), resilience work (M4)
and HLS/ffmpeg paths (M5) are next — see the
[milestone plan](docs/05-implementation-plan.md).

## Quickstart

Requires Python 3.12+ (Windows first-class, works everywhere Python does).

```bash
uv venv && uv pip install -e ".[dev]"   # or: python -m venv .venv && pip install -e ".[dev]"
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

# dev gates
ruff check . && mypy && pytest
```

What you get per post: the best-bitrate MP4 at
`downloads/x/<author>/<date>_<id>.mp4` (deterministic, Windows-safe),
a `<name>.meta.json` sidecar (caption, author, source URL, dims, size), and a
row in the local SQLite state store so re-runs **skip** already-downloaded
posts and `retry-failed` picks up the rest. Multi-video posts produce
`_v2`/`_v3` files; a post that half-finished is re-run for exactly the
missing media.

Exit codes: `0` all ok/skipped · `1` partial · `2` everything failed.

## The plan

The full blueprint lives in [`docs/`](docs/README.md) — 7 planning documents plus
10 technical deep-dives covering goals, architecture, tech stack (Python 3.12 +
asyncio + httpx + rich), and implementation milestones:

- Start here → [`docs/README.md`](docs/README.md) (document map)
- What we're building → [`docs/01-goals-and-scope.md`](docs/01-goals-and-scope.md)
- Build order → [`docs/05-implementation-plan.md`](docs/05-implementation-plan.md)
  (M0 scaffold → M1 spike + single-URL → M2 batch/state → **M3 local web UI**
  (`xdl serve`) → M4 resilience → M5 media completeness → M6 polish)

## Snapshot of key decisions

- Single Python package, one process, CLI (`xdl`) + localhost web UI sharing one core.
- Resolver chain: syndication fast-path → yt-dlp adapter (auto, bundled);
  no X account needed by default.
- Deterministic Windows-safe filenames, JSON metadata sidecars, SQLite history
  with skip/retry-failed semantics.
- Everything local — nothing phones home.
