"""ArtifactStore tests: containment, atomicity, events, candidate materialization."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from goaloop.models import GeneratedArtifactSet, GeneratedFile, Phase, RunEvent, RunState
from goaloop.storage import ArtifactStore

from .helpers import make_artifact_payload


def _payload() -> dict:
    return make_artifact_payload("safe", "safe_parse")


def _store(workspace_root: Path, run_id: str = "run-store-1") -> ArtifactStore:
    store = ArtifactStore(workspace_root, "safe", run_id)
    store.initialize()
    return store


def test_initialize_creates_dirs(workspace_root: Path) -> None:
    store = _store(workspace_root)
    assert store.run_dir.is_dir()
    assert store.iterations_dir.is_dir()
    assert store.crashes_dir.is_dir()
    assert store.private_session_dir.is_dir()


def test_custom_output_root_relocates_run_dir(workspace_root: Path, tmp_path: Path) -> None:
    out = tmp_path / "artifacts"
    store = ArtifactStore(workspace_root, "safe", "run-out-1", output_root=out)
    store.initialize()
    assert store.run_dir == out.resolve() / "safe" / "runs" / "run-out-1"
    assert store.run_dir.is_dir()
    # The private session record stays workspace-scoped regardless of output.
    assert store.private_session_dir == workspace_root / ".private-sessions" / "run-out-1"
    # Default output root is <workspace>/work.
    assert ArtifactStore(workspace_root, "safe", "x").run_dir == workspace_root / "work" / "safe" / "runs" / "x"


def test_events_are_append_only(workspace_root: Path) -> None:
    store = _store(workspace_root)
    store.append_event(RunEvent(sequence=1, phase=Phase.PREPROCESS, kind="a"))
    store.append_event(RunEvent(sequence=2, phase=Phase.PREPROCESS, kind="b"))
    lines = store.events_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["kind"] == "a"
    assert store.next_event_sequence() == 3


def test_state_roundtrip(workspace_root: Path) -> None:
    store = _store(workspace_root)
    state = RunState(
        run_id="run-store-1",
        project_name="safe",
        request={"source": "repos/safe", "function": "safe_parse"},
        goal={
            "run_id": "run-store-1",
            "objective": "o",
            "target_function": "f",
            "acceptance_criteria": [],
            "max_generation_loops": 5,
        },
    )
    store.save_state(state)
    loaded = store.load_state()
    assert loaded.run_id == state.run_id
    assert loaded.goal.objective == "o"


def test_materialize_candidate_and_hashes(workspace_root: Path) -> None:
    store = _store(workspace_root)
    artifacts = GeneratedArtifactSet.model_validate(
        {
            "run_id": "run-store-1",
            "generation_loop": 1,
            "summary": "s",
            "endpoint_plan": {
                "function": "safe_parse",
                "signature": "int f(const uint8_t*, size_t)",
                "location": "src/safe.c",
                "language": "c",
                "input_model": "bytes",
                "build": {"compiler": "clang", "harness_file": "harness_safe.c"},
            },
            "files": [
                {"path": "harness_safe.c", "content": "int main(){return 0;}", "purpose": "harness"},
                {"path": "Makefile", "content": "all:", "purpose": "review"},
                {"path": "build.sh", "content": "#!/bin/sh", "purpose": "review"},
                {"path": "endpoint.json", "content": "{}", "purpose": "review"},
                {"path": "README.fuzz.md", "content": "readme", "purpose": "review"},
            ],
        }
    )
    candidate = store.materialize_candidate(artifacts)
    assert candidate.is_dir()
    assert (candidate / "harness_safe.c").is_file()
    hashes = json.loads((candidate.parent / "hashes.json").read_text(encoding="utf-8"))
    assert hashes["harness_safe.c"]

    # Re-materializing the same loop must fail (never overwrite evidence).
    with pytest.raises(OSError):
        store.materialize_candidate(artifacts)


def test_escape_paths_rejected_by_schema() -> None:
    """The model contract itself rejects traversal before any file write."""
    with pytest.raises(ValidationError):
        GeneratedFile(path="../escape.c", content="x", purpose="escape")


def test_contained_rejects_escape(workspace_root: Path) -> None:
    store = _store(workspace_root)
    with pytest.raises(ValueError):
        ArtifactStore._contained(store.iterations_dir, "../escape.c")
