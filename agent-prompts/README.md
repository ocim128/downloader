# Parallel Agent Prompts — Round 2

Round 1 is **merged to `main`** (M0–M3: core CLI, batch/state, resolvers with
V8-exact token, `media/hls.py`, local web UI — 230 tests green, ruff + mypy clean,
live-verified). These are the round-2 prompts: two agents, two lanes, hard file
ownership, separate branches, no conflicts. Each prompt is self-contained for a
fresh chat.

| | Agent A | Agent B |
|---|---|---|
| Prompt file | [agent-a-core-pipeline.md](agent-a-core-pipeline.md) | [agent-b-spike-ui-hls.md](agent-b-spike-ui-hls.md) |
| Branch | `feat/core-2` (new, from `main`) | `feat/ui-2` (new, from `main`) |
| Directory | main checkout | separate clone (`../downloader-agent-b`) |
| Mission | **M4 resilience** (retries/backoff, 429 throttle, circuit breaker, Ctrl+C 130, `LINK_EXPIRED` recovery, `YtdlpResolver` + `fallback="auto"` chain) → **M5 media completeness** (HLS wiring at the marked `app._download_media` branch, MP3/GIF, `--force`/`--no-meta`) → small surface items (`xdl.app.analyze`, `xdl serve`, `xdl config`) | **UI on real core** (drop stub from happy-path tests, keep for fault injection) → **M6 config & logging** (`config.py` precedence, `report/logging.py`) → **docs reconciliation** (sync docs to shipped code per `COORDINATION.md` deviations) → **manual test matrix** |
| Commit prefix | `[core2]` | `[ui2]` |

Round-1 prompts live in git history (`git log -- agent-prompts/`).

## How to launch

1. Open two fresh agent sessions.
2. Paste the full contents of each prompt file. Self-contained — no other context
   needed. Both prompts point the agent at `COORDINATION.md` first.
3. Both push their own branches and append handoffs to `COORDINATION.md`.

## Integration (after both finish)

```
git checkout main && git merge feat/core-2   # core first (engine/app/cli surface)
git merge feat/ui-2                          # ui/config/docs — disjoint files
pytest                                        # full suite green = integrated
```

Conflicts should be impossible by construction (disjoint ownership; the only
cross-lane line — `setup_logging` called in `cli.main()` — is explicitly tasked to
Agent A with a fallback TODO). If a conflict appears, an agent crossed the
ownership line; check `COORDINATION.md` for the recorded reason.

## Known cross-lane touchpoints (deliberate, both prompts carry them)

- `xdl.app.analyze(...)` — A implements; B's UI switches injection post-merge.
- `report/logging.py::setup_logging` — B delivers the module; A wires the one-line
  call in `cli.main()`.
- `Settings.load(overrides)` signature — B may rework internals; the signature is
  frozen for A's CLI.
