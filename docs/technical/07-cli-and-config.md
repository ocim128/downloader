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
  -v, --verbose              repeatable: -v info, -vv debug

Subcommands:
  xdl retry-failed           re-run items whose state status is failed/incomplete
  xdl db [info|rebuild|vacuum]   state store admin                        [planned]
  xdl config [path|edit]     show resolved config + effective defaults   [planned]
  xdl serve                  (M3) localhost web UI, 127.0.0.1 only
```

**Shipped (round 2, 2026-10-17).** `xdl [OPTIONS] URLS...` is the *default command*
(a single-command typer app + group dispatch in `main()` — deviation 11 in
COORDINATION.md: click group callbacks can't intersperse options after positionals AND
keep subcommands), and `xdl retry-failed` works. `xdl serve` runs today as
`python -m xdl.ui` (same app, CLI alias pending); `xdl config`/`xdl db` are planned.
A documented `config.toml` template lives at `docs/config-template.toml`.

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
  greppable-ish and the `-v` log has the full detail.
- **No color/TTY** (piped output): rich auto-degrades to plain lines; progress becomes
  per-item single log lines. CI-friendly.
- Encoding: UTF-8 output on Windows Terminal; set
  `sys.stdout.reconfigure(encoding="utf-8")` guard for legacy consoles.

## Config file

Location (as shipped): `platformdirs.user_config_dir("xdl", appauthor=False)` →
Windows `%LOCALAPPDATA%\xdl\config.toml`, Linux `~/.config/xdl/config.toml`.
(platformdirs v4 nests author+name by default; `appauthor=False` keeps a single-level
dir shared with `state.db` — deviation 7 in COORDINATION.md. `StorageCfg.db_path`
overrides the state db, `LogCfg.dir` the log dir.) A missing file is fine; unknown
keys warn instead of failing. A commented template lives at
`docs/config-template.toml`; `xdl config edit` (first run of edit only) will copy it —
[planned].

```toml
# see docs/config-template.toml for the fully commented version
[storage]
root = "D:/Media/x"          # default: ./downloads
layout = "by-author"         # by-author | flat | by-date
filename_template = "{id}"    # vars: {id} {date} {author} {index} {slug}

[resolver]
primary = "syndication"
fallback = "auto"
googlebot_retry = true

[engine]
concurrency = 3
resolve_concurrency = 4
retries = 5
delay = 0                    # 0 = jittered 0.2-0.5s
chunk_kib = 64

[hls]
ffmpeg_timeout_min = 30

[log]
file = true                  # rotating 5 MB x 3, always-on INFO
level = "info"
```

Env overrides: `XDL_OUTPUT`, `XDL_CONCURRENCY`, `XDL_RESOLVE_CONCURRENCY`,
`XDL_FALLBACK`, `XDL_LOG_LEVEL`, ... plus the fully mechanical mapping
(`XDL_` + `__` for section nesting: `XDL_ENGINE__RETRIES`). Empty values are
ignored. Unknown keys (file or env) warn and are dropped — a stale template must
not brick the CLI.

`xdl config` prints the **resolved** settings (flag+env+file+defaults merged, source of
each value annotated) — one-command debugging of "why did it save THERE"
[planned; see CLI surface note].

## Settings model

```python
class Settings(BaseModel):
    storage: StorageCfg = StorageCfg()
    resolver: ResolverCfg = ResolverCfg()
    engine: EngineCfg = EngineCfg()
    hls: HlsCfg = HlsCfg()
    log: LogCfg = LogCfg()

    @classmethod
    def load(cls, overrides: dict) -> "Settings": ...   # TOML -> env -> overrides
```

One `Settings` instance constructed in `cli.main`, injected into `run_batch`; nothing
reads files/env past that point (testability).

## Logging

- Rich handler for console (respect `-v`; implemented in `report/logging.py::setup_logging`),
  plain rotating `RotatingFileHandler` (5 MB × 3) at
  `%LOCALAPPDATA%/xdl/logs/xdl.log` (overridable via `LogCfg.dir`) always-on at INFO —
  the file is where 429/backoff/resume events go; console stays clean.
- Redaction guard in the file formatter: cookie-shaped values (`auth_token=...`,
  `ct0=...`) replaced even though cookies are phase 7 (belt & suspenders).

## Shell completion

`typer` gives it free (`xdl --install-completion`); document in README — nice for
`--quality <tab>`.
