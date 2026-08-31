"""End-to-end workflow tests over real fixture repos with real clang.

The generation stage uses the ScriptedGenerationDriver (deterministic payloads);
the compile, fuzz, coverage and crash-analysis stages run real toolchain
commands through LocalLinuxBackend without bubblewrap.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from goaloop.backend import LocalLinuxBackend
from goaloop.driver import ScriptedGenerationDriver
from goaloop.models import (
    FuzzRunRequest,
    GenerationGoal,
    Language,
    LoopStage,
    Phase,
    ProcessResult,
    RunState,
    TerminalStatus,
    ValidationProfile,
)
from goaloop.storage import ArtifactStore
from goaloop.workflow import RunController

from .helpers import (
    BROKEN_HARNESS_TEMPLATE,
    HARNESS_CRASH_TEMPLATE,
    NO_REACH_TEMPLATE,
    make_artifact_payload,
)

FUZZ_SECONDS = 2


def _request(workspace: Path, *, source: str, function: str, loops: int = 3) -> FuzzRunRequest:
    return FuzzRunRequest(
        repo=source,
        source=".",
        function=function,
        language=Language.C,
        profile="default",
        model_profile="default",
        max_generation_loops=loops,
        fuzz_seconds=FUZZ_SECONDS,
    )


def _controller(
    workspace_root: Path,
    request: FuzzRunRequest,
    payloads: list[dict],
    *,
    run_id: str = "run-w",
    profile: ValidationProfile | None = None,
    interrupt_on_call: int | None = None,
    resume: bool = False,
) -> RunController:
    validation = profile or ValidationProfile(name="default", sandbox={"required": False})
    driver = ScriptedGenerationDriver(payloads, interrupt_on_call=interrupt_on_call)
    backend = LocalLinuxBackend(validation)
    return RunController(
        workspace_root=workspace_root,
        request=request,
        profile=validation,
        driver=driver,
        backend=backend,
        run_id=run_id,
        resume=resume,
    )


def _valid_payload(project: str, function: str) -> dict:
    return make_artifact_payload(project, function)


def _broken_payload(project: str, function: str) -> dict:
    return make_artifact_payload(
        project,
        function,
        harness_source=BROKEN_HARNESS_TEMPLATE.format(function=function),
        summary="harness with a compile error",
    )


def _harness_crash_payload(project: str, function: str) -> dict:
    return make_artifact_payload(
        project,
        function,
        harness_source=HARNESS_CRASH_TEMPLATE.format(function=function),
        summary="harness with a null dereference",
    )


def _run_dir(workspace_root: Path, run_id: str) -> Path:
    return ArtifactStore(workspace_root, "safe", run_id).run_dir


class TestSafeFixture:
    def test_harness_verified(self, workspace_root: Path) -> None:
        request = _request(workspace_root, source="repos/safe", function="safe_parse")
        controller = _controller(
            workspace_root,
            request,
            [_valid_payload("safe", "safe_parse")],
            run_id="run-safe-1",
        )
        state = controller.run()
        controller.close()
        assert state.terminal_status is TerminalStatus.HARNESS_VERIFIED
        assert state.generation_loop == 1
        assert state.goal.completed
        run_dir = _run_dir(workspace_root, "run-safe-1")
        assert (run_dir / "report.md").is_file()
        assert (run_dir / "validation.json").is_file()
        assert (run_dir / "research-metrics.json").is_file()
        assert (run_dir / "optimization-suggestions.json").is_file()
        assert (run_dir / "optimization-suggestions.md").is_file()
        optimization = json.loads((run_dir / "optimization-suggestions.json").read_text())
        assert optimization["suggestions"][0]["id"] == "validate-success-baseline"
        assert "## Optimization Suggestions" in (run_dir / "report.md").read_text(encoding="utf-8")
        execution = json.loads((run_dir / "executions" / "loop-01" / "execution.json").read_text())
        assert execution["disposition"] == "accepted"
        coverage = execution["coverage"]
        assert coverage["target_function_hit"] is True
        assert coverage["target_line_coverage"] is not None
        # libFuzzer counters must survive the target-metrics merge (regression:
        # a full model_dump() update used to zero them out).
        assert coverage["initial_cov"] is not None
        assert coverage["final_cov"] is not None
        assert coverage["execs_per_second"] is not None
        events = (run_dir / "events.jsonl").read_text(encoding="utf-8")
        assert "run:terminal" in events
        assert "harness_verified" in events
        assert "optimization:completed" in events

    def test_resume_terminal_run_refreshes_report(self, workspace_root: Path) -> None:
        request = _request(workspace_root, source="repos/safe", function="safe_parse")
        controller = _controller(
            workspace_root,
            request,
            [_valid_payload("safe", "safe_parse")],
            run_id="run-safe-2",
        )
        state = controller.run()
        controller.close()
        assert state.terminal_status is TerminalStatus.HARNESS_VERIFIED

        resumed = _controller(
            workspace_root,
            request,
            [],
            run_id="run-safe-2",
            resume=True,
        )
        state2 = resumed.run()
        resumed.close()
        assert state2.terminal_status is TerminalStatus.HARNESS_VERIFIED
        assert state2.generation_loop == 1  # evidence was not re-executed


class TestFragileFixture:
    def test_bug_reproduced(self, workspace_root: Path) -> None:
        request = _request(workspace_root, source="repos/fragile", function="fragile_parse", loops=2)
        controller = _controller(
            workspace_root,
            request,
            [_valid_payload("fragile", "fragile_parse")],
            run_id="run-fragile-1",
        )
        state = controller.run()
        controller.close()
        assert state.terminal_status is TerminalStatus.BUG_REPRODUCED
        assert state.generation_loop == 1
        run_dir = ArtifactStore(workspace_root, "fragile", "run-fragile-1").run_dir
        crashes = list((run_dir / "crashes" / "loop-01").glob("crash-*"))
        assert crashes, "expected a crash artifact to be saved"
        analysis = json.loads((run_dir / "crash-analysis.json").read_text(encoding="utf-8"))
        assert analysis["ownership"] == "product"
        assert analysis["reproductions"] == 3
        assert analysis["sanitizer_kind"] == "address"
        validation = json.loads((run_dir / "validation.json").read_text(encoding="utf-8"))
        assert validation["status"] == "bug_reproduced"


class TestRegeneration:
    def test_broken_then_fixed(self, workspace_root: Path) -> None:
        request = _request(workspace_root, source="repos/safe", function="safe_parse", loops=3)
        controller = _controller(
            workspace_root,
            request,
            [_broken_payload("safe", "safe_parse"), _valid_payload("safe", "safe_parse")],
            run_id="run-regen-1",
        )
        state = controller.run()
        controller.close()
        assert state.terminal_status is TerminalStatus.HARNESS_VERIFIED
        assert state.generation_loop == 2
        run_dir = ArtifactStore(workspace_root, "safe", "run-regen-1").run_dir
        optimization = json.loads((run_dir / "optimization-suggestions.json").read_text())
        suggestion_ids = {item["id"] for item in optimization["suggestions"]}
        assert "improve-first-pass-build-context" in suggestion_ids
        assert "reduce-generation-rework" in suggestion_ids
        events = (ArtifactStore(workspace_root, "safe", "run-regen-1").run_dir / "events.jsonl").read_text()
        assert "needs_regeneration" in events  # loop 1 evidence was fed back

    def test_no_reach_revises_via_coverage_feedback(self, workspace_root: Path) -> None:
        no_reach = make_artifact_payload(
            "safe",
            "safe_parse",
            harness_source=NO_REACH_TEMPLATE.format(function="safe_parse"),
            summary="compiles but never reaches the target",
        )
        request = _request(workspace_root, source="repos/safe", function="safe_parse", loops=3)
        controller = _controller(
            workspace_root,
            request,
            [no_reach, _valid_payload("safe", "safe_parse")],
            run_id="run-cover-1",
        )
        state = controller.run()
        controller.close()
        assert state.terminal_status is TerminalStatus.HARNESS_VERIFIED
        assert state.generation_loop == 2
        events = (ArtifactStore(workspace_root, "safe", "run-cover-1").run_dir / "events.jsonl").read_text()
        assert "needs_regeneration" in events

    def test_loop_budget_exhausted(self, workspace_root: Path) -> None:
        request = _request(workspace_root, source="repos/safe", function="safe_parse", loops=2)
        controller = _controller(
            workspace_root,
            request,
            [_broken_payload("safe", "safe_parse"), _broken_payload("safe", "safe_parse")],
            run_id="run-exhaust-1",
        )
        state = controller.run()
        controller.close()
        assert state.terminal_status is TerminalStatus.FAILED
        assert state.generation_loop == 2
        assert "budget" in _report_text(workspace_root, "run-exhaust-1")
        events_path = ArtifactStore(workspace_root, "safe", "run-exhaust-1").run_dir / "events.jsonl"
        events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
        assert any(event["kind"] == "generation:model_started" for event in events)
        assert any(event["kind"] == "execution:compile_started" for event in events)
        assert any(
            event["kind"] == "phase:enter" and event["payload"]["phase"] == "harness_execution"
            for event in events
        )
        assert sum(
            event["kind"] == "phase:enter" and event["payload"]["phase"] == "harness_generation"
            for event in events
        ) >= 2
        # validation.json must keep the specific reason, not the bare status
        validation = json.loads(
            (ArtifactStore(workspace_root, "safe", "run-exhaust-1").run_dir / "validation.json").read_text()
        )
        assert "budget exhausted" in validation["reason"]


def _report_text(workspace_root: Path, run_id: str) -> str:
    path = _run_dir(workspace_root, run_id) / "report.md"
    return path.read_text(encoding="utf-8") if path.is_file() else ""


class TestPolicy:
    def test_missing_required_file_fails_after_budget(self, workspace_root: Path) -> None:
        payload = _valid_payload("safe", "safe_parse")
        payload["files"] = [item for item in payload["files"] if item["path"] != "README.fuzz.md"]
        request = _request(workspace_root, source="repos/safe", function="safe_parse", loops=1)
        controller = _controller(workspace_root, request, [payload], run_id="run-policy-1")
        state = controller.run()
        controller.close()
        assert state.terminal_status is TerminalStatus.FAILED
        assert "policy" in _report_text(workspace_root, "run-policy-1")


class TestHarnessCrashRecovery:
    def test_harness_crash_returns_to_generation(self, workspace_root: Path) -> None:
        request = _request(workspace_root, source="repos/safe", function="safe_parse", loops=3)
        controller = _controller(
            workspace_root,
            request,
            [_harness_crash_payload("safe", "safe_parse"), _valid_payload("safe", "safe_parse")],
            run_id="run-harness-crash-1",
        )
        state = controller.run()
        controller.close()
        assert state.terminal_status is TerminalStatus.HARNESS_VERIFIED
        assert state.generation_loop == 2
        assert not (
            ArtifactStore(workspace_root, "safe", "run-harness-crash-1").run_dir / "crash-analysis.json"
        ).is_file()


class TestResume:
    def test_resume_continues_generation(self, workspace_root: Path) -> None:
        request = _request(workspace_root, source="repos/safe", function="safe_parse", loops=3)
        controller = _controller(
            workspace_root,
            request,
            [_broken_payload("safe", "safe_parse")],
            run_id="run-resume-1",
            interrupt_on_call=1,
        )
        with pytest.raises(RuntimeError, match="interrupted"):
            controller.run()
        controller.close()
        run_dir = _run_dir(workspace_root, "run-resume-1")
        state_path = run_dir / "state.json"
        assert state_path.is_file()
        persisted = json.loads(state_path.read_text(encoding="utf-8"))
        assert persisted["generation_loop"] == 1
        assert persisted["terminal_status"] is None

        resumed = _controller(
            workspace_root,
            request,
            [_valid_payload("safe", "safe_parse")],
            run_id="run-resume-1",
            resume=True,
        )
        state2 = resumed.run()
        resumed.close()
        assert state2.terminal_status is TerminalStatus.HARNESS_VERIFIED
        assert state2.generation_loop == 2

    def test_resume_reuses_materialized_candidate_after_execution_interrupt(self, workspace_root: Path) -> None:
        request = _request(workspace_root, source="repos/safe", function="safe_parse", loops=1)
        validation = ValidationProfile(name="default", sandbox={"required": False})

        class InterruptingBackend(LocalLinuxBackend):
            def execute(self, request):
                raise RuntimeError("execution interrupted")

        first_driver = ScriptedGenerationDriver([_valid_payload("safe", "safe_parse")])
        interrupted = RunController(
            workspace_root=workspace_root,
            request=request,
            profile=validation,
            driver=first_driver,
            backend=InterruptingBackend(validation),
            run_id="run-resume-materialized",
        )
        with pytest.raises(RuntimeError, match="execution interrupted"):
            interrupted.run()
        interrupted.close()

        run_dir = _run_dir(workspace_root, "run-resume-materialized")
        store = ArtifactStore(workspace_root, "safe", "run-resume-materialized")
        persisted = store.load_state()
        assert persisted.phase is Phase.HARNESS_EXECUTION
        assert persisted.active_loop == 1
        assert persisted.loop_stage is LoopStage.EXECUTING
        persisted.terminal_status = TerminalStatus.BLOCKED
        persisted.terminal_phase = Phase.HARNESS_EXECUTION
        persisted.phase = Phase.CRASH_ANALYSIS_REPORT
        store.save_state(persisted)

        class CompileFailureBackend(LocalLinuxBackend):
            def execute(self, request):
                return ProcessResult(
                    argv=request.argv,
                    exit_code=1,
                    duration_seconds=0.01,
                    stderr="synthetic compile failure",
                )

        resumed_driver = ScriptedGenerationDriver([])
        resumed = RunController(
            workspace_root=workspace_root,
            request=request,
            profile=validation,
            driver=resumed_driver,
            backend=CompileFailureBackend(validation),
            run_id="run-resume-materialized",
            resume=True,
        )
        state = resumed.run()
        resumed.close()

        assert resumed_driver.calls == 0
        assert state.generation_loop == 1
        assert state.active_loop is None
        assert state.loop_stage is None
        assert state.terminal_status is TerminalStatus.FAILED
        assert (run_dir / "iterations" / "loop-01" / "candidate").is_dir()
        events = (run_dir / "events.jsonl").read_text(encoding="utf-8")
        assert '"recovery_phase":"harness_execution"' in events
        assert "execution:checkpoint_resumed" in events

    def test_resume_applies_persisted_execution_without_rerunning_backend(self, workspace_root: Path) -> None:
        request = _request(workspace_root, source="repos/safe", function="safe_parse", loops=1)
        validation = ValidationProfile(name="default", sandbox={"required": False})

        class CompileFailureBackend(LocalLinuxBackend):
            def __init__(self) -> None:
                super().__init__(validation)
                self.calls = 0

            def execute(self, request):
                self.calls += 1
                return ProcessResult(
                    argv=request.argv,
                    exit_code=1,
                    duration_seconds=0.01,
                    stderr="synthetic compile failure",
                )

        class InterruptAfterExecutionController(RunController):
            def _apply_execution_decision(self, execution, loop):
                raise RuntimeError("interrupted after execution checkpoint")

        first_backend = CompileFailureBackend()
        interrupted = InterruptAfterExecutionController(
            workspace_root=workspace_root,
            request=request,
            profile=validation,
            driver=ScriptedGenerationDriver([_valid_payload("safe", "safe_parse")]),
            backend=first_backend,
            run_id="run-resume-executed",
        )
        with pytest.raises(RuntimeError, match="after execution checkpoint"):
            interrupted.run()
        interrupted.close()

        store = ArtifactStore(workspace_root, "safe", "run-resume-executed")
        persisted = store.load_state()
        assert persisted.generation_loop == 0
        assert persisted.active_loop == 1
        assert persisted.loop_stage is LoopStage.EXECUTED

        second_backend = CompileFailureBackend()
        resumed_driver = ScriptedGenerationDriver([])
        resumed = RunController(
            workspace_root=workspace_root,
            request=request,
            profile=validation,
            driver=resumed_driver,
            backend=second_backend,
            run_id="run-resume-executed",
            resume=True,
        )
        state = resumed.run()
        resumed.close()

        assert resumed_driver.calls == 0
        assert second_backend.calls == 0
        assert state.generation_loop == 1
        assert state.terminal_status is TerminalStatus.FAILED


class TestTerminalPaths:
    def test_needs_input_when_source_missing(self, workspace_root: Path) -> None:
        request = _request(workspace_root, source="repos/missing", function="f")
        controller = _controller(workspace_root, request, [], run_id="run-input-1")
        state = controller.run()
        controller.close()
        assert state.terminal_status is TerminalStatus.NEEDS_INPUT
        assert state.phase is Phase.CRASH_ANALYSIS_REPORT
        run_dir = ArtifactStore(workspace_root, "missing", "run-input-1").run_dir
        assert (run_dir / "report.md").is_file()
        optimization = json.loads((run_dir / "optimization-suggestions.json").read_text())
        assert optimization["suggestions"][0]["id"] == "fix-input-scope"

    def test_blocked_when_driver_unavailable(self, workspace_root: Path) -> None:
        request = _request(workspace_root, source="repos/safe", function="safe_parse")
        payloads = [_valid_payload("safe", "safe_parse")]
        controller = _controller(workspace_root, request, payloads, run_id="run-blocked-1")
        controller.driver.unavailable = True
        state = controller.run()
        controller.close()
        assert state.terminal_status is TerminalStatus.BLOCKED
        run_dir = ArtifactStore(workspace_root, "safe", "run-blocked-1").run_dir
        optimization = json.loads((run_dir / "optimization-suggestions.json").read_text())
        assert optimization["suggestions"][0]["id"] == "restore-runtime-prerequisites"


class TestSeedCorpus:
    def test_seed_corpus_copied_into_run(self, workspace_root: Path) -> None:
        seeds = workspace_root / "seeds"
        seeds.mkdir()
        (seeds / "seed1.json").write_text('{"a": 1}', encoding="utf-8")
        (seeds / "seed2.txt").write_bytes(b"\x01\x02\x03")
        request = _request(workspace_root, source="repos/safe", function="safe_parse", loops=2)
        request.seed_corpus = seeds
        controller = _controller(
            workspace_root,
            request,
            [_valid_payload("safe", "safe_parse")],
            run_id="run-seed-1",
        )
        state = controller.run()
        controller.close()
        assert state.terminal_status is TerminalStatus.HARNESS_VERIFIED
        run_dir = ArtifactStore(workspace_root, "safe", "run-seed-1").run_dir
        corpus_files = {p.name for p in (run_dir / "corpus").iterdir()}
        assert {"seed1.json", "seed2.txt"} <= corpus_files
        events = (run_dir / "events.jsonl").read_text(encoding="utf-8")
        assert "corpus:seed" in events


class TestOnEvent:
    def test_on_event_callback_receives_events(self, workspace_root: Path) -> None:
        seen: list[str] = []

        def collect(event: object) -> None:
            seen.append(event.kind)  # type: ignore[attr-defined]

        request = _request(workspace_root, source="repos/safe", function="safe_parse")
        validation = ValidationProfile(name="default", sandbox={"required": False})
        driver = ScriptedGenerationDriver([_valid_payload("safe", "safe_parse")])
        backend = LocalLinuxBackend(validation)
        controller = RunController(
            workspace_root=workspace_root,
            request=request,
            profile=validation,
            driver=driver,
            backend=backend,
            run_id="run-event-1",
            on_event=collect,
        )
        state = controller.run()
        controller.close()
        assert state.terminal_status is TerminalStatus.HARNESS_VERIFIED
        assert "execution:compile" in seen
        assert "execution:decided" in seen
        assert "run:terminal" in seen


class TestBuildDirMode:
    """Build-directory mode: harness links the user's CMake-built static library."""

    def test_build_dir_mode_harness_verified(self, workspace_root: Path) -> None:
        build_dir = workspace_root / "repos" / "cmake-proj"
        # The CMake fixture is not part of the workspace_root copy; copy it in.
        import shutil

        shutil.rmtree(build_dir, ignore_errors=True)
        shutil.copytree(
            Path(__file__).parent / "fixtures" / "repos" / "cmake-proj",
            build_dir,
        )
        payload = make_artifact_payload(
            "cmake-proj",
            "cmake_parse",
            harness_file="harness_cmake.c",
            target_sources=[],  # product sources come from the built library
        )
        request = _request(workspace_root, source="repos/cmake-proj", function="cmake_parse", loops=2)
        request.build_dir = build_dir
        validation = ValidationProfile(name="default", sandbox={"required": False})
        driver = ScriptedGenerationDriver([payload])
        backend = LocalLinuxBackend(validation)
        controller = RunController(
            workspace_root=workspace_root,
            request=request,
            profile=validation,
            driver=driver,
            backend=backend,
            run_id="run-cmake-1",
        )
        state = controller.run()
        controller.close()
        assert state.terminal_status is TerminalStatus.HARNESS_VERIFIED
        assert state.generation_loop == 1
        run_dir = ArtifactStore(workspace_root, "cmake-proj", "run-cmake-1").run_dir
        events = (run_dir / "events.jsonl").read_text(encoding="utf-8")
        assert "execution:cmake_configure" in events
        assert "execution:cmake_build" in events
        assert "execution:cmake_library" in events
        execution = json.loads((run_dir / "executions" / "loop-01" / "execution.json").read_text())
        assert execution["coverage"]["target_function_hit"] is True
        assert (build_dir / "goaloop-build" / "libcmake_target.a").is_file()

    def test_build_dir_requires_cmakelists(self, workspace_root: Path) -> None:
        bad_dir = workspace_root / "repos" / "safe" / "no-cmake"
        bad_dir.mkdir(exist_ok=True)
        request = _request(workspace_root, source="repos/safe", function="safe_parse")
        request.build_dir = bad_dir
        validation = ValidationProfile(name="default", sandbox={"required": False})
        driver = ScriptedGenerationDriver([make_artifact_payload("safe", "safe_parse")])
        controller = RunController(
            workspace_root=workspace_root,
            request=request,
            profile=validation,
            driver=driver,
            backend=LocalLinuxBackend(validation),
            run_id="run-cmake-bad",
        )
        state = controller.run()
        controller.close()
        assert state.terminal_status is TerminalStatus.NEEDS_INPUT
        assert "CMakeLists.txt" in _report_text(workspace_root, "run-cmake-bad")


class TestResumeRecovery:
    """Resume must retry failed/blocked runs, not just re-render the report."""

    @pytest.mark.parametrize(
        ("terminal_phase", "expected_phase", "recoverable"),
        [
            (Phase.PREPROCESS, Phase.PREPROCESS, True),
            (Phase.HARNESS_GENERATION, Phase.HARNESS_GENERATION, True),
            (Phase.HARNESS_EXECUTION, Phase.HARNESS_EXECUTION, True),
            (Phase.CRASH_ANALYSIS_REPORT, Phase.CRASH_ANALYSIS_REPORT, False),
        ],
    )
    def test_terminal_failure_routes_to_recorded_phase(
        self,
        workspace_root: Path,
        terminal_phase: Phase,
        expected_phase: Phase,
        recoverable: bool,
    ) -> None:
        run_id = f"run-route-{terminal_phase.value}"
        request = _request(workspace_root, source="repos/safe", function="safe_parse", loops=3)
        goal = GenerationGoal(
            run_id=run_id,
            objective="resume route",
            target_function="safe_parse",
            acceptance_criteria=[],
            max_generation_loops=3,
        )
        state = RunState(
            run_id=run_id,
            project_name="safe",
            request=request,
            phase=Phase.CRASH_ANALYSIS_REPORT,
            terminal_status=TerminalStatus.BLOCKED,
            terminal_phase=terminal_phase,
            goal=goal,
        )
        store = ArtifactStore(workspace_root, "safe", run_id)
        store.initialize()
        store.save_state(state)
        controller = RunController(
            workspace_root=workspace_root,
            request=request,
            profile=ValidationProfile(name="default", sandbox={"required": False}),
            driver=ScriptedGenerationDriver([]),
            backend=LocalLinuxBackend(ValidationProfile(name="default", sandbox={"required": False})),
            run_id=run_id,
            resume=True,
        )

        controller._load_checkpoint()
        try:
            assert controller.state is not None
            assert controller.state.phase is expected_phase
            assert (controller.state.terminal_status is None) is recoverable
        finally:
            controller.close()

    def test_resume_retries_blocked_generation(self, workspace_root: Path) -> None:
        request = _request(workspace_root, source="repos/safe", function="safe_parse", loops=3)
        # First run: driver unavailable -> BLOCKED before any harness is made.
        controller = _controller(
            workspace_root, request, [_valid_payload("safe", "safe_parse")], run_id="run-recv-1"
        )
        controller.driver.unavailable = True
        state = controller.run()
        controller.close()
        assert state.terminal_status is TerminalStatus.BLOCKED
        run_dir = ArtifactStore(workspace_root, "safe", "run-recv-1").run_dir
        assert not list((run_dir / "iterations").glob("loop-*/candidate"))

        # Resume with a working driver: must continue generation -> verified.
        resumed = _controller(
            workspace_root,
            request,
            [_valid_payload("safe", "safe_parse")],
            run_id="run-recv-1",
            resume=True,
        )
        state2 = resumed.run()
        resumed.close()
        assert state2.terminal_status is TerminalStatus.HARNESS_VERIFIED
        assert state2.generation_loop == 1
        events = (run_dir / "events.jsonl").read_text(encoding="utf-8")
        assert "run:resumed" in events
        assert list((run_dir / "iterations").glob("loop-*/candidate"))

    def test_resume_retries_failed_generation(self, workspace_root: Path) -> None:
        request = _request(workspace_root, source="repos/safe", function="safe_parse", loops=3)
        controller = _controller(
            workspace_root, request, [_valid_payload("safe", "safe_parse")], run_id="run-recv-2"
        )
        controller.driver.fail_after = 0  # GenerationFailure on the first call
        state = controller.run()
        controller.close()
        assert state.terminal_status is TerminalStatus.FAILED

        resumed = _controller(
            workspace_root,
            request,
            [_valid_payload("safe", "safe_parse")],
            run_id="run-recv-2",
            resume=True,
        )
        state2 = resumed.run()
        resumed.close()
        assert state2.terminal_status is TerminalStatus.HARNESS_VERIFIED

    def test_budget_exhausted_failed_not_recovered(self, workspace_root: Path) -> None:
        request = _request(workspace_root, source="repos/safe", function="safe_parse", loops=1)
        controller = _controller(
            workspace_root, request, [_broken_payload("safe", "safe_parse")], run_id="run-recv-3"
        )
        state = controller.run()
        controller.close()
        assert state.terminal_status is TerminalStatus.FAILED  # budget exhausted

        resumed = _controller(
            workspace_root,
            request,
            [_valid_payload("safe", "safe_parse")],
            run_id="run-recv-3",
            resume=True,
        )
        state2 = resumed.run()
        resumed.close()
        # Budget-exhausted FAILED stays terminal; resume only re-renders.
        assert state2.terminal_status is TerminalStatus.FAILED
        assert state2.generation_loop == 1
