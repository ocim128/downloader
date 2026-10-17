# downloader

Private, local batch downloader for X (Twitter) video posts — inspired by
[x-downloader.com](https://x-downloader.com/en/), but self-hosted on your own
machine: no web service, no third-party servers, no accounts.

**Status: planning complete, no code yet.**

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
