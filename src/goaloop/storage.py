"""Durable append-only run state and artifact storage."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from .models import GeneratedArtifactSet, RunEvent, RunState


class ArtifactStore:
    """Owns writes below one run directory and never writes into repos/."""

    def __init__(
        self,
        workspace_root: Path,
        project_name: str,
        run_id: str,
        output_root: Path | None = None,
    ) -> None:
        self.workspace_root = workspace_root.resolve()
        # Output root holding run products: default <workspace>/work, or the
        # user-provided --output directory (e.g. an external disk).
        self.output_root = (output_root or self.workspace_root / "work").resolve()
        self.run_dir = self.output_root / project_name / "runs" / run_id
        self.iterations_dir = self.run_dir / "iterations"
        self.logs_dir = self.run_dir / "logs"
        self.coverage_dir = self.run_dir / "coverage"
        self.corpus_dir = self.run_dir / "corpus"
        self.crashes_dir = self.run_dir / "crashes"
        self.private_session_dir = self.workspace_root / ".private-sessions" / run_id
        self.state_path = self.run_dir / "state.json"
        self.events_path = self.run_dir / "events.jsonl"

    def initialize(self) -> None:
        for path in (
            self.iterations_dir,
            self.logs_dir,
            self.coverage_dir,
            self.corpus_dir,
            self.crashes_dir,
            self.private_session_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)

    def save_state(self, state: RunState) -> None:
        self.write_json(self.state_path, state)

    def load_state(self) -> RunState:
        return RunState.model_validate_json(self.state_path.read_text(encoding="utf-8"))

    def append_event(self, event: RunEvent) -> None:
        self.events_path.parent.mkdir(parents=True, exist_ok=True)
        serialized = event.model_dump_json() + "\n"
        with self.events_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())

    def next_event_sequence(self) -> int:
        if not self.events_path.exists():
            return 1
        with self.events_path.open("r", encoding="utf-8") as handle:
            return sum(1 for line in handle if line.strip()) + 1

    def materialize_candidate(self, artifacts: GeneratedArtifactSet) -> Path:
        candidate_dir = self.iterations_dir / f"loop-{artifacts.generation_loop:02d}" / "candidate"
        candidate_dir.mkdir(parents=True, exist_ok=False)
        for generated in artifacts.files:
            destination = self._contained(candidate_dir, generated.path)
            destination.parent.mkdir(parents=True, exist_ok=True)
            self.write_text(destination, generated.content)
            if destination.name == "build.sh":
                destination.chmod(0o600)
        self.write_json(candidate_dir.parent / "response.json", artifacts)
        hashes = self.hash_tree(candidate_dir)
        self.write_json(candidate_dir.parent / "hashes.json", hashes)
        return candidate_dir

    def write_json(self, path: Path, value: BaseModel | dict[str, Any] | list[Any]) -> None:
        data = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
        content = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        self.write_text(path, content)

    def write_text(self, path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
            temporary = Path(handle.name)
        os.replace(temporary, path)

    def relative(self, path: Path) -> str:
        return path.resolve().relative_to(self.run_dir.resolve()).as_posix()

    @staticmethod
    def hash_tree(root: Path) -> dict[str, str]:
        hashes: dict[str, str] = {}
        for path in sorted(item for item in root.rglob("*") if item.is_file()):
            hashes[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
        return hashes

    @staticmethod
    def _contained(root: Path, relative: str) -> Path:
        root = root.resolve()
        destination = (root / relative).resolve()
        if not destination.is_relative_to(root):
            raise ValueError(f"artifact escapes candidate directory: {relative}")
        return destination


def create_run_id() -> str:
    """Return a sortable UTC run identifier with random collision resistance."""

    from datetime import UTC, datetime
    from uuid import uuid4

    return f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{uuid4().hex[:8]}"
