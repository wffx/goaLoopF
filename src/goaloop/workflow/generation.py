"""Generation loop mixin: model call, static policy, compile/fuzz/coverage, decision."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from .. import coverage as coverage_module
from ..driver import DriverUnavailable, GenerationFailure
from ..models import (
    CoverageMetrics,
    ExecutionDisposition,
    GeneratedArtifactSet,
    GenerationFeedback,
    HarnessExecutionResult,
    LoopStage,
    Phase,
    ProcessResult,
    RunContext,
    TerminalStatus,
)
from ..validation import (
    ArtifactPolicyError,
    assemble_cmake_build_request,
    assemble_cmake_configure_request,
    assemble_compile_request,
    assemble_fuzz_request,
    decide_generation,
    make_execution_result,
    parse_libfuzzer_metrics,
    validate_generated_artifacts,
)

if TYPE_CHECKING:
    from ._base import ControllerState

__all__ = ["GenerationMixin"]


class GenerationMixin:
    """One full candidate iteration: generate -> validate -> execute -> decide."""

    last_execution: HarnessExecutionResult | None
    _loop_hashes: dict[str, dict[str, str]]
    _first_compile_success: bool | None
    _time_to_bug: float | None

    def _generation_step(self: ControllerState) -> None:
        assert (
            self.state is not None and self.goal is not None and self.preprocess is not None and self.store is not None
        )
        loop = self.state.generation_loop + 1
        if loop > self.request.max_generation_loops:
            self._terminate(
                TerminalStatus.FAILED,
                f"generation loop budget exhausted after {self.request.max_generation_loops} loop(s)",
            )
            return

        if self._recover_durable_loop(loop):
            if self.state.terminal_status is None:
                self._resume_execution_step()
            return

        self.state.active_loop = loop
        self.state.loop_stage = LoopStage.MODEL_GENERATION
        self._save_checkpoint()

        self._event(
            "generation:model_started",
            {"loop": loop, "max_loops": self.request.max_generation_loops},
        )
        try:
            artifacts = self.driver.generate_artifacts(
                goal=self.goal,
                preprocess=self.preprocess,
                feedback=self.goal.latest_feedback,
            )
        except DriverUnavailable as exc:
            self._event("generation:driver_unavailable", {"loop": loop, "reason": str(exc)})
            self._terminate(TerminalStatus.BLOCKED, f"generation driver unavailable: {exc}")
            return
        except GenerationFailure as exc:
            self._event("generation:model_invalid", {"loop": loop, "reason": str(exc)})
            self._terminate(TerminalStatus.FAILED, f"model output stayed invalid: {exc}")
            return

        self._event("generation:model_completed", {"loop": loop, "files": len(artifacts.files)})
        self._event("generation:validation_started", {"loop": loop})
        try:
            validate_generated_artifacts(artifacts, self.profile)
        except ArtifactPolicyError as exc:
            self._record_loop_consumed(loop)
            summary = f"generated artifacts violate controller policy: {exc}"
            self._event("generation:policy_rejected", {"loop": loop, "reason": summary})
            self.goal.latest_feedback = GenerationFeedback(
                category="policy",
                summary=summary,
                artifact_hashes=self._candidate_hashes(self.store.iterations_dir / f"loop-{loop:02d}"),
            )
            if loop >= self.request.max_generation_loops:
                self._terminate(
                    TerminalStatus.FAILED,
                    "generated artifacts repeatedly violated controller policy",
                )
            return

        self._event("generation:validation_completed", {"loop": loop})
        self._execute_candidate(artifacts, loop)

    def _resume_execution_step(self: ControllerState) -> None:
        assert self.state is not None and self.store is not None
        loop = self.state.active_loop
        if loop is None:
            self._terminate(TerminalStatus.FAILED, "execution checkpoint has no active loop")
            return
        execution_path = self._execution_path(loop)
        if self.state.loop_stage is LoopStage.EXECUTED or (
            self.state.loop_stage is LoopStage.EXECUTING and execution_path.is_file()
        ):
            if not execution_path.is_file():
                self._terminate(TerminalStatus.FAILED, f"execution checkpoint is missing {execution_path.name}")
                return
            execution = HarnessExecutionResult.model_validate_json(execution_path.read_text(encoding="utf-8"))
            self.last_execution = execution
            self._event("execution:checkpoint_resumed", {"loop": loop, "stage": LoopStage.EXECUTED.value})
            self._apply_execution_decision(execution, loop)
            return

        response_path = self._response_path(loop)
        candidate_dir = self._candidate_dir(loop)
        if not response_path.is_file() or not candidate_dir.is_dir():
            self._terminate(
                TerminalStatus.FAILED,
                f"execution checkpoint for loop {loop} is incomplete; candidate or response is missing",
            )
            return
        artifacts = GeneratedArtifactSet.model_validate_json(response_path.read_text(encoding="utf-8"))
        self._event(
            "execution:checkpoint_resumed",
            {"loop": loop, "stage": (self.state.loop_stage or LoopStage.MATERIALIZED).value},
        )
        self._execute_candidate(artifacts, loop, materialized=True)

    def _execute_candidate(
        self: ControllerState,
        artifacts: GeneratedArtifactSet,
        loop: int,
        *,
        materialized: bool = False,
    ) -> None:
        assert (
            self.state is not None and self.store is not None and self.preprocess is not None and self.goal is not None
        )
        if materialized:
            candidate_dir = self._candidate_dir(loop)
        else:
            candidate_dir = self.store.materialize_candidate(artifacts)
            self.state.active_loop = loop
            self.state.loop_stage = LoopStage.MATERIALIZED
        if self.state.phase is not Phase.HARNESS_EXECUTION:
            self._enter_phase(Phase.HARNESS_EXECUTION)
        if not materialized:
            self._event("execution:materialized", {"loop": loop}, phase=Phase.HARNESS_EXECUTION)
        self.state.loop_stage = LoopStage.EXECUTING
        self._save_checkpoint()
        binary_name = artifacts.endpoint_plan.build.binary_name
        self._loop_hashes[str(loop)] = self._candidate_hashes(candidate_dir.parent)
        context = RunContext(
            run_id=self.state.run_id,
            project_name=self.state.project_name,
            run_dir=self.store.run_dir,
            source_root=self.preprocess.source_root,
            candidate_dir=candidate_dir,
            binary_name=binary_name,
        )
        self.backend.prepare(context)

        build_library, build_include_dirs = self._cmake_build_if_requested(loop)
        if build_library is None and self.request.build_dir is not None:
            # cmake configure/build failed; terminal decided by the event above.
            return

        compile_request = assemble_compile_request(
            artifacts,
            self.profile,
            self.preprocess.source_root,
            candidate_dir,
            build_library=build_library,
            build_include_dirs=build_include_dirs,
        )
        self._event("execution:compile_started", {"loop": loop}, phase=Phase.HARNESS_EXECUTION)
        compile_result = self.backend.execute(compile_request)
        self._event(
            "execution:compile",
            {"loop": loop, "exit_code": compile_result.exit_code, "timed_out": compile_result.timed_out},
            phase=Phase.HARNESS_EXECUTION,
        )
        if loop == 1:
            self._first_compile_success = compile_result.exit_code == 0

        fuzz_result: ProcessResult | None = None
        coverage: CoverageMetrics | None = None
        coverage_valid = True
        if compile_result.exit_code == 0:
            fuzz_result, coverage, coverage_valid = self._run_fuzz_and_coverage(loop, candidate_dir, binary_name)

        execution = make_execution_result(
            run_id=self.state.run_id,
            generation_loop=loop,
            compile_result=compile_result,
            fuzz_result=fuzz_result,
            coverage=coverage,
            coverage_valid=coverage_valid,
            crash_artifact=self._first_crash_artifact(loop),
        )
        self.last_execution = execution
        self._persist_execution(execution)
        self.state.loop_stage = LoopStage.EXECUTED
        self._save_checkpoint()
        self._apply_execution_decision(execution, loop)

    def _apply_execution_decision(self: ControllerState, execution: HarnessExecutionResult, loop: int) -> None:
        assert self.state is not None and self.goal is not None and self.store is not None
        decision = decide_generation(execution, self.profile.coverage)
        self._event(
            "execution:decided",
            {
                "loop": loop,
                "disposition": execution.disposition.value,
                "decision": decision.disposition.value,
                "sanitizer": execution.sanitizer_kind,
                "reason": decision.reason,
                "cov_delta": execution.coverage.cov_delta,
                "feature_delta": execution.coverage.feature_delta,
                "corpus_delta": execution.coverage.corpus_delta,
                "execs_per_second": execution.coverage.execs_per_second,
                "target_function_hit": execution.coverage.target_function_hit,
                "target_line_coverage": execution.coverage.target_line_coverage,
            },
            phase=Phase.HARNESS_EXECUTION,
        )
        if decision.feedback is not None and decision.feedback.log_excerpt:
            decision.feedback = decision.feedback.model_copy(
                update={"log_excerpt": self._redacted_excerpt(decision.feedback.log_excerpt)}
            )
        self.goal.current_loop = loop
        self.goal.latest_feedback = decision.feedback
        if decision.completes_goal:
            self.goal.completed = True

        if decision.disposition is ExecutionDisposition.ACCEPTED:
            self._complete_loop(loop)
            self._terminate(TerminalStatus.HARNESS_VERIFIED, decision.reason)
            self._enter_phase(Phase.CRASH_ANALYSIS_REPORT)
            return
        if decision.disposition is ExecutionDisposition.NEEDS_REGENERATION:
            self._complete_loop(loop)
            if loop >= self.request.max_generation_loops:
                self._terminate(
                    TerminalStatus.FAILED,
                    f"generation loop budget exhausted after {loop} loop(s): {decision.reason}",
                )
            else:
                self._enter_phase(Phase.HARNESS_GENERATION)
            return
        if decision.disposition is ExecutionDisposition.ENVIRONMENT_ERROR:
            self.state.loop_stage = LoopStage.MATERIALIZED
            self._terminate(TerminalStatus.BLOCKED, decision.reason)
            return
        # crash_candidate
        self._complete_loop(loop)
        self._enter_phase(Phase.CRASH_ANALYSIS_REPORT)

    def _recover_durable_loop(self: ControllerState, loop: int) -> bool:
        assert self.state is not None and self.store is not None
        if self.state.active_loop is not None and self.state.active_loop != loop:
            self._terminate(
                TerminalStatus.FAILED,
                f"active loop {self.state.active_loop} conflicts with expected loop {loop}",
            )
            return True
        execution_path = self._execution_path(loop)
        candidate_dir = self._candidate_dir(loop)
        response_path = self._response_path(loop)
        if execution_path.is_file():
            self.state.active_loop = loop
            self.state.loop_stage = LoopStage.EXECUTED
        elif candidate_dir.is_dir() and response_path.is_file():
            self.state.active_loop = loop
            self.state.loop_stage = LoopStage.MATERIALIZED
        elif self.state.loop_stage not in (LoopStage.MATERIALIZED, LoopStage.EXECUTING, LoopStage.EXECUTED):
            return False
        self.state.phase = Phase.HARNESS_EXECUTION
        self._save_checkpoint()
        return True

    def _candidate_dir(self: ControllerState, loop: int) -> Path:
        assert self.store is not None
        return self.store.iterations_dir / f"loop-{loop:02d}" / "candidate"

    def _response_path(self: ControllerState, loop: int) -> Path:
        assert self.store is not None
        return self.store.iterations_dir / f"loop-{loop:02d}" / "response.json"

    def _execution_path(self: ControllerState, loop: int) -> Path:
        assert self.store is not None
        return self.store.run_dir / "executions" / f"loop-{loop:02d}" / "execution.json"

    def _complete_loop(self: ControllerState, loop: int) -> None:
        assert self.state is not None
        self.state.generation_loop = loop
        self.state.active_loop = None
        self.state.loop_stage = None

    def _cmake_build_if_requested(
        self: ControllerState,
        loop: int,
    ) -> tuple[Path | None, list[Path] | None]:
        """Configure and build the user CMake project, returning the library.

        Returns ``(None, None)`` when build-dir mode is not in use; on failure
        it records a blocked/regeneration event and returns ``(None, None)``
        while leaving the terminal decision to the caller.
        """
        build_dir = self.request.build_dir
        if build_dir is None or self.store is None:
            return None, None
        assert self.preprocess is not None
        build_dir = build_dir.resolve()
        build_root = build_dir / "goaloop-build"
        cmake = self.profile.tools.cmake
        timeout = self.profile.resources.timeout_seconds

        configure = assemble_cmake_configure_request(
            cmake=cmake,
            clang=self.profile.tools.clang,
            clangxx=self.profile.tools.clangxx,
            build_dir=build_dir,
            build_root=build_root,
            flags=self.profile.build.flags,
            timeout_seconds=timeout,
        )
        self._event(
            "execution:cmake_configure_started",
            {"loop": loop, "build_dir": str(build_dir)},
            phase=Phase.HARNESS_EXECUTION,
        )
        configure_result = self.backend.execute(configure)
        self._event(
            "execution:cmake_configure",
            {"loop": loop, "exit_code": configure_result.exit_code, "build_dir": str(build_dir)},
            phase=Phase.HARNESS_EXECUTION,
        )
        if configure_result.exit_code != 0:
            self._terminate(TerminalStatus.BLOCKED, f"cmake configure failed: {configure_result.stderr[-2000:]}")
            return None, None

        build_request = assemble_cmake_build_request(
            cmake=cmake,
            build_root=build_root,
            target=self.profile.build.target,
            timeout_seconds=timeout,
        )
        self._event("execution:cmake_build_started", {"loop": loop}, phase=Phase.HARNESS_EXECUTION)
        build_result = self.backend.execute(build_request)
        self._event(
            "execution:cmake_build",
            {"loop": loop, "exit_code": build_result.exit_code},
            phase=Phase.HARNESS_EXECUTION,
        )
        if build_result.exit_code != 0:
            self._terminate(TerminalStatus.BLOCKED, f"cmake build failed: {build_result.stderr[-2000:]}")
            return None, None

        library = self._find_build_library(build_root)
        if library is None:
            self._terminate(
                TerminalStatus.BLOCKED,
                "cmake build produced no static library; declare build.library in the profile",
            )
            return None, None
        include_dirs = [build_dir / item for item in self.profile.build.include_dirs]
        self._event(
            "execution:cmake_library",
            {"loop": loop, "library": str(library)},
            phase=Phase.HARNESS_EXECUTION,
        )
        return library, include_dirs

    def _find_build_library(self: ControllerState, build_root: Path) -> Path | None:
        declared = self.profile.build.library
        if declared:
            candidate = build_root / declared
            return candidate if candidate.is_file() else None
        archives = sorted(item for item in build_root.rglob("*.a") if item.is_file())
        return archives[0] if archives else None

    def _run_fuzz_and_coverage(
        self: ControllerState,
        loop: int,
        candidate_dir: Path,
        binary_name: str,
    ) -> tuple[ProcessResult, CoverageMetrics, bool]:
        assert self.store is not None and self.preprocess is not None
        loop_crashes = self.store.crashes_dir / f"loop-{loop:02d}"
        loop_crashes.mkdir(parents=True, exist_ok=True)
        fuzz_request = assemble_fuzz_request(
            binary=candidate_dir / binary_name,
            corpus_dir=self.store.corpus_dir,
            crashes_dir=loop_crashes,
            fuzz_seconds=self.request.fuzz_seconds,
            timeout_seconds=self.profile.resources.timeout_seconds,
        )
        profraw = self.store.coverage_dir / f"loop-{loop:02d}.profraw"
        fuzz_env = {"LLVM_PROFILE_FILE": str(profraw)}
        fuzz_request = fuzz_request.model_copy(update={"env": fuzz_env})
        self._event(
            "execution:fuzz_started",
            {"loop": loop, "seconds": self.request.fuzz_seconds},
            phase=Phase.HARNESS_EXECUTION,
        )
        fuzz_result = self.backend.execute(fuzz_request)
        self._event(
            "execution:fuzz",
            {
                "loop": loop,
                "exit_code": fuzz_result.exit_code,
                "timed_out": fuzz_result.timed_out,
                "duration": fuzz_result.duration_seconds,
            },
            phase=Phase.HARNESS_EXECUTION,
        )
        if fuzz_result.timed_out:
            self._time_to_bug = fuzz_result.duration_seconds

        metrics = parse_libfuzzer_metrics(f"{fuzz_result.stdout}\n{fuzz_result.stderr}")
        binary = candidate_dir / binary_name
        target_metrics = None
        self._event("execution:coverage_started", {"loop": loop}, phase=Phase.HARNESS_EXECUTION)
        try:
            target_metrics = coverage_module.measure_coverage(
                backend=self.backend,
                binary=binary,
                profraw=profraw,
                profdata=self.store.coverage_dir / f"loop-{loop:02d}.profdata",
                coverage_json=self.store.coverage_dir / f"coverage-{loop:02d}.json",
                source_root=self.preprocess.source_root,
                target_function=self.request.function,
                llvm_profdata=self.profile.tools.llvm_profdata,
                llvm_cov=self.profile.tools.llvm_cov,
                timeout_seconds=self.profile.resources.timeout_seconds,
            )
        except coverage_module.CoverageMeasurementError as exc:
            self._event(
                "execution:coverage",
                {"loop": loop, "ok": False, "detail": str(exc)},
                phase=Phase.HARNESS_EXECUTION,
            )
        else:
            self._event(
                "execution:coverage",
                {"loop": loop, "ok": True},
                phase=Phase.HARNESS_EXECUTION,
            )
        target_update: dict[str, object] = {}
        if target_metrics is not None:
            # Field-level merge: target_metrics only owns the target attribution
            # fields; a full model_dump() would overwrite libFuzzer counters with
            # their defaults (None/0).
            target_update = {
                "target_function_hit": target_metrics.target_function_hit,
                "target_line_coverage": target_metrics.target_line_coverage,
                "target_line_delta": target_metrics.target_line_delta,
            }
        coverage = metrics.model_copy(update=target_update)
        return fuzz_result, coverage, target_metrics is not None

    def _record_loop_consumed(self: ControllerState, loop: int) -> None:
        assert self.state is not None and self.goal is not None
        self.goal.current_loop = loop
        self._complete_loop(loop)
        self._save_checkpoint()

    def _first_crash_artifact(self: ControllerState, loop: int) -> str | None:
        if self.store is None or self.state is None or self.preprocess is None:
            return None
        crash_files = self._loop_crash_files(loop)
        return crash_files[0].name if crash_files else None

    def _loop_crash_files(self: ControllerState, loop: int) -> list[Path]:
        if self.store is None:
            return []
        loop_crashes = self.store.crashes_dir / f"loop-{loop:02d}"
        if not loop_crashes.is_dir():
            return []
        return sorted(
            item
            for item in loop_crashes.iterdir()
            if item.is_file() and (item.name.startswith("crash-") or item.name.startswith("timeout-"))
        )

    def _candidate_hashes(self: ControllerState, iteration_dir: Path) -> dict[str, str]:
        hashes_path = iteration_dir / "hashes.json"
        if not hashes_path.is_file():
            return {}
        try:
            data = json.loads(hashes_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _compiled_binary(self: ControllerState, execution: HarnessExecutionResult | None) -> Path | None:
        if execution is None:
            return None
        argv = execution.compile_result.argv
        for index, item in enumerate(argv):
            if item == "-o" and index + 1 < len(argv):
                return Path(argv[index + 1])
        return None
