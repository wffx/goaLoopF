"""Crash analysis + report mixin: terminal mapping, markdown report, metrics."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import TYPE_CHECKING

from ..crash import analyze_crash
from ..driver import PROMPT_VERSION
from ..models import (
    CapabilityReport,
    CrashAnalysisResult,
    ExecutionDisposition,
    GenerationFeedback,
    Phase,
    PreprocessResult,
    RunContext,
    TerminalStatus,
)
from ..report import (
    build_research_metrics,
    build_validation_result,
    write_markdown_report,
)

if TYPE_CHECKING:
    from ._base import ControllerState

__all__ = ["ReportMixin"]

CRASH_ANALYSIS_FILENAME = "crash-analysis.json"


class ReportMixin:
    """Crash analysis, terminal mapping, and report/metrics writing."""

    last_crash_analysis: CrashAnalysisResult | None
    _phase_started: float
    _phase_durations: dict[str, float]

    def _report_step(self: ControllerState) -> None:
        assert self.state is not None
        if self.state.terminal_status is None:
            self._crash_analysis_if_needed()
        if self.state.terminal_status is None:
            if self.state.phase is Phase.HARNESS_GENERATION:
                return  # crash was harness-owned; the run continues generating
            self._terminate(TerminalStatus.FAILED, "report phase reached without a terminal status")
        self._write_report()

    def _crash_analysis_if_needed(self: ControllerState) -> None:
        if self.last_execution is None or self.last_execution.disposition is not ExecutionDisposition.CRASH_CANDIDATE:
            return
        if self.last_crash_analysis is not None:
            return
        assert self.store is not None and self.preprocess is not None
        assert self.state is not None and self.goal is not None
        loop = self.state.generation_loop
        candidate_dir = self._last_candidate_dir()
        fuzzer_binary = self._compiled_binary(self.last_execution) or (
            self.backend.collect(
                RunContext(
                    run_id=self.state.run_id,
                    project_name=self.state.project_name,
                    run_dir=self.store.run_dir,
                    source_root=self.preprocess.source_root,
                    candidate_dir=candidate_dir,
                )
            ).fuzzer_binary
        )
        crash_files = self._loop_crash_files(loop)
        if fuzzer_binary is None:
            self._terminate(TerminalStatus.BLOCKED, "crash candidate has no fuzzer binary to analyze")
            return
        fuzz_output = ""
        if self.last_execution.fuzz_result is not None:
            fuzz_output = f"{self.last_execution.fuzz_result.stdout}\n{self.last_execution.fuzz_result.stderr}"
        self.last_crash_analysis = analyze_crash(
            source_root=self.preprocess.source_root,
            candidate_dir=candidate_dir or self.store.run_dir,
            run_dir=self.store.run_dir,
            fuzzer_binary=fuzzer_binary,
            crash_files=crash_files,
            output=fuzz_output,
            profile=self.profile,
            backend=self.backend,
            target_function=self.request.function,
        )
        self.store.write_json(self.store.run_dir / CRASH_ANALYSIS_FILENAME, self.last_crash_analysis)
        self._event(
            "crash:analysis",
            {
                "ownership": self.last_crash_analysis.ownership.value,
                "sanitizer": self.last_crash_analysis.sanitizer_kind,
                "reproductions": self.last_crash_analysis.reproductions,
            },
        )
        analysis = self.last_crash_analysis
        if analysis.ownership.value == "product":
            self._terminate(TerminalStatus.BUG_REPRODUCED, analysis.reason)
            return
        if analysis.ownership.value == "unknown":
            self._terminate(TerminalStatus.NEEDS_REVIEW, analysis.reason)
            return
        # harness self-error: return to generation only while budget remains
        if self.state.generation_loop < self.request.max_generation_loops:
            self.goal.latest_feedback = GenerationFeedback(
                category="harness_error",
                summary=f"crash was caused by harness code: {analysis.reason}",
                log_excerpt=self._redacted_excerpt(analysis.stack_excerpt),
            )
            self.last_crash_analysis = None
            (self.store.run_dir / CRASH_ANALYSIS_FILENAME).unlink(missing_ok=True)
            self._save_checkpoint()
            self._enter_phase(Phase.HARNESS_GENERATION)
            return
        self._terminate(TerminalStatus.FAILED, f"harness self-error without loop budget: {analysis.reason}")

    def _write_report(self: ControllerState) -> None:
        assert self.state is not None and self.store is not None
        status = self.state.terminal_status or TerminalStatus.FAILED
        reason = status.value
        if status is TerminalStatus.HARNESS_VERIFIED and self.last_execution is not None:
            reason = self.last_execution.reason
        if self.last_crash_analysis is not None:
            reason = self.last_crash_analysis.reason
        if self.preprocess is not None and self.preprocess.reason and self.last_execution is None:
            reason = self.preprocess.reason
        if reason == status.value:
            # Keep the specific reason recorded at terminate time (e.g. "loop
            # budget exhausted after 3 loop(s): ...") instead of the bare
            # status word.
            reason = self._terminal_reason() or reason

        report_path = write_markdown_report(
            run_dir=self.store.run_dir,
            state=self.state,
            preprocess=self.preprocess or self._empty_preprocess(),
            execution=self.last_execution,
            crash_analysis=self.last_crash_analysis,
            reason=reason,
        )
        validation = build_validation_result(
            state=self.state,
            reason=reason,
            execution=self.last_execution,
            crash_analysis=self.last_crash_analysis,
            report_path=report_path.relative_to(self.store.run_dir).as_posix(),
        )
        self.store.write_json(self.store.run_dir / "validation.json", validation)
        self.state.validation_result_path = "validation.json"
        self._write_metrics(report_path)
        self._event(
            "report:written",
            {
                "status": status.value,
                "report": report_path.relative_to(self.store.run_dir).as_posix(),
            },
        )
        self._save_checkpoint()

    def _terminal_reason(self: ControllerState) -> str | None:
        """Read the reason recorded by the last run:terminal event."""
        if self.store is None:
            return None
        events_path = self.store.events_path
        if not events_path.is_file():
            return None
        for line in reversed(events_path.read_text(encoding="utf-8").splitlines()):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("kind") == "run:terminal":
                reason = event.get("payload", {}).get("reason")
                return str(reason) if reason else None
        return None

    def _write_metrics(self: ControllerState, report_path: Path) -> None:
        assert self.state is not None and self.store is not None
        elapsed = time.monotonic() - self._phase_started
        durations = dict(self._phase_durations)
        durations["crash_analysis_report"] = round(elapsed, 3)
        provider = self.model_profile.provider if self.model_profile is not None else "deepseek-official"
        model = self.model_profile.model if self.model_profile is not None else "deepseek-v4-pro"
        metrics = build_research_metrics(
            state=self.state,
            preprocess=self.preprocess or self._empty_preprocess(),
            model_profile_name=self.request.model_profile,
            provider=provider,
            model=model,
            prompt_version=PROMPT_VERSION,
            endpoint_label=provider,
            phase_durations=durations,
            format_retries=self.driver.format_retries if hasattr(self.driver, "format_retries") else 0,
            first_compile_success=self._first_compile_success,
            time_to_bug_seconds=self._time_to_bug,
        )
        metrics = metrics.model_copy(update={"loop_hashes": self._loop_hashes})
        self.store.write_json(self.store.run_dir / "research-metrics.json", metrics)

    def _last_candidate_dir(self: ControllerState) -> Path | None:
        if self.store is None or self.state is None:
            return None
        iterations = self.store.iterations_dir / f"loop-{self.state.generation_loop:02d}" / "candidate"
        return iterations if iterations.is_dir() else None

    def _empty_preprocess(self: ControllerState) -> PreprocessResult:
        # Fallback only used in the report phase of a terminal run whose
        # preprocess.json is missing (e.g. an interrupted resume); the contract
        # requires a non-ready result to carry a terminal_status.
        assert self.state is not None
        return PreprocessResult(
            run_id=self.state.run_id,
            ready=False,
            project_name=self.state.project_name,
            source_root=self.workspace_root / "repos" / self.state.project_name,
            language=self.request.language,
            target_function=self.request.function,
            capability_report=CapabilityReport(platform="unknown", capabilities=[]),
            terminal_status=TerminalStatus.FAILED,
            reason="preprocess result unavailable",
        )
