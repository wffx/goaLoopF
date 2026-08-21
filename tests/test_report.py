"""Report and redaction tests."""

from __future__ import annotations

from pathlib import Path

from goaloop.models import (
    CapabilityReport,
    FuzzRunRequest,
    GenerationGoal,
    Phase,
    PreprocessResult,
    RunState,
    TerminalStatus,
)
from goaloop.redaction import redact
from goaloop.report import build_research_metrics, build_validation_result, write_markdown_report


def _state(tmp_path: Path, status: TerminalStatus | None = None) -> RunState:
    return RunState(
        run_id="run-r",
        project_name="safe",
        request=FuzzRunRequest(source="repos/safe", function="safe_parse"),
        phase=Phase.CRASH_ANALYSIS_REPORT,
        terminal_status=status,
        goal=GenerationGoal(
            run_id="run-r",
            objective="o",
            target_function="safe_parse",
            acceptance_criteria=["a"],
            max_generation_loops=3,
            current_loop=1,
        ),
    )


def _preprocess() -> PreprocessResult:
    return PreprocessResult(
        run_id="run-r",
        ready=True,
        project_name="safe",
        source_root="/tmp/ws/repos/safe",
        language="c",
        target_function="safe_parse",
        contexts=[],
        candidate_signatures=["int safe_parse(const uint8_t*, size_t)"],
        capability_report=CapabilityReport(platform="Linux", capabilities=[]),
    )


class TestRedaction:
    def test_removes_credentials(self) -> None:
        text = "key=sk-abcdef1234567890 secret and DEEPSEEK_API_KEY=supersecretvalue"
        result = redact(text, Path("/tmp/ws"))
        assert "abcdef1234567890" not in result
        assert "supersecretvalue" not in result

    def test_removes_workspace_paths(self) -> None:
        text = "compiling /tmp/ws/repos/safe/src/safe.c failed at /tmp/ws/work/runs/x"
        result = redact(text, Path("/tmp/ws"))
        assert "/tmp/ws/repos" not in result
        assert "<workspace>" in result or "<path>" in result

    def test_removes_home(self) -> None:
        result = redact(f"file {Path.home()}/secret.txt", Path("/tmp/ws"))
        assert str(Path.home()) not in result

    def test_keeps_short_values(self) -> None:
        text = "summary=sk-a short token stays"
        result = redact(text, Path("/tmp/ws"))
        assert "sk-a short token stays" in result


class TestReport:
    def test_markdown_report_written(self, tmp_path: Path) -> None:
        state = _state(tmp_path, TerminalStatus.HARNESS_VERIFIED)
        path = write_markdown_report(
            run_dir=tmp_path,
            state=state,
            preprocess=_preprocess(),
            execution=None,
            crash_analysis=None,
            reason="candidate satisfied coverage policy",
        )
        assert path.name == "report.md"
        text = path.read_text(encoding="utf-8")
        assert "harness_verified" in text
        assert "safe_parse" in text
        assert "does not prove the product is bug-free" in text

    def test_validation_result_mapping(self, tmp_path: Path) -> None:
        state = _state(tmp_path, TerminalStatus.NEEDS_INPUT)
        validation = build_validation_result(
            state=state,
            reason="source missing",
            report_path="report.md",
        )
        assert validation.status is TerminalStatus.NEEDS_INPUT
        assert validation.generation_loops_used == 0

    def test_research_metrics(self, tmp_path: Path) -> None:
        state = _state(tmp_path, TerminalStatus.HARNESS_VERIFIED)
        metrics = build_research_metrics(
            state=state,
            preprocess=_preprocess(),
            model_profile_name="default",
            provider="deepseek-official",
            model="deepseek-v4-pro",
            prompt_version="v1",
            endpoint_label="deepseek-official",
            phase_durations={"preprocess": 0.1},
            format_retries=1,
            first_compile_success=True,
            time_to_bug_seconds=None,
        )
        assert metrics.run_id == "run-r"
        assert metrics.final_status is TerminalStatus.HARNESS_VERIFIED
        assert metrics.first_compile_success is True
        assert metrics.format_retries == 1
        assert metrics.token_source == "unavailable"
