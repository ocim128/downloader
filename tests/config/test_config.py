"""Agent B (M6) config precedence tests — lives beside src/xdl/config.py.

tests/unit/** is Agent A's lane per the ownership map; these new files cover
the config/logging modules B owns (no edits to A's files).
"""

from __future__ import annotations

import warnings
from pathlib import Path

import pytest

from xdl.config import VALID_LAYOUTS, Settings


def test_absent_file_and_env_gives_defaults() -> None:
    s = Settings.load(read_env=False)
    assert s.engine.concurrency == 3
    assert s.resolver.fallback == "auto"
    assert s.storage.filename_template == "{id}"
    assert s.storage.layout in VALID_LAYOUTS


def test_toml_only(tmp_path: Path) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        '[engine]\nretries = 7\n\n[storage]\nroot = "D:/dl"\nlayout = "flat"\n'
        'filename_template = "{date}_{id}"\n',
        encoding="utf-8",
    )
    s = Settings.load(read_env=False, config_path=cfg)
    assert s.engine.retries == 7
    assert s.storage.layout == "flat"
    assert s.storage.filename_template == "{date}_{id}"  # old default still settable


def test_env_beats_toml(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text('[log]\nlevel = "debug"\n', encoding="utf-8")
    monkeypatch.setenv("XDL_LOG_LEVEL", "warning")  # env alias (docs/07)
    monkeypatch.setenv("XDL_ENGINE__RETRIES", "2")  # mechanical nesting
    s = Settings.load(read_env=True, config_path=cfg)
    assert s.log.level == "warning"
    assert s.engine.retries == 2


def test_cli_overrides_beat_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDL_CONCURRENCY", "5")
    s = Settings.load({"engine.concurrency": 2})
    assert s.engine.concurrency == 2
    s2 = Settings.load()
    assert s2.engine.concurrency == 5


def test_unknown_keys_warn_and_do_not_brick(tmp_path: Path) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        '[engine]\nconcurrency = 4\nmagic = true\n\n[future]\nx = 1\n',
        encoding="utf-8",
    )
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        s = Settings.load(read_env=False, config_path=cfg)
    texts = [str(w.message) for w in caught]
    assert s.engine.concurrency == 4
    assert any("engine.magic" in t for t in texts)
    assert any("section 'future'" in t for t in texts)


def test_env_alias_resolve_concurrency(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDL_RESOLVE_CONCURRENCY", "9")
    monkeypatch.setenv("XDL_FALLBACK", "fxtwitter")
    s = Settings.load()
    assert s.engine.resolve_concurrency == 9
    assert s.resolver.fallback == "fxtwitter"


def test_bad_layout_rejected() -> None:
    with pytest.raises(ValueError, match="unknown layout"):
        Settings.load({"storage.layout": "holographic"})


def test_toml_then_env_then_override_same_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text('[engine]\nretries = 1\n', encoding="utf-8")
    monkeypatch.setenv("XDL_ENGINE__RETRIES", "3")
    s = Settings.load({"engine.retries": 5}, config_path=cfg)
    assert s.engine.retries == 5
    s_file_env = Settings.load(config_path=cfg)
    assert s_file_env.engine.retries == 3
