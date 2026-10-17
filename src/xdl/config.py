"""Settings (pydantic) + TOML/env loading + precedence.

Precedence (highest wins): CLI overrides > env (XDL_*) > config.toml > defaults.
This module is a leaf: it imports nothing from `xdl`, so `models.py` can depend
on it freely.
"""

from __future__ import annotations

import os
import tomllib
import warnings
from pathlib import Path
from typing import Any

import platformdirs
from pydantic import BaseModel, Field

DEFAULT_LAYOUT = "by-author"
VALID_LAYOUTS = ("by-author", "flat", "by-date")

#: config.toml default location (per-platform; single-level app dir)
CONFIG_FILE_NAME = "config.toml"


class StorageCfg(BaseModel):
    root: Path = Path("downloads")  # cwd-relative default (spec'd)
    layout: str = DEFAULT_LAYOUT
    filename_template: str = "{id}"
    db_path: Path | None = None  # None -> <app_dir>/state.db (platformdirs)


class ResolverCfg(BaseModel):
    primary: str = "syndication"
    fallback: str = "auto"  # auto | ytdlp | fxtwitter | none


class EngineCfg(BaseModel):
    concurrency: int = Field(default=3, ge=1)
    resolve_concurrency: int = Field(default=4, ge=1)
    retries: int = Field(default=5, ge=1)
    delay: float = 0.0  # 0 = jittered 0.2-0.5s between resolves


class HlsCfg(BaseModel):
    ffmpeg_timeout_min: int = 30


class Settings(BaseModel):
    storage: StorageCfg = StorageCfg()
    resolver: ResolverCfg = ResolverCfg()
    engine: EngineCfg = EngineCfg()
    hls: HlsCfg = HlsCfg()

    @classmethod
    def load(
        cls,
        overrides: dict[str, Any] | None = None,
        *,
        config_path: Path | None = None,
        read_env: bool = True,
    ) -> Settings:
        """Merge defaults <- TOML <- env <- overrides into one Settings.

        A missing config.toml is fine; unknown keys in file/env produce a
        warning (never a crash - a stale template must not brick the CLI).
        """
        merged: dict[str, Any] = {}
        toml = _load_toml(config_path)
        _deep_merge(merged, _prune_unknown("config.toml", toml))
        if read_env:
            env = _load_env()
            _deep_merge(merged, _prune_unknown("env", env))
        if overrides:
            _deep_merge(merged, _nested(overrides))
        settings = cls(**merged)
        if settings.storage.layout not in VALID_LAYOUTS:
            msg = f"unknown layout {settings.storage.layout!r} (expected one of {VALID_LAYOUTS})"
            raise ValueError(msg)
        return settings

    @property
    def app_dir(self) -> Path:
        """Per-user app dir (config.toml and state.db live here).

        Windows: %LOCALAPPDATA%/xdl — platformdirs v4 nests author+name by
        default; appauthor=False keeps it single-level (DECISIONS.md #16).
        """
        return Path(platformdirs.user_config_dir("xdl", appauthor=False))

    @property
    def config_path(self) -> Path:
        return self.app_dir / CONFIG_FILE_NAME

    @property
    def db_path(self) -> Path:
        if self.storage.db_path is not None:
            return self.storage.db_path
        return self.app_dir / "state.db"


def _load_toml(path: Path | None) -> dict[str, Any]:
    """Read config.toml (tolerant of absence); unknown keys are warned, not
    rejected - the file lives in user hands and must stay forward-compatible."""
    if path is None:
        path = Settings().app_dir / CONFIG_FILE_NAME
    if not path.is_file():
        return {}
    with path.open("rb") as f:
        return tomllib.load(f)


#: doc'd convenience names beyond the mechanical XDL_<SECTION>__<FIELD> map
# (XDL_OUTPUT is special-cased to a Path below)
_ENV_ALIASES = {
    "XDL_CONCURRENCY": ("engine", "concurrency"),
    "XDL_RESOLVE_CONCURRENCY": ("engine", "resolve_concurrency"),
    "XDL_FALLBACK": ("resolver", "fallback"),
}


def _load_env() -> dict[str, Any]:
    """XDL_<SECTION>__<FIELD> env vars -> nested dict; XDL_OUTPUT and the
    other documented conveniences map mechanically too (docs/technical/07)."""
    out: dict[str, Any] = {}
    for key, value in os.environ.items():
        if not key.startswith("XDL_") or not value.strip():
            continue
        if key in _ENV_ALIASES:
            section, field = _ENV_ALIASES[key]
        else:
            parts = key[4:].lower().split("__")
            if len(parts) != 2:
                continue
            section, field = parts
        out.setdefault(section, {})[field] = _coerce(value)
    if raw := os.environ.get("XDL_OUTPUT"):
        out.setdefault("storage", {})["root"] = Path(raw)
    return out


def _prune_unknown(source: str, data: dict[str, Any]) -> dict[str, Any]:
    """Drop keys that do not exist on Settings (2 levels deep), warning once
    per key — a stale template or stray env var warns instead of bricking
    the CLI."""
    clean: dict[str, Any] = {}
    sections = Settings.model_fields
    for section, value in data.items():
        if section not in sections:
            warnings.warn(
                f"unknown config section '{section}' (source: {source})",
                UserWarning,
                stacklevel=3,
            )
            continue
        submodel = sections[section].annotation
        fields = getattr(submodel, "model_fields", None)
        if not isinstance(fields, dict) or not isinstance(value, dict):
            clean[section] = value
            continue
        keep: dict[str, Any] = {}
        for field, field_value in value.items():
            if field in fields:
                keep[field] = field_value
                continue
            warnings.warn(
                f"unknown config key '{section}.{field}' (source: {source})",
                UserWarning,
                stacklevel=3,
            )
        clean[section] = keep
    return clean


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
