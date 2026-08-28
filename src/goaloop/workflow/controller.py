"""RunController: the four-phase state machine with checkpointing and resume."""

from __future__ import annotations

import os
import shutil
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..backend import ExecutionBackend
from ..driver import GenerationDriver
from ..models import (
    CrashAnalysisResult,
    FuzzRunRequest,
    GenerationGoal,
    HarnessExecutionResult,
    ModelProfile,
    Phase,
    PreprocessResult,
    RunEvent,
    RunState,
    TerminalStatus,
    ValidationProfile,
)
from ..preprocess import preprocess_request
from ..storage import ArtifactStore, create_run_id
from .generation import GenerationMixin
from .report import ReportMixin

PREPROCESS_FILENAME = "preprocess.json"


def _provider_base_url_env(provider: str) -> str:
    return provider.upper().replace("-", "_") + "_BASE_URL"


def _provider_model_env(provider: str) -> str:
    return provider.upper().replace("-", "_") + "_MODEL"
GOAL_FILENAME = "goal.json"
EXECUTIONS_DIR = "executions"


class RunController(GenerationMixin, ReportMixin):
    """Drives one run to a terminal status, resuming from disk when asked."""

    def __init__(
        self,
        *,
        workspace_root: Path,
        request: FuzzRunRequest,
        profile: ValidationProfile,
        driver: GenerationDriver,
        backend: ExecutionBackend,
        model_profile: ModelProfile | None = None,
        run_id: str | None = None,
        resume: bool = False,
        output_root: Path | None = None,
        on_event: Callable[[RunEvent], None] | None = None,
    ) -> None:
        self.workspace_root = workspace_root.resolve()
        self.request = request
        self.profile = profile
        self.driver = driver
        self.backend = backend
        self.model_profile = model_profile
        self.resume = resume
        # Output root for run products; defaults to <workspace>/work.
        self.output_root = (output_root or self.workspace_root / "work").resolve()
        self.on_event = on_event

        self.state: RunState | None = None
        self.store: ArtifactStore | None = None
        self.preprocess: PreprocessResult | None = None
        self.goal: GenerationGoal | None = None
        self.last_execution: HarnessExecutionResult | None = None
        self.last_crash_analysis: CrashAnalysisResult | None = None
        self._phase_started = time.monotonic()
        self._phase_durations: dict[str, float] = {}
        self._loop_hashes: dict[str, dict[str, str]] = {}
        self._first_compile_success: bool | None = None
        self._time_to_bug: float | None = None
        self._resumed_run_id = run_id

    # -- public -------------------------------------------------------------

    def run(self) -> RunState:
        if self.resume:
            self._load_checkpoint()
        else:
            run_id = self._resumed_run_id or create_run_id()
            self._create_initial_state(run_id)

        while self.state is not None and self.state.terminal_status is None:
            phase = self.state.phase
            if phase is Phase.PREPROCESS:
                self._preprocess_step()
            elif phase is Phase.HARNESS_GENERATION:
                self._generation_step()
            elif phase is Phase.CRASH_ANALYSIS_REPORT:
                self._report_step()
                if self.state.phase is Phase.HARNESS_GENERATION:
                    continue  # harness-owned crash returned to generation
                break
            else:  # pragma: no cover - defensive
                self._terminate(TerminalStatus.FAILED, f"unexpected phase {phase}")
                break

        # Every terminal run must pass through the report phase exactly once.
        if self.state is not None and self.state.terminal_status is not None:
            if self.state.phase is not Phase.CRASH_ANALYSIS_REPORT:
                self._enter_phase(Phase.CRASH_ANALYSIS_REPORT)
            if self.store is None or not (self.store.run_dir / "validation.json").is_file():
                self._report_step()
        return self.state  # type: ignore[return-value]

    def close(self) -> None:
        self.driver.close()

    # -- lifecycle helpers ---------------------------------------------------

    def _create_initial_state(self, run_id: str) -> None:
        goal = GenerationGoal(
            run_id=run_id,
            objective=(
                f"generate a libFuzzer harness for {self.request.function} in "
                f"{self.request.source} within repository {self.request.repo or self.request.source} "
                "that compiles, executes, and reaches the target function"
            ),
            target_function=self.request.function,
            acceptance_criteria=[
                "candidate compiles with -fsanitize=fuzzer,address,undefined",
                "candidate executes and hits the target function",
                "coverage policy in the validation profile is satisfied",
            ],
            max_generation_loops=self.request.max_generation_loops,
        )
        self.state = RunState(
            run_id=run_id,
            project_name="unknown",
            request=self.request,
            phase=Phase.PREPROCESS,
            goal=goal,
            output_root=self.output_root,
        )
        self.goal = goal
        # Nothing is persisted until preprocess resolves the project name, so a
        # crash during preprocess leaves no half-written run directory behind.

    def _load_checkpoint(self) -> None:
        if self._resumed_run_id is None:
            raise ValueError("resume requires --run-id")
        run_id = self._resumed_run_id
        matches = sorted(self.output_root.glob(f"*/runs/{run_id}"))
        if not matches:
            raise FileNotFoundError(f"run {run_id!r} was not found under {self.output_root}")
        run_dir = matches[0]
        project_name = run_dir.parent.parent.name
        self.store = ArtifactStore(self.workspace_root, project_name, run_id, output_root=self.output_root)
        self.state = self.store.load_state()
        self.goal = self.state.goal
        preprocess_path = run_dir / PREPROCESS_FILENAME
        if preprocess_path.is_file():
            self.preprocess = PreprocessResult.model_validate_json(preprocess_path.read_text(encoding="utf-8"))
        goal_path = run_dir / GOAL_FILENAME
        if goal_path.is_file():
            self.goal = GenerationGoal.model_validate_json(goal_path.read_text(encoding="utf-8"))
            self.state.goal = self.goal
        execution_path = self._last_execution_path(run_dir)
        if execution_path is not None:
            self.last_execution = HarnessExecutionResult.model_validate_json(execution_path.read_text(encoding="utf-8"))
        crash_path = run_dir / "crash-analysis.json"
        if crash_path.is_file():
            self.last_crash_analysis = CrashAnalysisResult.model_validate_json(crash_path.read_text(encoding="utf-8"))
        self._recover_terminal_run()
        self._event("phase:resume", {"phase": self.state.phase.value})

    def _recover_terminal_run(self) -> None:
        """Let resume retry a failed/blocked run instead of only re-rendering.

        A FAILED/BLOCKED terminal (e.g. a transient model endpoint error, or an
        SDK failure) is often worth retrying after the environment is fixed;
        resume clears the terminal marker and returns to the generation phase.
        Already-executed loops are never re-run: evidence is preserved and the
        next generation continues at ``generation_loop + 1``.

        Terminal statuses that are real outcomes are NOT recovered:
        harness_verified, bug_reproduced, needs_review (they completed), and
        needs_input (the request itself is wrong). Budget-exhausted FAILED runs
        are also kept terminal; the generation step guards against exceeding
        the budget.
        """
        if self.state is None or self.state.terminal_status is None:
            return
        status = self.state.terminal_status
        if status not in (TerminalStatus.FAILED, TerminalStatus.BLOCKED):
            return
        if status is TerminalStatus.FAILED and self.state.generation_loop >= self.request.max_generation_loops:
            return  # budget already exhausted; nothing left to retry
        reason = self._terminal_reason() or status.value
        self.state.terminal_status = None
        if self.goal is not None:
            self.goal.completed = False
        self.state.phase = Phase.HARNESS_GENERATION
        self._event("run:resumed", {"from_status": status.value, "reason": reason})

    def _save_checkpoint(self) -> None:
        if self.state is None or self.store is None:
            return
        self.store.save_state(self.state)
        if self.goal is not None:
            self.store.write_json(self.store.run_dir / GOAL_FILENAME, self.goal)

    def _event(self, kind: str, payload: dict[str, Any], *, phase: Phase | None = None) -> None:
        if self.state is None or self.store is None:
            return
        event = RunEvent(
            sequence=self.store.next_event_sequence(),
            phase=phase or self.state.phase,
            kind=kind,
            payload=payload,
        )
        self.store.append_event(event)
        if self.on_event is not None:
            self.on_event(event)

    def _progress(self, kind: str, payload: dict[str, Any]) -> None:
        if self.state is None or self.on_event is None:
            return
        self.on_event(
            RunEvent(
                sequence=0,
                phase=self.state.phase,
                kind=kind,
                payload=payload,
            )
        )

    def _enter_phase(self, phase: Phase) -> None:
        assert self.state is not None
        elapsed = time.monotonic() - self._phase_started
        self._phase_durations[self.state.phase.value] = round(elapsed, 3)
        self._phase_started = time.monotonic()
        self.state.phase = phase
        self._event("phase:enter", {"phase": phase.value})
        self._save_checkpoint()

    def _terminate(self, status: TerminalStatus, reason: str) -> None:
        assert self.state is not None
        self.state.terminal_status = status
        self._event("run:terminal", {"status": status.value, "reason": reason})
        self._save_checkpoint()

    def _persist_preprocess(self) -> None:
        if self.store is not None and self.preprocess is not None:
            assert self.state is not None
            self.store.write_json(self.store.run_dir / PREPROCESS_FILENAME, self.preprocess)
            self.state.preprocess_result_path = (
                (self.store.run_dir / PREPROCESS_FILENAME).relative_to(self.store.run_dir).as_posix()
            )

    def _persist_execution(self, execution: HarnessExecutionResult) -> None:
        assert self.store is not None and self.state is not None
        path = self.store.run_dir / EXECUTIONS_DIR / f"loop-{execution.generation_loop:02d}" / "execution.json"
        self.store.write_json(path, execution)
        self.state.last_execution_path = path.relative_to(self.store.run_dir).as_posix()

    def _last_execution_path(self, run_dir: Path) -> Path | None:
        executions = run_dir / EXECUTIONS_DIR
        if not executions.is_dir():
            return None
        candidates = sorted(executions.glob("loop-*/execution.json"))
        return candidates[-1] if candidates else None

    # -- phase: preprocess ---------------------------------------------------

    def _preprocess_step(self) -> None:
        assert self.state is not None
        started = time.monotonic()
        self._progress(
            "preprocess:started",
            {"repo": str(self.request.repo or self.request.source), "source": str(self.request.source)},
        )
        self._ensure_model_credential()
        preprocess = preprocess_request(
            self.workspace_root,
            self.state.run_id,
            self.request,
            self.profile,
            api_key_env=self.model_profile.api_key_env if self.model_profile is not None else "DEEPSEEK_API_KEY",
            max_context_bytes=self.request.max_context_kb * 1024,
            on_progress=self._progress,
        )
        self.preprocess = preprocess
        self.state.project_name = preprocess.project_name
        self.store = ArtifactStore(
            self.workspace_root,
            preprocess.project_name,
            self.state.run_id,
            output_root=self.output_root,
        )
        self.store.initialize()
        self._seed_corpus()
        self._persist_preprocess()
        duration = round(time.monotonic() - started, 3)
        self._event(
            "preprocess:done",
            {
                "ready": preprocess.ready,
                "status": preprocess.terminal_status.value if preprocess.terminal_status else None,
                "reason": preprocess.reason,
                "duration": duration,
            },
        )
        self._phase_durations["preprocess"] = duration
        self._save_checkpoint()
        if not preprocess.ready:
            status = preprocess.terminal_status or TerminalStatus.FAILED
            self._terminate(status, preprocess.reason or "preprocess did not produce a ready result")
            self._enter_phase(Phase.CRASH_ANALYSIS_REPORT)
            return
        self._enter_phase(Phase.HARNESS_GENERATION)

    def _ensure_model_credential(self) -> None:
        """Make profile-stored api_key/base_url/model visible to the SDK runtime.

        The key is injected into api_key_env; the endpoint and the model are
        injected into the pi-ai <PROVIDER>_BASE_URL / <PROVIDER>_MODEL variables
        so profile values work for pi-ai routes (e.g. custom-gateway reads
        CUSTOM_GATEWAY_BASE_URL / CUSTOM_GATEWAY_MODEL) as well as deepseek.
        """
        if self.model_profile is None:
            return
        if self.model_profile.api_key:
            os.environ[self.model_profile.api_key_env] = self.model_profile.api_key
        if self.model_profile.base_url:
            os.environ[_provider_base_url_env(self.model_profile.provider)] = self.model_profile.base_url
        os.environ[_provider_model_env(self.model_profile.provider)] = self.model_profile.model

    def _seed_corpus(self) -> None:
        """Copy user-provided seed inputs into the run's corpus before fuzzing."""
        seed = self.request.seed_corpus
        if seed is None or self.store is None:
            return
        seed_dir = seed.resolve()
        if not seed_dir.is_dir():
            self._event("corpus:seed", {"ok": False, "detail": f"seed corpus is not a directory: {seed_dir}"})
            return
        copied = 0
        for item in sorted(seed_dir.iterdir()):
            if item.is_file():
                shutil.copy2(item, self.store.corpus_dir / item.name)
                copied += 1
        self._event("corpus:seed", {"ok": True, "copied": copied, "source": str(seed_dir)})

    def _redacted_excerpt(self, excerpt: str | None) -> str | None:
        if excerpt is None or self.preprocess is None:
            return excerpt
        from ..redaction import redact

        return redact(excerpt, self.workspace_root)
