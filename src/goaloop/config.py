"""TOML profile loading and workspace configuration."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel

from .models import ModelProfile, ValidationProfile

ProfileT = TypeVar("ProfileT", bound=BaseModel)


class ProfileError(RuntimeError):
    """A requested profile is missing or invalid."""


def validation_profile_paths(workspace_root: Path) -> list[Path]:
    paths = [workspace_root / "profiles"]
    config_home = os.environ.get("XDG_CONFIG_HOME")
    if config_home:
        paths.append(Path(config_home) / "goaloop" / "profiles")
    else:
        paths.append(Path.home() / ".config" / "goaloop" / "profiles")
    return paths


def model_profile_paths(workspace_root: Path) -> list[Path]:
    paths = [workspace_root / "model-profiles"]
    config_home = os.environ.get("XDG_CONFIG_HOME")
    if config_home:
        paths.append(Path(config_home) / "goaloop" / "model-profiles")
    else:
        paths.append(Path.home() / ".config" / "goaloop" / "model-profiles")
    return paths


def load_validation_profile(name: str, workspace_root: Path) -> ValidationProfile:
    profile = _load_profile(name, validation_profile_paths(workspace_root), ValidationProfile)
    profile.default_include_dirs = [
        (workspace_root / item).resolve().as_posix() for item in profile.default_include_dirs
    ]
    return profile


def load_model_profile(name: str, workspace_root: Path) -> ModelProfile:
    profile = _load_profile(name, model_profile_paths(workspace_root), ModelProfile)
    if profile.cordis is not None and not profile.cordis.is_absolute():
        profile.cordis = (workspace_root / profile.cordis).resolve()
    return profile


def _load_profile(name: str, directories: list[Path], model: type[ProfileT]) -> ProfileT:
    if not name or "/" in name or "\\" in name or name in {".", ".."}:
        raise ProfileError(f"invalid profile name: {name!r}")
    attempted: list[str] = []
    for directory in directories:
        path = directory / f"{name}.toml"
        attempted.append(str(path))
        if not path.is_file():
            continue
        try:
            with path.open("rb") as handle:
                data: dict[str, Any] = tomllib.load(handle)
            data.setdefault("name", name)
            return model.model_validate(data)
        except (OSError, tomllib.TOMLDecodeError, ValueError) as exc:
            raise ProfileError(f"invalid profile {path}: {exc}") from exc
    raise ProfileError(f"profile {name!r} not found; searched: {', '.join(attempted)}")
