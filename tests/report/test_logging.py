"""Agent B (M6) logging tests — covers src/xdl/report/logging.py.

Windows note: platformdirs resolves app dirs via the Windows folder API, so
env redirection does not work; tests use the `LogCfg.dir` override instead
(same pattern as `StorageCfg.db_path`).
"""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

import pytest
from rich.logging import RichHandler

from xdl.config import LogCfg, Settings, StorageCfg
from xdl.report.logging import setup_logging


def _settings(tmp_path: Path, *, file: bool = True, level: str = "info") -> Settings:
    return Settings(
        storage=StorageCfg(root=tmp_path / "downloads", db_path=tmp_path / "state.db"),
        log=LogCfg(file=file, level=level, dir=tmp_path / "logs"),
    )


def _file_of(logger: logging.Logger) -> logging.handlers.RotatingFileHandler:
    handlers = [
        h for h in logger.handlers
        if isinstance(h, logging.handlers.RotatingFileHandler)
    ]
    (one,) = handlers
    return one


def _file_text(logger: logging.Logger) -> str:
    return Path(_file_of(logger).baseFilename).read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def _clean_handlers() -> None:
    root = logging.getLogger("xdl")
    for old in list(root.handlers):
        root.removeHandler(old)
    root.propagate = False


def test_setup_installs_console_and_file(tmp_path: Path) -> None:
    logger = setup_logging(_settings(tmp_path))
    assert any(isinstance(h, RichHandler) for h in logger.handlers)
    log_file = tmp_path / "logs" / "xdl.log"

    logger.info("hello file")
    for handler in logger.handlers:
        handler.flush()
    assert log_file.is_file()
    assert "hello file" in log_file.read_text(encoding="utf-8")


def test_file_level_is_always_on_info(tmp_path: Path) -> None:
    logger = setup_logging(_settings(tmp_path, level="warning"))
    assert _file_of(logger).level == logging.INFO


def test_verbose_escalates_console(tmp_path: Path) -> None:
    rich2 = next(
        h for h in setup_logging(_settings(tmp_path), verbose=2).handlers
        if isinstance(h, RichHandler)
    )
    assert rich2.level == logging.DEBUG
    rich1 = next(
        h for h in setup_logging(_settings(tmp_path), verbose=1).handlers
        if isinstance(h, RichHandler)
    )
    assert rich1.level == logging.INFO


def test_cookie_redaction(tmp_path: Path) -> None:
    logger = setup_logging(_settings(tmp_path))
    logger.info(
        "auth_token=SECRET0 ct0=SECRET1; ok=2 auth_multi_ext=SECRET2 oauth_token=SECRET3"
    )
    for handler in logger.handlers:
        handler.flush()
    text = _file_text(logger)
    assert "SECRET0" not in text and "SECRET1" not in text
    assert "SECRET2" not in text and "SECRET3" not in text
    assert "<redacted>" in text
    assert "ok=2" in text  # non-cookie values untouched


def test_redacter_handles_percent_format(tmp_path: Path) -> None:
    logger = setup_logging(_settings(tmp_path))
    logger.info("cookie %s leaked", "auth_token=SECRET9")
    for handler in logger.handlers:
        handler.flush()
    text = _file_text(logger)
    assert "SECRET9" not in text and "<redacted>" in text


def test_file_logging_disabled_when_configured(tmp_path: Path) -> None:
    logger = setup_logging(_settings(tmp_path, file=False))
    assert not any(isinstance(h, logging.FileHandler) for h in logger.handlers)


def test_setup_is_idempotent(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    first = setup_logging(s)
    count = len(first.handlers)
    second = setup_logging(s)
    assert second is first
    assert len(second.handlers) == count


def test_rotating_limits(tmp_path: Path) -> None:
    handler = _file_of(setup_logging(_settings(tmp_path)))
    assert handler.maxBytes == 5 * 1024 * 1024
    assert handler.backupCount == 3


def test_unwritable_logdir_does_not_crash(tmp_path: Path) -> None:
    blocked = tmp_path / "blocked"
    blocked.touch()  # occupies the dir path: mkdir under it must fail
    s = Settings(
        storage=StorageCfg(root=tmp_path / "downloads", db_path=tmp_path / "state.db"),
        log=LogCfg(file=True, dir=blocked),
    )
    logger = setup_logging(s)
    assert not any(isinstance(h, logging.FileHandler) for h in logger.handlers)
