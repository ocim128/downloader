# Technical 07 — CLI & Configuration

`xdl` — typer-based, single command surface plus a few subcommands. Everything is
configurable by flag, env (`XDL_*`), or TOML; precedence **flag > env > file > default**.

## Command surface

```
xdl [OPTIONS] [URLS]...

  URLS                       zero or more URLs (also: -f FILE, or '-' for stdin)

Input:
  -f, --file PATH            batch file: one URL per line, '#'-comments, blanks ok
  --id                       treat bare numeric arguments as tweet ids

Output:
  -o, --output PATH          downloads root (default ./downloads)
  --quality TEXT             'best' | 'worst' | number = max height (e.g. 720)
  --audio-only               extract MP3 (ffmpeg) instead of video        [shipped: M5]
  --gif                      convert gif-kind posts to real .gif (ffmpeg) [shipped: M5]
  --no-meta                  skip sidecar JSON
  --force                    re-download even if state says complete

Behavior:
  -c, --concurrency INT      parallel file downloads [default: 3]
  --resolve-concurrency INT  parallel metadata lookups [default: 4]
  --delay SECONDS            extra fixed delay between resolves [default: jittered 0.2-0.5]
  --retries INT              max attempts per item [default: 5]
  --fallback TEXT            resolver fallback: 'auto' | 'ytdlp' | 'fxtwitter' | 'none'
  --dry-run                  resolve + print planned paths, download nothing

Subcommands:
  xdl retry-failed           re-run items whose state status is failed/incomplete
  xdl config                 print the resolved settings + paths
  xdl serve                  localhost web UI, 127.0.0.1 only
```

**Shipped.** `xdl [OPTIONS] URLS...` is the *default command* (a single-command typer
app + group dispatch in `main()` — DECISIONS.md #18: click group callbacks can't
intersperse options after positionals AND keep subcommands). `xdl retry-failed`,
`xdl config`, and `xdl serve` all work; `xdl serve` delegates to `python -m xdl.ui`
(same app, one flag surface). A documented `config.toml` template lives at
`docs/config-template.toml`.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | all items ok or skipped |
| 1 | partial: ≥1 item failed, ≥1 succeeded |
| 2 | total: every item failed, or usage/config error before any IO |
| 130 | interrupted (Ctrl+C) — summary printed, partials resumable |

Partial-failure reporting is first-class: the summary table is the product.

## Output UX (rich)

- **During run**: one progress bar per in-flight download (total size, speed, ETA) up to
  3 visible; a live status line for resolves ("resolving 34/120"); counters pinned top:
  `ok 12 · skip 3 · fail 0 · queued 105`.
- **End**: summary table — per item: short id, author, status, path (truncated), elapsed
  or error name + one-line detail. Then totals line. Nothing decorative; the table is
  greppable-ish.
- **No color/TTY** (piped output): rich auto-degrades to plain lines; progress becomes
  per-item single log lines. CI-friendly.
- Encoding: UTF-8 output on Windows Terminal; set
  `sys.stdout.reconfigure(encoding="utf-8")` guard for legacy consoles.

## Config file

Location (as shipped): `platformdirs.user_config_dir("xdl", appauthor=False)` →
Windows `%LOCALAPPDATA%\xdl\config.toml`, Linux `~/.config/xdl/config.toml`.
(platformdirs v4 nests author+name by default; `appauthor=False` keeps a single-level
dir shared with `state.db` — DECISIONS.md #16. `StorageCfg.db_path` overrides the state
db.) A missing file is fine; unknown keys warn instead of failing. A commented
template lives at `docs/config-template.toml`.

```toml
# see docs/config-template.toml for the fully commented version
[storage]
root = "D:/Media/x"          # default: ./downloads
layout = "by-author"         # by-author | flat | by-date
filename_template = "{id}"    # vars: {id} {date} {author} {index} {slug}

[resolver]
primary = "syndication"
fallback = "auto"

[engine]
concurrency = 3
resolve_concurrency = 4
retries = 5
delay = 0                    # 0 = jittered 0.2-0.5s

[hls]
ffmpeg_timeout_min = 30
```

Env overrides: `XDL_OUTPUT`, `XDL_CONCURRENCY`, `XDL_RESOLVE_CONCURRENCY`,
`XDL_FALLBACK`, ... plus the fully mechanical mapping
(`XDL_` + `__` for section nesting: `XDL_ENGINE__RETRIES`). Empty values are
ignored. Unknown keys (file or env) warn and are dropped — a stale template must
not brick the CLI.

`xdl config` prints the **resolved** settings (flag+env+file+defaults merged) plus the
config-file and state-db paths — one-command debugging of "why did it save THERE".
Per-value source annotation is not implemented.

## Settings model

```python
class Settings(BaseModel):
    storage: StorageCfg = StorageCfg()
    resolver: ResolverCfg = ResolverCfg()
    engine: EngineCfg = EngineCfg()
    hls: HlsCfg = HlsCfg()

    @classmethod
    def load(cls, overrides: dict) -> "Settings": ...   # TOML -> env -> overrides
```

One `Settings` instance constructed in `cli.main`, injected into `run_batch`; nothing
reads files/env past that point (testability).

## Logging

There is no logging subsystem. `report/logging.py`, `LogCfg`, and the `-v/-vv` flags
were deleted: the handlers were installed but no module ever emitted a log record, so
the flags and the "always-on rotating file log" were theater. Re-add logging only
together with real call sites (429 pause, retry give-up, `.part` resume offset, breaker
open) — see DECISIONS.md "Deliberately not built".

## Shell completion

`typer` gives it free (`xdl --install-completion`); document in README — nice for
`--quality <tab>`.
