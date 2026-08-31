"""Pydantic contracts shared by every stage of the goal loop."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SCHEMA_VERSION: Literal["1.0"] = "1.0"


class Contract(BaseModel):
    """Strict base contract; unexpected model fields are never ignored."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class Language(StrEnum):
    AUTO = "auto"
    C = "c"
    CPP = "cpp"


class Phase(StrEnum):
    PREPROCESS = "preprocess"
    HARNESS_GENERATION = "harness_generation"
    HARNESS_EXECUTION = "harness_execution"
    CRASH_ANALYSIS_REPORT = "crash_analysis_report"


class LoopStage(StrEnum):
    MODEL_GENERATION = "model_generation"
    MATERIALIZED = "materialized"
    EXECUTING = "executing"
    EXECUTED = "executed"


class TerminalStatus(StrEnum):
    HARNESS_VERIFIED = "harness_verified"
    BUG_REPRODUCED = "bug_reproduced"
    NEEDS_REVIEW = "needs_review"
    NEEDS_INPUT = "needs_input"
    BLOCKED = "blocked"
    FAILED = "failed"


class OptimizationPriority(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class OptimizationCategory(StrEnum):
    INPUT = "input"
    ENVIRONMENT = "environment"
    MODEL_OUTPUT = "model_output"
    GENERATION = "generation"
    BUILD = "build"
    CONTEXT = "context"
    PERFORMANCE = "performance"
    TOOLING = "tooling"
    TRIAGE = "triage"
    VALIDATION = "validation"


class ExecutionDisposition(StrEnum):
    ACCEPTED = "accepted"
    NEEDS_REGENERATION = "needs_regeneration"
    CRASH_CANDIDATE = "crash_candidate"
    ENVIRONMENT_ERROR = "environment_error"


class CrashOwnership(StrEnum):
    PRODUCT = "product"
    HARNESS = "harness"
    UNKNOWN = "unknown"


class FuzzRunRequest(Contract):
    repo: Path | None = None
    source: Path
    function: Annotated[str, Field(min_length=1, max_length=256, pattern=r"^[\w:$~.<>-]+$")]
    language: Language = Language.AUTO
    profile: str = "default"
    model_profile: str = "default"
    max_generation_loops: Annotated[int, Field(ge=1, le=20)] = 5
    fuzz_seconds: Annotated[int, Field(ge=1, le=86_400)] = 600
    # Source-context budget embedded in every generation prompt, in KiB.
    # This is the dominant contributor to the model's input-token usage: the
    # default (96 KiB ≈ 25-32K tokens) keeps one prompt well inside a 128K
    # window together with scaffolding, feedback and the model's own response.
    max_context_kb: Annotated[int, Field(ge=8, le=1024)] = 96
    # Optional directory of seed inputs copied into the run's corpus before
    # fuzzing, so successive runs can reuse a previous run's corpus.
    seed_corpus: Path | None = None
    # Optional CMake project directory (must contain CMakeLists.txt). When set,
    # the controller configures and builds the project inside that directory
    # (out-of-source to <build_dir>/goaloop-build, with sanitizer/coverage
    # instrumentation) and links the produced static library into the harness
    # instead of compiling target sources from model-declared BuildPlan.
    build_dir: Path | None = None


class ToolchainSettings(Contract):
    clang: str = "clang"
    clangxx: str = "clang++"
    llvm_profdata: str = "llvm-profdata"
    llvm_cov: str = "llvm-cov"
    bubblewrap: str = "bwrap"
    cmake: str = "cmake"


class SandboxSettings(Contract):
    required: bool = True
    disable_network: bool = True


class ResourceLimits(Contract):
    timeout_seconds: Annotated[int, Field(ge=1, le=86_400)] = 660
    memory_mb: Annotated[int, Field(ge=64, le=131_072)] = 2048
    cpu_seconds: Annotated[int, Field(ge=1, le=86_400)] = 660
    process_count: Annotated[int, Field(ge=1, le=4096)] = 16
    max_commands: Annotated[int, Field(ge=3, le=100)] = 16
    max_output_bytes: Annotated[int, Field(ge=4096, le=16_777_216)] = 1_048_576


class BuildSettings(Contract):
    """CMake build-directory mode: configure/build an existing CMake project.

    The build runs inside the user-provided build directory (out-of-source to
    ``<build_dir>/goaloop-build``) with sanitizer+coverage instrumentation
    injected via CMAKE_C_FLAGS / CMAKE_CXX_FLAGS, so the produced static
    library keeps source-level coverage attribution. The controller never
    executes model-generated scripts.
    """

    target: str | None = None
    # Static library path relative to <build_dir>/goaloop-build. When unset,
    # the controller auto-discovers the first *.a (declare it when the project
    # produces more than one static library).
    library: str | None = None
    # Extra include directories relative to the build_dir, prepended for the
    # harness compile.
    include_dirs: list[str] = Field(default_factory=list)
    # Extra configure flags, e.g. ["-DCMAKE_BUILD_TYPE=Release"].
    flags: list[str] = Field(default_factory=list)


class CoverageDecisionPolicy(Contract):
    require_target_function_hit: Literal[True] = True
    min_libfuzzer_cov_delta: Annotated[int, Field(ge=0)] = 0
    min_feature_delta: Annotated[int, Field(ge=0)] = 0
    min_corpus_delta: Annotated[int, Field(ge=0)] = 0
    min_target_line_delta: Annotated[float, Field(ge=0.0, le=100.0)] = 0.0
    require_any_positive_growth: bool = True
    min_execs_per_second: Annotated[float, Field(ge=0.0)] = 0.0


class ValidationProfile(Contract):
    name: str = "default"
    backend: Literal["local_linux"] = "local_linux"
    tools: ToolchainSettings = Field(default_factory=ToolchainSettings)
    sandbox: SandboxSettings = Field(default_factory=SandboxSettings)
    resources: ResourceLimits = Field(default_factory=ResourceLimits)
    coverage: CoverageDecisionPolicy = Field(default_factory=CoverageDecisionPolicy)
    build: BuildSettings = Field(default_factory=BuildSettings)
    # Compiler -D definitions the controller always appends (e.g. project
    # build knowledge like HAVE_WRITEV for old c-ares). User-reviewed profile
    # content, trusted and not part of the model-generated BuildPlan.
    default_defines: list[str] = Field(default_factory=list)
    # Extra include directories (relative to the workspace root, resolved at
    # profile load) that the controller always appends as -I. Lets users supply
    # configure-generated headers (e.g. ares_build.h) without touching repos/.
    default_include_dirs: list[str] = Field(default_factory=list)
    allowed_compiler_flags: list[str] = Field(
        default_factory=lambda: [
            "-I",
            "-D",
            "-U",
            "-std=",
            "-O",
            "-g",
            "-W",
            "-f",
            "-m",
            "-pthread",
        ]
    )
    allowed_libraries: list[str] = Field(default_factory=list)


class ModelProfile(Contract):
    name: str = "default"
    # Provider route resolved by the Cordis composition's LLM adapter. The
    # bundled deepseek adapter registers "deepseek-official"; the pi-ai adapter
    # routes by its providers dict keys (openai, anthropic, deepseek, ... or a
    # hand-declared OpenAI-compatible gateway name).
    provider: str = "deepseek-official"
    model: str = "deepseek-v4-pro"
    max_tokens: int | None = Field(default=None, ge=1)
    # The model's context window (input limit in tokens). When set, the driver
    # estimates each generation prompt and fails fast with an actionable error
    # before hitting the endpoint's own "input exceeds limit" rejection.
    max_input_tokens: int | None = Field(default=None, ge=1)
    cordis: Path | None = None
    # Optional endpoint override for the deepseek adapter (OpenAI-compatible
    # gateways should be configured in the pi-ai Cordis providers instead).
    base_url: str | None = None
    # Environment variable holding the model credential; used by preprocess
    # and doctor to gate readiness. The runtime subprocess inherits the whole
    # environment, so the adapter reads its own key (e.g. OPENAI_API_KEY).
    api_key_env: str = "DEEPSEEK_API_KEY"
    # Optional credential stored directly in the profile (alternative to the
    # environment variable). Precedence: --api-key CLI > api_key in toml >
    # api_key_env environment variable. SECURITY: a plaintext key in a profile
    # file can leak via version control or file sharing; prefer environment
    # variables or a .local profile that is git-ignored.
    api_key: str | None = None


class Capability(Contract):
    name: str
    available: bool
    detail: str


class CapabilityReport(Contract):
    platform: str
    capabilities: list[Capability]

    @property
    def ready(self) -> bool:
        return all(item.available for item in self.capabilities)


SourceContextKind = Literal[
    "target_function", "incoming_tree", "outgoing_tree", "param_constraints"
]


class SourceContext(Contract):
    kind: SourceContextKind = "target_function"
    path: str
    sha256: str
    content: str
    truncated: bool = False
    start_line: int | None = Field(default=None, ge=1)
    end_line: int | None = Field(default=None, ge=1)

    @field_validator("path")
    @classmethod
    def relative_source_path(cls, value: str) -> str:
        _validate_relative_path(value)
        return value


class PreprocessResult(Contract):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    run_id: str
    phase: Literal[Phase.PREPROCESS] = Phase.PREPROCESS
    ready: bool
    project_name: str
    source_root: Path
    source_scope: Path | None = None
    language: Language
    target_function: str
    contexts: list[SourceContext] = Field(default_factory=list)
    candidate_signatures: list[str] = Field(default_factory=list)
    capability_report: CapabilityReport
    terminal_status: TerminalStatus | None = None
    reason: str | None = None
    # Resolved CMake build directory when --build-dir mode is used. In that
    # mode the controller builds the project itself and build-file contents
    # are excluded from contexts, so this path is the only build info the
    # model receives (it cannot read files anyway).
    build_dir: Path | None = None

    @model_validator(mode="before")
    @classmethod
    def discard_legacy_bulk_contexts(cls, value: Any) -> Any:
        """Let resume load older runs without re-injecting dependency/build contents."""
        if not isinstance(value, dict) or not isinstance(value.get("contexts"), list):
            return value
        migrated = dict(value)
        migrated["contexts"] = [
            context
            for context in value["contexts"]
            if not isinstance(context, dict) or context.get("kind") not in {"dependency", "build"}
        ]
        return migrated

    @model_validator(mode="after")
    def terminal_state_matches_readiness(self) -> PreprocessResult:
        if self.ready and self.terminal_status is not None:
            raise ValueError("ready preprocess result cannot be terminal")
        if not self.ready and self.terminal_status is None:
            raise ValueError("non-ready preprocess result requires terminal_status")
        return self


class GenerationFeedback(Contract):
    category: str
    summary: str
    compile_exit_code: int | None = None
    sanitizer_kind: str | None = None
    cov_delta: int | None = None
    feature_delta: int | None = None
    corpus_delta: int | None = None
    target_function_hit: bool | None = None
    target_line_coverage: float | None = None
    log_excerpt: str | None = None
    artifact_hashes: dict[str, str] = Field(default_factory=dict)


class GenerationGoal(Contract):
    run_id: str
    objective: str
    target_function: str
    acceptance_criteria: list[str]
    max_generation_loops: int
    current_loop: int = 0
    completed: bool = False
    latest_feedback: GenerationFeedback | None = None


class BuildPlan(Contract):
    compiler: Literal["clang", "clang++"]
    harness_file: str
    target_sources: list[str] = Field(default_factory=list, max_length=256)
    include_dirs: list[str] = Field(default_factory=list, max_length=128)
    defines: list[str] = Field(default_factory=list, max_length=128)
    cflags: list[str] = Field(default_factory=list, max_length=128)
    ldflags: list[str] = Field(default_factory=list, max_length=128)
    libraries: list[str] = Field(default_factory=list, max_length=128)
    binary_name: str = "fuzzer"

    @field_validator("harness_file", "binary_name")
    @classmethod
    def safe_output_path(cls, value: str) -> str:
        _validate_relative_path(value)
        return value

    @field_validator("target_sources", "include_dirs")
    @classmethod
    def safe_repo_paths(cls, values: list[str]) -> list[str]:
        for value in values:
            _validate_relative_path(value)
        return values

    @field_validator("defines", "cflags", "ldflags", "libraries")
    @classmethod
    def shell_free_tokens(cls, values: list[str]) -> list[str]:
        forbidden = ("\x00", "\n", "\r", ";", "&&", "||", "`", "$(", ">", "<")
        for value in values:
            if not value or any(token in value for token in forbidden):
                raise ValueError(f"unsafe build token: {value!r}")
        return values


class EndpointPlan(Contract):
    function: str
    signature: str
    location: str
    language: Literal["c", "cpp"]
    input_model: str
    lifecycle: list[str] = Field(default_factory=list)
    build: BuildPlan

    @field_validator("location")
    @classmethod
    def safe_location(cls, value: str) -> str:
        _validate_relative_path(value)
        return value


class GeneratedFile(Contract):
    path: str
    content: Annotated[str, Field(max_length=1_000_000)]
    purpose: str

    @field_validator("path")
    @classmethod
    def safe_generated_path(cls, value: str) -> str:
        _validate_relative_path(value)
        return value


class GeneratedArtifactSet(Contract):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    run_id: str
    phase: Literal[Phase.HARNESS_GENERATION] = Phase.HARNESS_GENERATION
    generation_loop: Annotated[int, Field(ge=1, le=20)]
    candidate_ready: Literal[True] = True
    summary: str
    endpoint_plan: EndpointPlan
    files: Annotated[list[GeneratedFile], Field(min_length=4, max_length=64)]
    format_retry: Annotated[int, Field(ge=0, le=1)] = 0

    @model_validator(mode="after")
    def unique_file_paths(self) -> GeneratedArtifactSet:
        paths = [item.path for item in self.files]
        if len(paths) != len(set(paths)):
            raise ValueError("generated file paths must be unique")
        return self


class ProcessRequest(Contract):
    argv: Annotated[list[str], Field(min_length=1, max_length=512)]
    cwd: Path
    timeout_seconds: Annotated[int, Field(ge=1, le=86_400)]
    env: dict[str, str] = Field(default_factory=dict)
    # When set, the child's stdout is written directly to this file instead of
    # being captured in memory, so large outputs (e.g. llvm-cov exports) are
    # not truncated by max_output_bytes.
    stdout_path: Path | None = None


class ProcessResult(Contract):
    argv: list[str]
    exit_code: int | None
    timed_out: bool = False
    duration_seconds: float
    stdout: str = ""
    stderr: str = ""
    output_truncated: bool = False


class CoverageMetrics(Contract):
    initial_cov: int | None = None
    final_cov: int | None = None
    cov_delta: int = 0
    initial_features: int | None = None
    final_features: int | None = None
    feature_delta: int = 0
    initial_corpus: int | None = None
    final_corpus: int | None = None
    corpus_delta: int = 0
    execs_per_second: float | None = None
    target_function_hit: bool = False
    target_line_coverage: float | None = None
    target_line_delta: float = 0.0


class HarnessExecutionResult(Contract):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    run_id: str
    phase: Literal[Phase.HARNESS_EXECUTION] = Phase.HARNESS_EXECUTION
    generation_loop: int
    disposition: ExecutionDisposition
    compile_result: ProcessResult
    fuzz_result: ProcessResult | None = None
    coverage: CoverageMetrics = Field(default_factory=CoverageMetrics)
    sanitizer_kind: str | None = None
    crash_artifact: str | None = None
    reason: str


class GenerationDecision(Contract):
    """Deterministic controller decision derived from execution evidence."""

    disposition: ExecutionDisposition
    completes_goal: bool
    reason: str
    feedback: GenerationFeedback | None = None

    @model_validator(mode="after")
    def completion_matches_disposition(self) -> GenerationDecision:
        if self.completes_goal != (self.disposition is ExecutionDisposition.ACCEPTED):
            raise ValueError("only an accepted candidate can complete the generation goal")
        if self.disposition is ExecutionDisposition.NEEDS_REGENERATION and self.feedback is None:
            raise ValueError("regeneration decisions require structured feedback")
        return self


class CrashAnalysisResult(Contract):
    ownership: CrashOwnership
    sanitizer_kind: str | None = None
    reproductions: Annotated[int, Field(ge=0, le=3)] = 0
    required_reproductions: Literal[3] = 3
    minimized_artifact: str | None = None
    stack_excerpt: str | None = None
    reason: str


class ValidationResult(Contract):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    run_id: str
    phase: Literal[Phase.CRASH_ANALYSIS_REPORT] = Phase.CRASH_ANALYSIS_REPORT
    status: TerminalStatus
    generation_loops_used: int
    execution: HarnessExecutionResult | None = None
    crash_analysis: CrashAnalysisResult | None = None
    report_path: str | None = None
    reason: str


class RunEvent(Contract):
    sequence: int
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    phase: Phase
    kind: str
    payload: dict[str, object] = Field(default_factory=dict)


class RunContext(Contract):
    """Everything the execution backend needs to sandbox one run."""

    run_id: str
    project_name: str
    run_dir: Path
    source_root: Path
    candidate_dir: Path | None = None
    binary_name: str = "fuzzer"


class ExecutionLease(Contract):
    """Bounded authorization for commands a backend may execute in one run."""

    allowed_executables: list[str] = Field(default_factory=list)
    allowed_dirs: list[str] = Field(default_factory=list)
    timeout_seconds: int
    max_commands: int
    commands_used: int = 0

    def authorize(self, argv: list[str]) -> bool:
        if not argv:
            return False
        if self.commands_used >= self.max_commands:
            return False
        executable = argv[0]
        candidates = [executable]
        if "/" not in executable:
            import shutil

            found = shutil.which(executable)
            if found is not None:
                candidates.append(found)
        for candidate in candidates:
            if candidate in self.allowed_executables:
                return True
            try:
                resolved = str(Path(candidate).resolve())
            except OSError:
                continue
            if resolved in self.allowed_executables:
                return True
            if any(Path(resolved).is_relative_to(Path(directory).resolve()) for directory in self.allowed_dirs):
                return True
        return False


class CollectedArtifacts(Contract):
    fuzzer_binary: Path | None = None
    corpus_dir: Path | None = None
    crash_files: list[Path] = Field(default_factory=list)
    profraw_files: list[Path] = Field(default_factory=list)
    profdata_path: Path | None = None
    coverage_json: Path | None = None


class ResearchMetrics(Contract):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    run_id: str
    provider: str
    model: str
    prompt_version: str
    endpoint_label: str
    started_at: datetime
    finished_at: datetime
    phase_durations: dict[str, float] = Field(default_factory=dict)
    generation_loops_used: int = 0
    format_retries: int = 0
    first_compile_success: bool | None = None
    final_status: TerminalStatus
    token_source: Literal["sdk", "unavailable"] = "unavailable"
    tokens_used: int | None = None
    dsh_trace_path: str | None = None
    dsh_trace_summary_path: str | None = None
    dsh_trace_events: int = 0
    model_calls: int = 0
    model_call_seconds: float = 0.0
    estimated_input_tokens: int = 0
    model_response_chars: int = 0
    tool_calls: int = 0
    time_to_bug_seconds: float | None = None
    loop_hashes: dict[str, dict[str, str]] = Field(default_factory=dict)


class OptimizationSuggestion(Contract):
    id: Annotated[str, Field(min_length=1, max_length=80, pattern=r"^[a-z0-9-]+$")]
    priority: OptimizationPriority
    category: OptimizationCategory
    title: Annotated[str, Field(min_length=1, max_length=160)]
    evidence: list[Annotated[str, Field(min_length=1, max_length=1000)]] = Field(default_factory=list)
    recommendation: Annotated[str, Field(min_length=1, max_length=2000)]
    expected_impact: Annotated[str, Field(min_length=1, max_length=1000)]


class OptimizationAnalysis(Contract):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    run_id: str
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    final_status: TerminalStatus
    source_metrics_path: str = "research-metrics.json"
    trace_summary_path: str | None = None
    summary: Annotated[str, Field(min_length=1, max_length=1000)]
    signals: dict[str, int | float | str | bool | None] = Field(default_factory=dict)
    suggestions: list[OptimizationSuggestion] = Field(default_factory=list)


class RunState(Contract):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    run_id: str
    project_name: str
    request: FuzzRunRequest
    phase: Phase = Phase.PREPROCESS
    generation_loop: int = 0
    active_loop: int | None = None
    loop_stage: LoopStage | None = None
    terminal_status: TerminalStatus | None = None
    terminal_phase: Phase | None = None
    goal: GenerationGoal
    last_execution_path: str | None = None
    validation_result_path: str | None = None
    preprocess_result_path: str | None = None
    optimization_analysis_path: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    # Output root holding this run's artifacts (resolved absolute path).
    # None means the default <workspace>/work layout; persisted so reports and
    # audits can show where the run's products actually live.
    output_root: Path | None = None


def _validate_relative_path(value: str) -> None:
    if "\\" in value:
        raise ValueError("paths must use forward slashes")
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts or "." in path.parts:
        raise ValueError(f"path must be clean and relative: {value!r}")
