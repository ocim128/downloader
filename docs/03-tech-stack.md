# 03 — Tech Stack

## Language decision: **Python 3.12+**

Scored against the alternatives for *this* project (single-user local CLI, network-bound,
Windows primary platform, one maintainer):

| Criterion | Python 3.12 | Node.js/TS | Go | Rust |
|---|---|---|---|---|
| HTTP client w/ HTTP/2, streaming, timeouts | httpx ✅ | undici/got ✅ | stdlib ✅ | reqwest ✅ |
| Async I/O ergonomics for a batch pipeline | asyncio ✅ | native ✅ | goroutines ✅ | tokio ✅ |
| Prototyping speed / refactor cost | highest | high | medium | lowest |
| CLI ecosystem (args, help, completion) | typer ✅ | commander ✅ | cobra ✅ | clap ✅ |
| Terminal UI (progress, tables, Windows-safe) | rich ✅ | cli503/ora, weaker tables | bubbletea ✅ | ratatui ✅ |
| ffmpeg glue (subprocess + parse) | trivial | trivial | trivial | trivial |
| Reference code to crib from (react-tweet is TS) | port is small | direct | port | port |
| Packaging for the owner's machine (Windows) | uv/pipx, painless | node single-file is clunky | single binary ✅ | single binary ✅ |
| SQLite | stdlib ✅ | better-sqlite3 | stdlib | rusqlite |
| Maintenance risk in 5 years for a hobbyist | low | low | medium (rewrite cost) | medium |

**Decision: Python.** The dominant factors are iteration speed while reverse-engineered
endpoints churn (they will), and the richness of `rich` for progress UI on Windows. Go's
single-binary advantage doesn't buy much for an owner who can `pipx install` once.
The exact token algorithm ports cleanly (see
[technical/02-media-resolution.md](technical/02-media-resolution.md) for the IEEE-754-exact
base-36 trick).

## Runtime requirements

- Python **3.12+** (match/case, `tomllib` in stdlib, modern typing).
- OS: Windows 10/11 primary; code stays POSIX-clean (pathlib everywhere, no win32 calls).
- Optional external binary: **ffmpeg** on PATH — only needed for HLS posts, MP3
  extraction, or GIF conversion. Absence degrades those features with a clear error, and
  is detected at startup with an install hint (`winget install ffmpeg`).

## Dependencies

### Runtime (kept deliberately small)

| Package | Why | Notes |
|---|---|---|
| `httpx[http2]` | async HTTP client, streaming bodies, `MockTransport` for tests | the workhorse |
| `pydantic` ≥2 | typed models for remote JSON, `Settings` with TOML loading | remote data is untrusted — parse strictly |
| `typer` | CLI parsing, help, shell completion | thin wrapper over argparse |
| `rich` | progress bars, tables, Windows-safe styling | reporter layer only |
| `platformdirs` | OS-correct config/log dirs | `%APPDATA%\xdl` on Windows |

Deliberately **not** included:

- `tenacity` — retry/backoff is ~30 lines and needs deep integration with our throttle
  state and `Retry-After`; a library would fight us. Hand-rolled in `engine/throttle.py`.
- `aiofiles` — the download path writes sequentially per file; thread-offloaded writes
  add complexity without measurable gain at 3 concurrent files. Plain sync file I/O in
  the async path is acceptable *only* inside the downloader's write loop where chunks are
  large (documented decision; revisit if profiling shows stalls).
- `yt-dlp` as a **core runtime dependency** — the resolver layer stays own-code and
  source-agnostic (TikTok later), and yt-dlp's bulk/dedup/naming story doesn't fit our
  pipeline. But it is a **first-class opt-in fallback adapter**, not a dirty secret:
  installable as an extra (`pipx install ".[ytdlp]"`) and enabled with
  `fallback = "ytdlp"`. When X breaks the syndication endpoint, the immediate fix is a
  config flip while the primary adapter gets patched — the yt-dlp community fixes X
  extraction continuously, and solo-owning that arms race is this project's biggest
  maintenance risk. Outline: [technical/02-media-resolution.md](technical/02-media-resolution.md) Adapter D.

### Dev

| Package | Why |
|---|---|
| `pytest`, `pytest-asyncio` | test runner |
| `ruff` | lint + format (one tool) |
| `mypy` (strict on `src/xdl`) | the resolver/selector logic is fiddly; types pay off |
| `httpx` `MockTransport` + recorded JSON fixtures | hermetic adapter tests |

## Packaging & install

- `pyproject.toml`, `src/` layout, console script entry point: `xdl = xdl.cli:main`.
- Install for the owner: `uv tool install .` or `pipx install .` (isolated venv, on PATH).
- Dev: `uv venv && uv pip install -e ".[dev]"`.
- Python version pinned via `requires-python = ">=3.12"`.
- No lockfile commitment beyond what `uv` generates (`uv.lock` committed is fine).

## Why asyncio and not threads

The pipeline is fan-out network I/O with small critical sections. `asyncio` gives
structured cancellation (Ctrl+C mid-batch), natural semaphores/queues, and a single
place (the throttle) to coordinate global backoff. Threads would work but make graceful
shutdown and shared throttle state messier for zero perf benefit here.

## Versioning & compat policy

- Semver-ish for the owner's own use; breaking CLI changes noted in CHANGELOG.
- Pin nothing at runtime except a floor (`httpx>=0.27`, `pydantic>=2.7`); the tool is
  installed once, not shipped to strangers.
- Python 3.13/3.14 compatibility expected but not guaranteed in CI (local project —
  test on whatever the owner runs).
