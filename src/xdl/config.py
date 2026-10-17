"""Settings (pydantic) + TOML/env loading + precedence.

Precedence (highest wins): CLI overrides > env (XDL_*) > config.toml > defaults.
This module is a leaf: it imports nothing from `xdl`, so `models.py` can depend
on it freely.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

import platformdirs
from pydantic import BaseModel, Field

DEFAULT_LAYOUT = "by-author"
VALID_LAYOUTS = ("by-author", "flat", "by-date")


class StorageCfg(BaseModel):
    root: Path = Path("downloads")  # cwd-relative default (spec'd)
    layout: str = DEFAULT_LAYOUT
    filename_template: str = "{date}_{id}"


class ResolverCfg(BaseModel):
    primary: str = "syndication"
    fallback: str = "auto"  # auto | ytdlp | fxtwitter | none
    ytdlp_mode: str = "resolve-only"
    googlebot_retry: bool = True


class EngineCfg(BaseModel):
    concurrency: int = Field(default=3, ge=1)
    resolve_concurrency: int = Field(default=4, ge=1)
    retries: int = Field(default=5, ge=1)
    delay: float = 0.0  # 0 = jittered 0.2-0.5s between resolves
    chunk_kib: int = Field(default=64, ge=1)


class HlsCfg(BaseModel):
    ffmpeg_timeout_min: int = 30


class LogCfg(BaseModel):
    file: bool = True
    level: str = "info"


class Settings(BaseModel):
    storage: StorageCfg = StorageCfg()
    resolver: ResolverCfg = ResolverCfg()
    engine: EngineCfg = EngineCfg()
    hls: HlsCfg = HlsCfg()
    log: LogCfg = LogCfg()

    @classmethod
    def load(
        cls,
        overrides: dict[str, Any] | None = None,
        *,
        config_path: Path | None = None,
        read_env: bool = True,
    ) -> Settings:
        """Merge defaults <- TOML <- env <- overrides into one Settings."""
        merged: dict[str, Any] = {}
        if read_env or config_path:
            _deep_merge(merged, _load_toml(config_path))
        if read_env:
            _deep_merge(merged, _load_env())
        if overrides:
            _deep_merge(merged, _nested(overrides))
        settings = cls(**merged)
        if settings.storage.layout not in VALID_LAYOUTS:
            msg = f"unknown layout {settings.storage.layout!r} (expected one of {VALID_LAYOUTS})"
            raise ValueError(msg)
        return settings

    @property
    def app_dir(self) -> Path:
        """%APPDATA%/xdl on Windows (config.toml, state.db, logs/ live here)."""
        return Path(platformdirs.user_config_dir("xdl"))

    @property
    def db_path(self) -> Path:
        return self.app_dir / "state.db"


def _load_toml(path: Path | None) -> dict[str, Any]:
    if path is None:
        path = Settings().app_dir / "config.toml"
    if not path.is_file():
        return {}
    with path.open("rb") as f:
        return tomllib.load(f)


def _load_env() -> dict[str, Any]:
    """XDL_<SECTION>__<FIELD> env vars -> nested dict (XDL_OUTPUT special-cased)."""
    out: dict[str, Any] = {}
    for key, value in os.environ.items():
        if not key.startswith("XDL_") or key == "XDL_OUTPUT":
            continue
        parts = key[4:].lower().split("__")
        if len(parts) != 2:
            continue
        section, field = parts
        out.setdefault(section, {})[field] = _coerce(value)
    if output := os.environ.get("XDL_OUTPUT"):
        out.setdefault("storage", {})["root"] = Path(output)
    return out


def _coerce(raw: str) -> Any:
    if raw.lower() in ("true", "false"):
        return raw.lower() == "true"
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        return raw


def _nested(overrides: dict[str, Any]) -> dict[str, Any]:
    """CLI overrides arrive flat & typed (e.g. {'storage.root': Path}); nest them."""
    out: dict[str, Any] = {}
    for key, value in overrides.items():
        if value is None:
            continue
        cursor = out
        parts = key.split(".")
        for part in parts[:-1]:
            cursor = cursor.setdefault(part, {})
        cursor[parts[-1]] = value
    return out


def _deep_merge(base: dict[str, Any], extra: dict[str, Any]) -> None:
    """Merge section dicts without clobbering sibling keys (env+overrides both
    touch [storage])."""
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
