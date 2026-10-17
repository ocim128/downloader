"""CLI surface: default-command flags, subcommands (config/serve/retry),
mutual-exclusion validation. Uses typer's CliRunner with hermetic settings —
no network, no real runs (the run path itself is covered by integration
tests driving run_batch directly).
"""

from __future__ import annotations

import os

from typer.testing import CliRunner

from xdl.cli import app, solo

runner = CliRunner()

URL = "https://x.com/someuser/status/1750123456789012352"


def test_version_flag() -> None:
    result = runner.invoke(solo, ["--version"])
    assert result.exit_code == 0
    assert "xdl 0.1.0" in result.output


def test_audio_only_and_gif_are_mutually_exclusive(
    monkeypatch,
) -> None:
    # bail out before any network: conflict check precedes _execute
    monkeypatch.setattr("xdl.cli._execute", lambda **kw: None)
    result = runner.invoke(solo, ["--audio-only", "--gif", URL])
    assert result.exit_code == 2
    assert "not both" in result.output


def test_config_command_prints_resolved_settings(
    monkeypatch,
    tmp_path,
) -> None:
    for key in list(os.environ):
        if key.startswith("XDL_"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("XDL_OUTPUT", str(tmp_path / "dl"))
    result = runner.invoke(app, ["config"])
    assert result.exit_code == 0
    for section in ("[storage]", "[resolver]", "[engine]", "[hls]", "[log]"):
        assert section in result.output
    root_lines = [ln for ln in result.output.splitlines() if ln.startswith("root")]
    assert root_lines and "dl" in root_lines[0]  # env override won
    assert "state.db" in result.output


def test_serve_help_delegates_to_ui_runner() -> None:
    # argparse --help prints and exits before any server is built
    result = runner.invoke(app, ["serve", "--help"])
    assert result.exit_code == 0
    assert "xdl serve" in result.output
    assert "--no-open" in result.output


def test_help_lists_subcommands() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for name in ("retry-failed", "serve", "config"):
        assert name in result.output
