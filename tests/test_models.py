"""Contract validation tests for the Pydantic models."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from goaloop.models import (
    SCHEMA_VERSION,
    BuildPlan,
    ExecutionDisposition,
    ExecutionLease,
    FuzzRunRequest,
    GeneratedArtifactSet,
    GeneratedFile,
    GenerationDecision,
    Language,
    PreprocessResult,
    TerminalStatus,
)


class TestFuzzRunRequest:
    def test_defaults(self) -> None:
        request = FuzzRunRequest(source="repos/x", function="parse")
        assert request.language is Language.AUTO
        assert request.max_generation_loops == 5
        assert request.fuzz_seconds == 600

    def test_loop_bounds(self) -> None:
        with pytest.raises(ValidationError):
            FuzzRunRequest(source="repos/x", function="parse", max_generation_loops=0)
        with pytest.raises(ValidationError):
            FuzzRunRequest(source="repos/x", function="parse", max_generation_loops=21)

    def test_function_symbol_pattern(self) -> None:
        with pytest.raises(ValidationError):
            FuzzRunRequest(source="repos/x", function="bad;symbol")
        with pytest.raises(ValidationError):
            FuzzRunRequest(source="repos/x", function="")


class TestBuildPlanSafety:
    def test_rejects_absolute_paths(self) -> None:
        with pytest.raises(ValidationError):
            BuildPlan(harness_file="/etc/passwd", target_sources=["/abs/path.c"])

    def test_rejects_parent_traversal(self) -> None:
        with pytest.raises(ValidationError):
            BuildPlan(harness_file="../escape.c")

    def test_rejects_backslashes(self) -> None:
        with pytest.raises(ValidationError):
            BuildPlan(harness_file="dir\\escape.c")

    def test_rejects_shell_metacharacters(self) -> None:
        with pytest.raises(ValidationError):
            BuildPlan(harness_file="h.c", cflags=["-D;rm -rf /"])
        with pytest.raises(ValidationError):
            BuildPlan(harness_file="h.c", ldflags=["$(evil)"])

    def test_extra_fields_rejected(self) -> None:
        with pytest.raises(ValidationError):
            BuildPlan(harness_file="h.c", sneaky="value")


class TestGeneratedArtifactSet:
    def _valid_files(self) -> list[dict]:
        return [
            {"path": "harness.c", "content": "int main(){}", "purpose": "harness"},
            {"path": "Makefile", "content": "all:", "purpose": "review"},
            {"path": "build.sh", "content": "#!/bin/sh", "purpose": "review"},
            {"path": "endpoint.json", "content": "{}", "purpose": "review"},
            {"path": "README.fuzz.md", "content": "readme", "purpose": "review"},
        ]

    def _plan(self) -> dict:
        return {
            "function": "parse",
            "signature": "int parse(const uint8_t *d, size_t s)",
            "location": "src/parse.c",
            "language": "c",
            "input_model": "bytes",
            "build": {"compiler": "clang", "harness_file": "harness.c"},
        }

    def test_roundtrip(self) -> None:
        artifacts = GeneratedArtifactSet.model_validate(
            {
                "run_id": "run-1",
                "generation_loop": 2,
                "summary": "ok",
                "endpoint_plan": self._plan(),
                "files": self._valid_files(),
            }
        )
        assert artifacts.schema_version == SCHEMA_VERSION
        assert artifacts.generation_loop == 2

    def test_duplicate_file_paths_rejected(self) -> None:
        files = self._valid_files()
        files.append(dict(files[0]))
        with pytest.raises(ValidationError):
            GeneratedArtifactSet.model_validate(
                {
                    "run_id": "run-1",
                    "generation_loop": 1,
                    "summary": "ok",
                    "endpoint_plan": self._plan(),
                    "files": files,
                }
            )

    def test_too_few_files_rejected(self) -> None:
        with pytest.raises(ValidationError):
            GeneratedArtifactSet.model_validate(
                {
                    "run_id": "run-1",
                    "generation_loop": 1,
                    "summary": "ok",
                    "endpoint_plan": self._plan(),
                    "files": self._valid_files()[:2],
                }
            )

    def test_wrong_phase_rejected(self) -> None:
        data = {
            "run_id": "run-1",
            "phase": "harness_execution",
            "generation_loop": 1,
            "summary": "ok",
            "endpoint_plan": self._plan(),
            "files": self._valid_files(),
        }
        with pytest.raises(ValidationError):
            GeneratedArtifactSet.model_validate(data)

    def test_generated_file_content_cap(self) -> None:
        with pytest.raises(ValidationError):
            GeneratedFile(path="big.c", content="x" * 1_000_001, purpose="harness")


class TestGenerationDecision:
    def test_only_accepted_completes_goal(self) -> None:
        with pytest.raises(ValidationError):
            GenerationDecision(
                disposition=ExecutionDisposition.NEEDS_REGENERATION,
                completes_goal=True,
                reason="nope",
            )

    def test_regeneration_requires_feedback(self) -> None:
        with pytest.raises(ValidationError):
            GenerationDecision(
                disposition=ExecutionDisposition.NEEDS_REGENERATION,
                completes_goal=False,
                reason="regen",
            )

    def test_valid_accepted(self) -> None:
        decision = GenerationDecision(
            disposition=ExecutionDisposition.ACCEPTED,
            completes_goal=True,
            reason="ok",
        )
        assert decision.completes_goal


class TestExecutionLease:
    def test_authorize_exact_executable(self) -> None:
        lease = ExecutionLease(
            allowed_executables=["/usr/bin/clang"],
            allowed_dirs=["/run"],
            timeout_seconds=10,
            max_commands=3,
        )
        assert lease.authorize(["/usr/bin/clang", "-v"])
        assert not lease.authorize(["/usr/bin/gcc"])

    def test_authorize_by_directory(self) -> None:
        lease = ExecutionLease(
            allowed_executables=[],
            allowed_dirs=["/tmp/work"],
            timeout_seconds=10,
            max_commands=3,
        )
        assert lease.authorize(["/tmp/work/candidate/fuzzer"])
        assert not lease.authorize(["/tmp/other/fuzzer"])

    def test_command_budget(self) -> None:
        lease = ExecutionLease(
            allowed_executables=["/bin/true"],
            allowed_dirs=[],
            timeout_seconds=10,
            max_commands=2,
        )
        assert lease.authorize(["/bin/true"])
        lease.commands_used = 2
        assert not lease.authorize(["/bin/true"])


class TestPreprocessResult:
    def test_ready_cannot_be_terminal(self) -> None:
        with pytest.raises(ValidationError):
            PreprocessResult(
                run_id="r",
                ready=True,
                project_name="p",
                source_root=Path("/x"),
                language=Language.C,
                target_function="f",
                capability_report={"platform": "Linux", "capabilities": []},
                terminal_status=TerminalStatus.NEEDS_INPUT,
            )

    def test_not_ready_requires_terminal(self) -> None:
        with pytest.raises(ValidationError):
            PreprocessResult(
                run_id="r",
                ready=False,
                project_name="p",
                source_root=Path("/x"),
                language=Language.C,
                target_function="f",
                capability_report={"platform": "Linux", "capabilities": []},
            )
