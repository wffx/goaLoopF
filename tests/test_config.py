"""Profile loading tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from goaloop.config import ProfileError, load_model_profile, load_validation_profile


def _write_profile(directory: Path, name: str, content: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.toml").write_text(content, encoding="utf-8")


def test_load_validation_profile(workspace_root: Path) -> None:
    profile = load_validation_profile("default", workspace_root)
    assert profile.name == "default"
    assert profile.backend == "local_linux"


def test_load_model_profile_resolves_cordis(tmp_path: Path) -> None:
    (tmp_path / "cordis").mkdir()
    _write_profile(
        tmp_path / "model-profiles",
        "dev",
        'name = "dev"\nprovider = "deepseek-official"\nmodel = "deepseek-v4-pro"\ncordis = "cordis/x.yml"\n',
    )
    profile = load_model_profile("dev", tmp_path)
    assert profile.cordis == (tmp_path / "cordis" / "x.yml").resolve()


def test_missing_profile_raises(tmp_path: Path) -> None:
    with pytest.raises(ProfileError):
        load_validation_profile("nope", tmp_path)


def test_invalid_profile_name_rejected(tmp_path: Path) -> None:
    for bad in ("../escape", "a/b", ".", "..", ""):
        with pytest.raises(ProfileError):
            load_validation_profile(bad, tmp_path)


def test_invalid_toml_raises(tmp_path: Path) -> None:
    _write_profile(tmp_path / "profiles", "broken", "name = [unclosed")
    with pytest.raises(ProfileError):
        load_validation_profile("broken", tmp_path)
