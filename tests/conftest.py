"""Shared pytest fixtures: workspace, fixture repos, default profile, fake key."""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

import pytest

from goaloop import preprocess as preprocess_module
from goaloop.krepo import KRepoError, KRepoReport
from goaloop.models import SandboxSettings, ValidationProfile

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session", autouse=True)
def fake_api_key() -> None:
    """Preprocess requires DEEPSEEK_API_KEY presence; tests never call the model."""
    original_krepo = os.environ.get("GOALOOP_KREPO")
    os.environ.setdefault("DEEPSEEK_API_KEY", "test-key-not-a-real-secret")
    os.environ.setdefault("GOALOOP_KREPO", str(Path(__file__).parent.parent / "tools" / "kRepo"))
    yield
    os.environ.pop("DEEPSEEK_API_KEY", None)
    if original_krepo is None:
        os.environ.pop("GOALOOP_KREPO", None)
    else:
        os.environ["GOALOOP_KREPO"] = original_krepo


@pytest.fixture(autouse=True)
def fake_krepo_report(monkeypatch: pytest.MonkeyPatch) -> None:
    def _read(
        workspace_root: Path,
        repo_root: Path,
        source_file: Path,
        function: str,
    ) -> KRepoReport:
        del workspace_root, repo_root
        text = source_file.read_text(encoding="utf-8", errors="replace")
        match = None
        for candidate in re.finditer(rf"\b{re.escape(function)}\s*\(", text):
            body_start = text.find("{", candidate.end())
            declaration_end = text.find(";", candidate.end())
            if body_start >= 0 and (declaration_end < 0 or body_start < declaration_end):
                match = candidate
                break
        if match is None:
            raise KRepoError(f"test fixture could not extract {function}")
        body_start = text.find("{", match.end())
        depth = 0
        body_end = len(text)
        for index in range(body_start, len(text)):
            if text[index] == "{":
                depth += 1
            elif text[index] == "}":
                depth -= 1
                if depth == 0:
                    body_end = index + 1
                    break
        source_start = text.rfind("\n", 0, match.start()) + 1
        source = text[source_start:body_end]
        start_line = text.count("\n", 0, source_start) + 1
        end_line = start_line + source.count("\n")
        selected = {
            "id": 1,
            "name": function,
            "file": str(source_file),
            "start_line": start_line,
            "end_line": end_line,
        }
        tree = {"selected": selected, "functions": [selected], "edges": {}, "limits": {"truncated": False}}
        return KRepoReport(
            source=source,
            incoming_tree={**tree, "call_sites": []},
            outgoing_tree={**tree, "skipped_auxiliary_calls": []},
            selected_file=str(source_file),
            start_line=start_line,
            end_line=end_line,
        )

    monkeypatch.setattr(preprocess_module, "read_krepo_report", _read)


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
