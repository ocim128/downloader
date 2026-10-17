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
  --audio-only               extract MP3 (ffmpeg) instead of video
  --gif                      convert gif-kind posts to real .gif (ffmpeg)
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
  xdl db [info|rebuild|vacuum]   state store admin
  xdl config [path|edit]     show resolved config + effective defaults
  xdl serve                  (M3) localhost web UI, 127.0.0.1 only
```

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

Location: `platformdirs.user_config_dir("xdl")` → Windows `%APPDATA%\xdl\config.toml`,
Linux `~/.config/xdl/config.toml`. Created with commented defaults by `xdl config edit`
(first run of edit only; the app never requires the file to exist).

```toml
# ~/.config/xdl/config.toml  (or %APPDATA%\xdl\config.toml)
[storage]
root = "D:/Media/x"          # default: ./downloads
layout = "by-author"         # by-author | flat | by-date
filename_template = "{date}_{id}"

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
file = true                  # rotating, next to state.db
level = "info"
```

Env overrides: `XDL_OUTPUT`, `XDL_CONCURRENCY`, `XDL_FALLBACK`, `XDL_LOG_LEVEL`, ...
mechanical mapping (`XDL_` + `__` for section nesting: `XDL_ENGINE__RETRIES`).

`xdl config` prints the **resolved** settings (flag+env+file+defaults merged, source of
each value annotated) — one-command debugging of "why did it save THERE".

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

- Rich handler for console (respect `-v`), plain rotating `RotatingFileHandler`
  (5 MB × 3) at `%APPDATA%/xdl/logs/xdl.log` always-on at INFO — the file is where
  429/backoff/resume events go; console stays clean.
- Redaction guard in the file formatter: cookie-shaped values (`auth_token=...`,
  `ct0=...`) replaced even though cookies are phase 7 (belt & suspenders).

## Shell completion

`typer` gives it free (`xdl --install-completion`); document in README — nice for
`--quality <tab>`.
