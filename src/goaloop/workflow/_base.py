"""Shared controller-state surface used by the RunController mixins.

The mixins (generation, report) access RunController state only through this
Protocol. It declares every attribute and method the mixins touch, including
the mixin-owned methods they call on each other.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from ..backend import ExecutionBackend
from ..driver import GenerationDriver
from ..models import (
    CoverageMetrics,
    CrashAnalysisResult,
    FuzzRunRequest,
    GeneratedArtifactSet,
    GenerationGoal,
    HarnessExecutionResult,
    ModelProfile,
    Phase,
    PreprocessResult,
    ProcessResult,
    ResearchMetrics,
    RunEvent,
    RunState,
    TerminalStatus,
    ValidationProfile,
)
from ..storage import ArtifactStore


class ControllerState(Protocol):
    """Everything the generation/report mixins may touch on the controller."""

    workspace_root: Path
    request: FuzzRunRequest
    profile: ValidationProfile
    driver: GenerationDriver
    backend: ExecutionBackend
    model_profile: ModelProfile | None
    resume: bool
    on_event: Callable[[RunEvent], None] | None

    state: RunState | None
    store: ArtifactStore | None
    preprocess: PreprocessResult | None
    goal: GenerationGoal | None
    last_execution: HarnessExecutionResult | None
    last_crash_analysis: CrashAnalysisResult | None

    _phase_started: float
    _phase_durations: dict[str, float]
    _loop_hashes: dict[str, dict[str, str]]
    _first_compile_success: bool | None
    _time_to_bug: float | None
    _resumed_run_id: str | None

    # lifecycle (RunController)
    def _event(self, kind: str, payload: dict[str, Any], *, phase: Phase | None = None) -> None: ...
    def _save_checkpoint(self) -> None: ...
    def _enter_phase(self, phase: Phase) -> None: ...
    def _terminate(self, status: TerminalStatus, reason: str) -> None: ...
    def _persist_execution(self, execution: HarnessExecutionResult) -> None: ...
    def _redacted_excerpt(self, excerpt: str | None) -> str | None: ...

    # generation mixin
    def _generation_step(self) -> None: ...
    def _resume_execution_step(self) -> None: ...
    def _execute_candidate(
        self, artifacts: GeneratedArtifactSet, loop: int, *, materialized: bool = False
    ) -> None: ...
    def _apply_execution_decision(self, execution: HarnessExecutionResult, loop: int) -> None: ...
    def _run_fuzz_and_coverage(
        self, loop: int, candidate_dir: Path, binary_name: str
    ) -> tuple[ProcessResult, CoverageMetrics, bool]: ...
    def _record_loop_consumed(self, loop: int) -> None: ...
    def _recover_durable_loop(self, loop: int) -> bool: ...
    def _candidate_dir(self, loop: int) -> Path: ...
    def _response_path(self, loop: int) -> Path: ...
    def _execution_path(self, loop: int) -> Path: ...
    def _complete_loop(self, loop: int) -> None: ...
    def _first_crash_artifact(self, loop: int) -> str | None: ...
    def _loop_crash_files(self, loop: int) -> list[Path]: ...
    def _candidate_hashes(self, iteration_dir: Path) -> dict[str, str]: ...
    def _compiled_binary(self, execution: HarnessExecutionResult | None) -> Path | None: ...
    def _cmake_build_if_requested(self, loop: int) -> tuple[Path | None, list[Path] | None]: ...
    def _find_build_library(self, build_root: Path) -> Path | None: ...

    # report mixin
    def _report_step(self) -> None: ...
    def _crash_analysis_if_needed(self) -> None: ...
    def _write_report(self) -> None: ...
    def _terminal_reason(self) -> str | None: ...
    def _write_metrics(self, report_path: Path) -> ResearchMetrics: ...
    def _last_candidate_dir(self) -> Path | None: ...
    def _empty_preprocess(self) -> PreprocessResult: ...
