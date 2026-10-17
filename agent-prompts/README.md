# Parallel Agent Prompts

Two self-contained prompts for two AI agents implementing the plan **in parallel,
without conflicts**. The split is by hard file ownership + separate branches +
separate working directories — the lanes never edit the same file.

| | Agent A | Agent B |
|---|---|---|
| Prompt file | [agent-a-core-pipeline.md](agent-a-core-pipeline.md) | [agent-b-spike-ui-hls.md](agent-b-spike-ui-hls.md) |
| Branch | `feat/core` | `feat/ui-spike` |
| Directory | main checkout | separate clone (e.g. `../downloader-agent-b`) |
| Owns | scaffold, models, urls, resolvers (syndication + chain), engine, selector, storage/state/sidecar, CLI, batch orchestration (M0→M2) | live spike (fixtures + token goldens), `media/hls.py`, web UI + stub orchestrator (M3) |
| Critical deliverable | `xdl <url>` works, then batch+state | fixtures/goldens first (unblocks A), then HLS, then UI |

## How to launch

1. Open two agent sessions (two windows/terminals).
2. Paste the full contents of `agent-a-core-pipeline.md` into one,
   `agent-b-spike-ui-hls.md` into the other. Each prompt is self-contained.
3. Let them run; both push their own branches and append handoffs to
   `COORDINATION.md`.

## Integration (after both finish)

Merge order matters:

```
git checkout main
git merge feat/core          # core first — it defines models/pyproject
# resolve trivially: B touched none of A's files
git merge feat/ui-spike
# delete tests/ui/stub_orchestrator.py usage where the real core now exists
# (each B test that used the stub has a marked TODO)
pytest                       # full suite green = integration done
```

Conflicts should be impossible by construction; if one appears, it means an agent
crossed the ownership line — check `COORDINATION.md` for the recorded reason.

## Why this split

- The core pipeline (A) is the critical path — everything else stubs or feeds it.
- The spike/fixtures (B, priority 1) needs **no code from A** and directly unblocks
  A's adapter/token tests.
- The web UI (B) is built against the frozen interfaces in
  `docs/04-project-structure.md` via a stub orchestrator, so it never waits on A.
- `media/hls.py` (B) is a leaf module with a complete standalone spec.
