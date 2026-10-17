"""Logging setup (docs/technical/07 "Logging", implemented at M6).

- Console: a rich handler whose level comes from the CLI's `-v/-vv` (falls
  back to `settings.log.level`). Never level-gated below WARNING for the
  file's what-you-need-not-see content - the console stays clean, the file
  stays chatty.
- File: rotating (`RotatingFileHandler`, 5 MB x 3) at
  `<app_dir>/logs/xdl.log`, always-on INFO (independent of -v), plain ASCII
  formatter, the one writing 429/backoff/resume events.
- Redaction: a `logging.Filter` that replaces cookie-shaped values
  (`auth_token=...`, `ct0=...`) in the *file* record, belt & suspenders for
  the cookie phase that never ships by default (docs/06 policy).

Wiring: `cli.main()` calls `setup_logging(settings, verbose=<-v count>)`
(Agent A's lane). Idempotent: re-setup replaces handlers wholesale, so tests
can rebuild freely without stacking duplicates.

# TODO(A-merge): call site not landed yet on feat/core-2 when this shipped;
# recorded in COORDINATION.md (one-liner) so it lands with M6 core wiring.
"""

from __future__ import annotations

import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

from xdl.config import Settings

_FILE_MAX_BYTES = 5 * 1024 * 1024
_FILE_BACKUPS = 3
_FILE_NAME = "xdl.log"

_LEVEL_NAMES = {"debug": logging.DEBUG, "info": logging.INFO,
                "warning": logging.WARNING, "error": logging.ERROR,
                "critical": logging.CRITICAL}

#: cookie/auth-shaped query-and-header fragments that must never hit disk
_COOKIE_RE = re.compile(
    r"(?i)\b(auth_token|ct0|auth_multi_ext|guest_token|oauth_token|"
    r"authorization)\s*[=:]\s*[^\s;\"',&$<]+"
)

_logger = logging.getLogger("xdl")


class CookieRedactor(logging.Filter):
    """Replaces cookie-shaped values - even inside %-style formatted records -
    with `<redacted>` before anything is serialized to a handler."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.args:
            try:
                message = record.getMessage()
            except Exception:
                return True
        else:
            message = str(record.msg)
        redacted = _COOKIE_RE.sub(r"\1=<redacted>", message)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True


def setup_logging(
    settings: Settings,
    *,
    verbose: int = 0,
) -> logging.Logger:
    """Configure the `xdl` logger: rich console + always-on rotating file.

    `verbose`: the CLI's `-v` count (-v -> info, -vv -> debug; 0/None follows
    `settings.log.level`, falling back to `config.log.console_level`).
    Returns the configured root `xdl` logger.
    """
    level = _console_level(settings, verbose)
    handlers: list[logging.Handler] = [_console_handler(level)]

    file_handler = _file_handler(settings)
    if file_handler is not None:
        handlers.append(file_handler)

    # Wholesale replacement keeps repeated setup calls idempotent (tests,
    # config re-loads). Children inherit (propagate=True, their own handler
    # lists stay empty).
    for old in list(_logger.handlers):
        _logger.removeHandler(old)
        old.close()
    for handler in handlers:
        _logger.addHandler(handler)
    _logger.setLevel(logging.DEBUG)  # handlers gate their own levels
    _logger.propagate = False
    return _logger


def _console_level(settings: Settings, verbose: int) -> int:
    if verbose >= 2:
        return logging.DEBUG
    if verbose == 1:
        return logging.INFO
    raw = settings.log.console_level or settings.log.level
    return _LEVEL_NAMES.get(str(raw).lower(), logging.INFO)


def _console_handler(level: int) -> logging.Handler:
    from rich.logging import RichHandler

    handler: logging.Handler = RichHandler(rich_tracebacks=True, show_path=False)
    handler.setLevel(level)
    return handler


def _file_handler(settings: Settings) -> RotatingFileHandler | None:
    """Always-on INFO rotating log (5 MB x 3) at <log dir>/xdl.log.

    Log dir: `settings.log.dir` override, else `<app_dir>/logs` (same
    single-level app dir as state.db; platformdirs resolves via the Windows
    folder API on win32, so it cannot be redirected with env vars - tests use
    the `dir` override instead).
    """
    if not settings.log.file:
        return None
    logs_dir: Path = settings.log.dir or (settings.app_dir / "logs")
    try:
        logs_dir.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            logs_dir / _FILE_NAME,
            maxBytes=_FILE_MAX_BYTES,
            backupCount=_FILE_BACKUPS,
            encoding="utf-8",
        )
    except OSError:
        # An unwritable log dir must never take the run down; console keeps
        # working and the file simply skips this session.
        _logger.warning(
            "cannot open log file under %s - file logging disabled this session",
            logs_dir,
        )
        return None
    handler.setLevel(logging.INFO)
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s %(message)s", "%Y-%m-%dT%H:%M:%S"
        )
    )
    handler.addFilter(CookieRedactor())
    return handler


def redact(message: str) -> str:
    """Value-shape redaction helper (public for tests/inspection)."""
    return _COOKIE_RE.sub(r"\1=<redacted>", message)
