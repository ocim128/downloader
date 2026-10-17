# AGENT — Task: output files named exactly by tweet ID (single lane)

Small, well-defined change to a **working project**. No parallel lanes — you are
the only agent; work directly against `main`.

## Mission

Default output filenames must be **exactly the tweet ID from the URL**:

```
https://x.com/Rainmaker1973/status/2111083867478368447
→ 2111083867478368447.mp4        (+ 2111083867478368447.meta.json)
```

Today the default is `{date}_{id}` (`2026-10-17_2111083867478368447.mp4`). The
owner wants the bare ID — duplicates are a non-issue for this single-user local
tool, and an ID-named file maps 1:1 to its source URL.

## Context

- Repo: this clone of https://github.com/ocim128/downloader (Windows, Git Bash,
  Python 3.12+; `.venv` exists — `pip install -e ".[dev]"` if fresh).
- State: M0–M6 complete on `main`; 318 tests / ruff / mypy strict green.
- Read `COORDINATION.md` first (both round-1/2 handoffs + integrator notes) —
  the naming system is state-anchored and deviation-documented.

## Exact changes

1. **Default template `{date}_{id}` → `{id}`** in BOTH places it is defaulted:
   - `src/xdl/config.py` (`StorageCfg.filename_template`)
   - `src/xdl/storage/naming.py` (the `template` default)
   The template stays configurable — `{date}_{id}` must still work when set in
   `config.toml` (verify with a test; the renderer already supports `{id}`).
2. **Directory layout unchanged**: default stays `by-author`, so the result is
   `downloads/x/Rainmaker1973/2111083867478368447.mp4`. Only the filename
   changes — do not touch layout handling.
3. **Multi-video suffix stays**: two media in one post can't share a filename —
   index 1 → `2111083867478368447.mp4`, index ≥ 2 → `2111083867478368447_v2.mp4`
   (existing `_v{index}` behavior, now on the bare ID). Sidecar basename follows
   the media file automatically — verify, don't special-case.
4. **No migration, no renames of existing files**: skip logic reuses the stored
   `file_path` from `state.db`, so posts downloaded under the old
   `{date}_{id}` names keep skipping correctly; old- and new-named files
   coexist by design. Do NOT add a rename/migration pass.
5. **Tests**: update every assertion that encodes the old default (naming unit
   tests, integration `EXPECTED_VIDEO_PATH_PARTS`, any UI/docs-facing test),
   and add one regression test asserting the owner's exact example: URL with
   id `2111083867478368447` → file named `2111083867478368447.mp4` (in the
   author folder), sidecar `2111083867478368447.meta.json`.
6. **Docs**: update wherever the default is shown — `docs/technical/05-storage-naming-dedup.md`
   (template table + examples), `docs/technical/07-cli-and-config.md` (config
   sample), root `README.md` if it shows filenames; add a `CHANGELOG.md` entry.
   Keep the `{date}`/`{author}`/`{slug}` template variables documented — they
   remain available.

## Rules

- Smallest sensible diff; do not touch unrelated modules.
- Gates green at every commit: `pytest`, `ruff check .`, `mypy src`.
- Tests hermetic (no network).
- Deviations/notes: append under a new `## Agent naming notes` section in
  `COORDINATION.md`.

## Git

- `git checkout main && git pull && git checkout -b feat/naming-by-id`; stay on
  it; commit prefix `[naming]`; push `-u origin feat/naming-by-id` when done.
- Do not merge into `main` — the owner integrates.

## Definition of done

- `xdl <url>` on a fresh post writes `<id>.mp4` (+ sidecar) in the author
  folder; re-run skips; a post downloaded earlier under the old name still
  skips (state-anchored); the `{date}_{id}` template still works when
  configured; all gates green; branch pushed; handoff note appended.
