"""Shared pytest fixtures: workspace, fixture repos, default profile, fake key."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from goaloop.models import SandboxSettings, ValidationProfile

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session", autouse=True)
def fake_api_key() -> None:
    """Preprocess requires DEEPSEEK_API_KEY presence; tests never call the model."""
    os.environ.setdefault("DEEPSEEK_API_KEY", "test-key-not-a-real-secret")
    yield
    os.environ.pop("DEEPSEEK_API_KEY", None)


@pytest.fixture()
def workspace_root(tmp_path: Path) -> Path:
    project_root = Path(__file__).parent.parent
    ws = tmp_path / "workspace"
    (ws / "repos").mkdir(parents=True)
    for project in ("safe", "fragile"):
        shutil.copytree(FIXTURES_DIR / "repos" / project, ws / "repos" / project)
    shutil.copytree(project_root / "profiles", ws / "profiles")
    shutil.copytree(project_root / "model-profiles", ws / "model-profiles")
    return ws


@pytest.fixture()
def default_profile() -> ValidationProfile:
    return ValidationProfile(name="default", sandbox=SandboxSettings(required=False))
