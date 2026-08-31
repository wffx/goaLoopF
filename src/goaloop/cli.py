"""Typer CLI for the goaloop-fuzz workflow."""

from __future__ import annotations

import json
import os
import shlex
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import typer

from .backend import LocalLinuxBackend, toolchain_capabilities
from .config import load_model_profile, load_validation_profile
from .driver import DeepSeekHarnessDriver
from .krepo import krepo_cli_path
from .models import Capability, FuzzRunRequest, Language, ModelProfile, RunEvent, RunState
from .optimization import OPTIMIZATION_ANALYSIS_FILENAME
from .redaction import redact
from .report import REPORT_FILENAME, VALIDATION_FILENAME
from .storage import ArtifactStore, RunLockedError, create_run_id
from .trace import DshTraceTerminalFormatter
from .workflow import RunController

app = typer.Typer(
    name="goaloop",
    help="Deterministic goal loop for DeepSeek-assisted libFuzzer harness generation.",
    no_args_is_help=True,
    add_completion=False,
)


def _workspace_root(workspace: Path | None) -> Path:
    if workspace is not None:
        return workspace.resolve()
    env = os.environ.get("GOALOOP_WORKSPACE")
    return Path(env).resolve() if env else Path.cwd().resolve()


def _find_run_dir(workspace_root: Path, run_id: str, output_root: Path | None = None) -> Path:
    base = (output_root or workspace_root / "work").resolve()
    matches = sorted(base.glob(f"*/runs/{run_id}"))
    if not matches:
        hint = "" if output_root is not None else " (if the run used --output, pass the same --output here)"
        raise typer.BadParameter(f"run {run_id!r} was not found under {base}{hint}")
    return matches[0]


def _load_run_state(run_dir: Path) -> RunState:
    state_path = run_dir / "state.json"
    if not state_path.is_file():
        raise typer.BadParameter(f"run directory {run_dir} has no state.json")
    return RunState.model_validate_json(state_path.read_text(encoding="utf-8"))


@app.command()
def run(
    repo: Path = typer.Option(..., "--repo", help="code repository root directory"),
    source: Path = typer.Option(..., "--source", help="target function directory or file within the repository"),
    function: str = typer.Option(..., "--function", help="target function symbol"),
    language: str = typer.Option("auto", "--language", help="auto | c | cpp"),
    profile: str = typer.Option("default", "--profile", help="validation profile name"),
    model_profile: str = typer.Option("default", "--model-profile", help="model profile name"),
    max_generation_loops: int = typer.Option(5, "--max-generation-loops", min=1, max=20),
    fuzz_seconds: int = typer.Option(600, "--fuzz-seconds", min=1, max=86400),
    max_context_kb: int = typer.Option(
        96,
        "--max-context-kb",
        min=8,
        max=1024,
        help="source-context budget embedded in each generation prompt (KiB); lower it to cut input tokens",
    ),
    max_input_tokens: int | None = typer.Option(
        None,
        "--max-input-tokens",
        min=1024,
        help="fail fast when a prompt is estimated to exceed this many input tokens (default: model profile)",
    ),
    seed_corpus: Path | None = typer.Option(
        None, "--seed-corpus", help="directory of seed inputs copied into the run corpus"
    ),
    build_dir: Path | None = typer.Option(
        None,
        "--build-dir",
        help="CMake project directory (must contain CMakeLists.txt); builds inside it and links the static library",
    ),
    model_name: str | None = typer.Option(
        None, "--model-name", help="override model id (e.g. gpt-4o, deepseek-v4-pro)"
    ),
    base_url: str | None = typer.Option(None, "--base-url", help="override model endpoint (deepseek adapter)"),
    api_key: str | None = typer.Option(
        None,
        "--api-key",
        help="override model credential (injected into the profile's api_key_env)",
    ),
    output: Path | None = typer.Option(
        None,
        "--output",
        help="root directory for this run's products (default: <workspace>/work)",
    ),
    verbose: bool = typer.Option(False, "--verbose", help="include event payloads in live progress output"),
    debug: bool = typer.Option(False, "--debug", help="stream a readable redacted DSH/model trace summary"),
    workspace: Path | None = typer.Option(None, "--workspace", help="workspace root (default: cwd)"),
) -> None:
    """Run the full four-phase workflow for one target function."""
    ws = _workspace_root(workspace)
    request = FuzzRunRequest(
        repo=repo,
        source=source,
        function=function,
        language=Language(language),
        profile=profile,
        model_profile=model_profile,
        max_generation_loops=max_generation_loops,
        fuzz_seconds=fuzz_seconds,
        max_context_kb=max_context_kb,
        seed_corpus=seed_corpus,
        build_dir=build_dir,
    )
    validation = load_validation_profile(profile, ws)
    model = _apply_model_overrides(load_model_profile(model_profile, ws), model_name, base_url, api_key)
    run_id = create_run_id()
    private_session = ws / ".private-sessions" / run_id
    driver = DeepSeekHarnessDriver(
        provider=model.provider,
        model=model.model,
        max_tokens=model.max_tokens,
        max_input_tokens=max_input_tokens or model.max_input_tokens,
        cordis=model.cordis,
        base_url=model.base_url,
        workspace_root=ws,
        session_root=private_session,
        run_id=run_id,
        on_trace=_dsh_trace_printer(ws) if debug else None,
    )
    backend = LocalLinuxBackend(validation)
    controller = RunController(
        workspace_root=ws,
        request=request,
        profile=validation,
        driver=driver,
        backend=backend,
        model_profile=model,
        run_id=run_id,
        output_root=output,
        on_event=_event_printer(verbose),
    )
    typer.echo(f"[goaloop] run {run_id} started (repo={repo}, source={source}, function={function})")
    try:
        state = controller.run()
    finally:
        controller.close()
    _echo_state(state, run_dir=ArtifactStore(ws, state.project_name, run_id, output_root=output).run_dir)


@app.command()
def resume(
    run_id: str = typer.Option(..., "--run-id", help="run id to resume"),
    model_name: str | None = typer.Option(None, "--model-name", help="override model id"),
    base_url: str | None = typer.Option(None, "--base-url", help="override model endpoint"),
    api_key: str | None = typer.Option(None, "--api-key", help="override model credential"),
    max_input_tokens: int | None = typer.Option(
        None,
        "--max-input-tokens",
        min=1024,
        help="fail fast when a prompt is estimated to exceed this many input tokens (default: model profile)",
    ),
    output: Path | None = typer.Option(
        None,
        "--output",
        help="root directory where the run's products live (default: <workspace>/work)",
    ),
    verbose: bool = typer.Option(False, "--verbose", help="include event payloads in live progress output"),
    debug: bool = typer.Option(False, "--debug", help="stream a readable redacted DSH/model trace summary"),
    workspace: Path | None = typer.Option(None, "--workspace"),
) -> None:
    """Resume a run from its persisted checkpoint."""
    ws = _workspace_root(workspace)
    run_dir = _find_run_dir(ws, run_id, output)
    state = _load_run_state(run_dir)
    validation = load_validation_profile(state.request.profile, ws)
    model = _apply_model_overrides(load_model_profile(state.request.model_profile, ws), model_name, base_url, api_key)
    private_session = ws / ".private-sessions" / run_id
    driver = DeepSeekHarnessDriver(
        provider=model.provider,
        model=model.model,
        max_tokens=model.max_tokens,
        max_input_tokens=max_input_tokens or model.max_input_tokens,
        cordis=model.cordis,
        base_url=model.base_url,
        workspace_root=ws,
        session_root=private_session,
        run_id=run_id,
        on_trace=_dsh_trace_printer(ws) if debug else None,
    )
    backend = LocalLinuxBackend(validation)
    controller = RunController(
        workspace_root=ws,
        request=state.request,
        profile=validation,
        driver=driver,
        backend=backend,
        model_profile=model,
        run_id=run_id,
        resume=True,
        output_root=output,
        on_event=_event_printer(verbose),
    )
    typer.echo(f"[goaloop] resuming {run_id} from phase {state.phase.value}")
    try:
        try:
            state = controller.run()
        except RunLockedError as exc:
            typer.echo(f"[goaloop] resume rejected: {exc}", err=True)
            raise typer.Exit(1) from exc
    finally:
        controller.close()
    _echo_state(state, run_dir=run_dir)


@app.command()
def status(
    run_id: str = typer.Option(..., "--run-id", help="run id to inspect"),
    json_output: bool = typer.Option(False, "--json", help="dump RunState as JSON"),
    output: Path | None = typer.Option(
        None,
        "--output",
        help="root directory where the run's products live (default: <workspace>/work)",
    ),
    workspace: Path | None = typer.Option(None, "--workspace"),
) -> None:
    """Show the persisted state of one run."""
    ws = _workspace_root(workspace)
    run_dir = _find_run_dir(ws, run_id, output)
    state = _load_run_state(run_dir)
    if json_output:
        typer.echo(json.dumps(state.model_dump(mode="json"), ensure_ascii=False, indent=2))
        return
    _echo_state(state, run_dir=run_dir)


@app.command()
def report(
    run_id: str = typer.Option(..., "--run-id", help="run id to report"),
    fmt: str = typer.Option("markdown", "--format", help="markdown | json"),
    output: Path | None = typer.Option(
        None,
        "--output",
        help="root directory where the run's products live (default: <workspace>/work)",
    ),
    workspace: Path | None = typer.Option(None, "--workspace"),
) -> None:
    """Print the final report of one run."""
    ws = _workspace_root(workspace)
    run_dir = _find_run_dir(ws, run_id, output)
    _load_run_state(run_dir)  # validate the run exists and is readable
    if fmt == "json":
        path = run_dir / VALIDATION_FILENAME
        if not path.is_file():
            typer.echo(f"validation result not produced for {run_id}", err=True)
            raise typer.Exit(1)
        typer.echo(path.read_text(encoding="utf-8"))
        return
    path = run_dir / REPORT_FILENAME
    if not path.is_file():
        typer.echo(f"report not produced for {run_id}", err=True)
        raise typer.Exit(1)
    typer.echo(path.read_text(encoding="utf-8"))


@app.command()
def doctor(
    profile: str = typer.Option("default", "--profile"),
    model_profile: str = typer.Option("default", "--model-profile"),
    workspace: Path | None = typer.Option(None, "--workspace"),
) -> None:
    """Check whether this workspace can run the workflow."""
    ws = _workspace_root(workspace)
    try:
        validation = load_validation_profile(profile, ws)
    except Exception as exc:  # ProfileError
        typer.echo(f"[doctor] invalid validation profile: {exc}", err=True)
        raise typer.Exit(1) from exc
    try:
        model = load_model_profile(model_profile, ws)
    except Exception as exc:
        typer.echo(f"[doctor] invalid model profile: {exc}", err=True)
        raise typer.Exit(1) from exc

    capabilities = toolchain_capabilities(validation)
    sdk_available = _sdk_importable()
    capabilities.append(
        Capability(
            name="deepseek_harness_sdk",
            available=sdk_available,
            detail="importable" if sdk_available else "not installed",
        )
    )
    krepo_cli = krepo_cli_path(ws)
    capabilities.append(
        Capability(
            name="krepo",
            available=krepo_cli.is_file(),
            detail=str(krepo_cli) if krepo_cli.is_file() else f"missing: {krepo_cli}",
        )
    )
    env_key = os.environ.get(model.api_key_env)
    has_key = bool(env_key or model.api_key)
    source = "profile api_key" if model.api_key else f"{model.api_key_env} set"
    capabilities.append(
        Capability(
            name="model_api_key",
            available=has_key,
            detail=source if has_key else f"{model.api_key_env} missing (and no api_key in profile)",
        )
    )
    for item in capabilities:
        marker = "ok" if item.available else "MISSING"
        typer.echo(f"[doctor] {marker:<7} {item.name:<22} {item.detail}")
    typer.echo(f"[doctor] model profile {model_profile}: provider={model.provider} model={model.model}")
    ready = all(item.available for item in capabilities)
    if not ready:
        typer.echo("[doctor] environment is NOT ready", err=True)
        raise typer.Exit(1)
    typer.echo("[doctor] environment is ready")


@app.command()
def evaluate(
    suite: Path = typer.Argument(..., help="manifest.json with entries to run"),
    repetitions: int = typer.Option(3, "--repetitions", min=1, max=20),
    output: Path | None = typer.Option(
        None,
        "--output",
        help="root directory for run products and the results file (default: <workspace>/work)",
    ),
    workspace: Path | None = typer.Option(None, "--workspace"),
    debug: bool = typer.Option(False, "--debug", help="stream a readable redacted DSH/model trace summary"),
) -> None:
    """Run a suite of entries several times and summarize outcomes."""
    ws = _workspace_root(workspace)
    manifest = json.loads(suite.read_text(encoding="utf-8"))
    entries: list[dict[str, Any]] = manifest if isinstance(manifest, list) else manifest.get("entries", [])
    if not entries:
        typer.echo("[evaluate] manifest has no entries", err=True)
        raise typer.Exit(1)

    results: list[dict[str, Any]] = []
    total = len(entries) * repetitions
    done = 0
    started = time.monotonic()
    for entry in entries:
        request = FuzzRunRequest.model_validate(
            {
                "repo": entry.get("repo"),
                "source": entry["source"],
                "function": entry["function"],
                "language": entry.get("language", "auto"),
                "profile": entry.get("profile", "default"),
                "model_profile": entry.get("model_profile", "default"),
                "max_generation_loops": entry.get("max_generation_loops", 5),
                "fuzz_seconds": entry.get("fuzz_seconds", 600),
                "max_context_kb": entry.get("max_context_kb", 96),
                "seed_corpus": entry.get("seed_corpus"),
                "build_dir": entry.get("build_dir"),
            }
        )
        validation = load_validation_profile(request.profile, ws)
        model = _apply_model_overrides(
            load_model_profile(request.model_profile, ws),
            entry.get("model_name"),
            entry.get("base_url"),
            entry.get("api_key"),
        )
        for repetition in range(1, repetitions + 1):
            done += 1
            typer.echo(f"[evaluate] {done}/{total} running {request.function} (rep {repetition})")
            run_id = create_run_id()
            private_session = ws / ".private-sessions" / run_id
            driver = DeepSeekHarnessDriver(
                provider=model.provider,
                model=model.model,
                max_tokens=model.max_tokens,
                max_input_tokens=model.max_input_tokens,
                cordis=model.cordis,
                workspace_root=ws,
                session_root=private_session,
                run_id=run_id,
                on_trace=_dsh_trace_printer(ws) if debug else None,
            )
            backend = LocalLinuxBackend(validation)
            controller = RunController(
                workspace_root=ws,
                request=request,
                profile=validation,
                driver=driver,
                backend=backend,
                model_profile=model,
                run_id=run_id,
                output_root=output,
                on_event=_event_printer(False),
            )
            try:
                state = controller.run()
            finally:
                controller.close()
            run_dir = ArtifactStore(ws, state.project_name, run_id, output_root=output).run_dir
            metrics_path = run_dir / "research-metrics.json"
            metrics = json.loads(metrics_path.read_text(encoding="utf-8")) if metrics_path.is_file() else {}
            optimization = _read_optimization_analysis(run_dir)
            suggestions = optimization.get("suggestions")
            suggestion_list = suggestions if isinstance(suggestions, list) else []
            results.append(
                {
                    "run_id": run_id,
                    "repo": request.repo.as_posix() if request.repo is not None else None,
                    "source": request.source.as_posix(),
                    "function": request.function,
                    "repetition": repetition,
                    "status": state.terminal_status.value if state.terminal_status else None,
                    "generation_loops_used": state.generation_loop,
                    "first_compile_success": metrics.get("first_compile_success"),
                    "time_to_bug_seconds": metrics.get("time_to_bug_seconds"),
                    "format_retries": metrics.get("format_retries", 0),
                    "dsh_trace_path": metrics.get("dsh_trace_path"),
                    "dsh_trace_events": metrics.get("dsh_trace_events", 0),
                    "model_calls": metrics.get("model_calls", 0),
                    "model_call_seconds": metrics.get("model_call_seconds", 0.0),
                    "estimated_input_tokens": metrics.get("estimated_input_tokens", 0),
                    "model_response_chars": metrics.get("model_response_chars", 0),
                    "tool_calls": metrics.get("tool_calls", 0),
                    "optimization_suggestion_count": len(suggestion_list),
                    "optimization_suggestions": suggestion_list,
                }
            )

    summary: dict[str, dict[str, int]] = {}
    for item in results:
        key = item["function"]
        bucket = summary.setdefault(key, {})
        bucket[item["status"] or "none"] = bucket.get(item["status"] or "none", 0) + 1
    payload = {
        "suite": suite.as_posix(),
        "repetitions": repetitions,
        "duration_seconds": round(time.monotonic() - started, 3),
        "results": results,
        "summary": summary,
        "observability": _evaluate_observability(results),
        "optimization": _evaluate_optimization(results),
    }
    if output is not None:
        out_root = output.resolve()
        out_root.mkdir(parents=True, exist_ok=True)
        out_path = out_root / "evaluate-results.json"
    else:
        out_path = ws / "evaluate-results.json"
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for function, counts in summary.items():
        typer.echo(f"[evaluate] {function}: {counts}")
    typer.echo(f"[evaluate] results written to {out_path}")


def _evaluate_observability(results: list[dict[str, Any]]) -> dict[str, dict[str, int | float]]:
    aggregates: dict[str, dict[str, int | float]] = {}
    for result in results:
        function = str(result["function"])
        bucket = aggregates.setdefault(
            function,
            {
                "runs": 0,
                "trace_events": 0,
                "model_calls": 0,
                "model_call_seconds": 0.0,
                "estimated_input_tokens": 0,
                "model_response_chars": 0,
                "tool_calls": 0,
                "format_retries": 0,
            },
        )
        bucket["runs"] = int(bucket["runs"]) + 1
        for field in (
            "dsh_trace_events",
            "model_calls",
            "estimated_input_tokens",
            "model_response_chars",
            "tool_calls",
            "format_retries",
        ):
            target = "trace_events" if field == "dsh_trace_events" else field
            value = result.get(field)
            if isinstance(value, int) and not isinstance(value, bool):
                bucket[target] = int(bucket[target]) + value
        duration = result.get("model_call_seconds")
        if isinstance(duration, (int, float)) and not isinstance(duration, bool):
            bucket["model_call_seconds"] = round(float(bucket["model_call_seconds"]) + float(duration), 6)
    for bucket in aggregates.values():
        runs = int(bucket["runs"])
        bucket["average_model_call_seconds"] = round(float(bucket["model_call_seconds"]) / runs, 6)
        bucket["average_estimated_input_tokens"] = round(int(bucket["estimated_input_tokens"]) / runs, 2)
    return aggregates


def _evaluate_optimization(results: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    aggregates: dict[str, dict[str, dict[str, Any]]] = {}
    for result in results:
        function = str(result["function"])
        function_bucket = aggregates.setdefault(function, {})
        suggestions = result.get("optimization_suggestions")
        if not isinstance(suggestions, list):
            continue
        for suggestion in suggestions:
            if not isinstance(suggestion, dict) or not isinstance(suggestion.get("id"), str):
                continue
            suggestion_id = suggestion["id"]
            bucket = function_bucket.setdefault(
                suggestion_id,
                {
                    "id": suggestion_id,
                    "title": suggestion.get("title"),
                    "priority": suggestion.get("priority"),
                    "runs": 0,
                },
            )
            bucket["runs"] = int(bucket["runs"]) + 1
    return {
        function: sorted(items.values(), key=lambda item: (-int(item["runs"]), str(item["id"])))
        for function, items in aggregates.items()
    }


def _echo_state(state: RunState, *, run_dir: Path) -> None:
    typer.echo(f"[goaloop] run {state.run_id}")
    typer.echo(f"[goaloop] project: {state.project_name}")
    typer.echo(f"[goaloop] phase: {state.phase.value}")
    typer.echo(f"[goaloop] generation loops used: {state.generation_loop}")
    typer.echo(f"[goaloop] status: {state.terminal_status.value if state.terminal_status else 'in_progress'}")
    reason = _terminal_reason(run_dir)
    if reason:
        typer.echo(f"[goaloop] reason: {reason}")
    typer.echo(f"[goaloop] artifacts: {run_dir}")
    if state.terminal_status is not None and not _has_candidates(run_dir):
        typer.echo(
            "[goaloop] 注意: 该 run 未生成任何 harness 候选（iterations/ 为空），"
            "原因见上方 reason。常见原因: 预处理失败（源码/目标函数/构建目录/凭据）"
            "或模型调用失败。"
        )
    _echo_optimization_suggestions(run_dir)
    typer.echo("[goaloop] 定位: goaloop report --run-id <id> 看完整报告; 事件日志: " + str(run_dir / "events.jsonl"))


def _echo_optimization_suggestions(run_dir: Path) -> None:
    analysis = _read_optimization_analysis(run_dir)
    suggestions = analysis.get("suggestions")
    if not isinstance(suggestions, list) or not suggestions:
        return
    typer.echo(f"[goaloop] optimization suggestions: {run_dir / OPTIMIZATION_ANALYSIS_FILENAME}")
    for suggestion in suggestions:
        if not isinstance(suggestion, dict):
            continue
        priority = suggestion.get("priority", "unknown")
        title = suggestion.get("title", "未命名建议")
        recommendation = suggestion.get("recommendation", "")
        typer.echo(f"[goaloop] optimization [{priority}] {title}: {recommendation}")


def _read_optimization_analysis(run_dir: Path) -> dict[str, Any]:
    path = run_dir / OPTIMIZATION_ANALYSIS_FILENAME
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _has_candidates(run_dir: Path) -> bool:
    iterations = run_dir / "iterations"
    if not iterations.is_dir():
        return False
    return any(iterations.glob("loop-*/candidate"))


def _terminal_reason(run_dir: Path) -> str | None:
    """Specific terminal reason: the run:terminal event always carries it;
    validation.json is a fallback (older runs may store the bare status word)."""
    events_path = run_dir / "events.jsonl"
    if events_path.is_file():
        try:
            for line in reversed(events_path.read_text(encoding="utf-8").splitlines()):
                if not line.strip():
                    continue
                event = json.loads(line)
                if event.get("kind") == "run:terminal":
                    reason = event.get("payload", {}).get("reason")
                    return str(reason) if reason else None
        except (OSError, ValueError):
            pass
    validation_path = run_dir / "validation.json"
    if validation_path.is_file():
        try:
            reason = json.loads(validation_path.read_text(encoding="utf-8")).get("reason")
            if reason and reason != "in_progress":
                return str(reason)
        except (OSError, ValueError):
            pass
    return None


def _apply_model_overrides(
    model: ModelProfile,
    model_name: str | None,
    base_url: str | None,
    api_key: str | None,
) -> ModelProfile:
    """Apply CLI-level model overrides on top of the loaded model profile.

    Precedence for the credential: ``--api-key`` CLI > ``api_key`` stored in
    the profile toml > the ``api_key_env`` environment variable. The effective
    key is injected into ``api_key_env`` for this process only (never
    persisted), so both the preprocess readiness check and the SDK subprocess
    see it.
    """
    update: dict[str, Any] = {}
    if model_name is not None:
        update["model"] = model_name
    effective_base_url = base_url if base_url is not None else model.base_url
    if base_url is not None:
        update["base_url"] = base_url
    if effective_base_url is not None:
        # pi-ai reads each provider's endpoint from <PROVIDER>_BASE_URL (e.g.
        # CUSTOM_GATEWAY_BASE_URL); inject so profile/CLI base_url works for
        # pi-ai routes too, not just the deepseek adapter.
        os.environ[_provider_base_url_env(model.provider)] = effective_base_url
    effective_model = model_name if model_name is not None else model.model
    if model_name is not None:
        update["model"] = model_name
    # pi-ai's custom-gateway route resolves the model against a catalog read
    # from <PROVIDER>_MODEL (e.g. CUSTOM_GATEWAY_MODEL); without this injection
    # a profile/CLI model that differs from the catalog default fails with
    # "unknown model".
    os.environ[_provider_model_env(model.provider)] = effective_model
    effective_key = api_key if api_key is not None else model.api_key
    if effective_key is not None:
        os.environ[model.api_key_env] = effective_key
    return model.model_copy(update=update)


def _provider_base_url_env(provider: str) -> str:
    """Environment variable a pi-ai route reads its baseURL from.

    Naming convention: provider name uppercased with '-' -> '_' plus _BASE_URL,
    e.g. provider "custom-gateway" -> CUSTOM_GATEWAY_BASE_URL (the pi-ai cordis
    already reads that variable for the custom-gateway route).
    """
    return provider.upper().replace("-", "_") + "_BASE_URL"


def _provider_model_env(provider: str) -> str:
    """Environment variable a pi-ai route reads its model catalog from."""
    return provider.upper().replace("-", "_") + "_MODEL"


def _event_printer(verbose: bool) -> Callable[[RunEvent], None]:
    """Return a callback that prints phase and step progress for controller events."""

    def _print(event: RunEvent) -> None:
        payload = event.payload
        line = _progress_line(event.kind, payload)
        if line is None and not verbose:
            return
        line = line or f"step={event.kind}"
        if verbose and payload:
            line += f" details={json.dumps(payload, ensure_ascii=False, default=str)[:500]}"
        typer.echo(f"[goaloop] phase={event.phase.value} {line}")

    return _print


def _dsh_trace_printer(workspace_root: Path) -> Callable[[str, dict[str, Any]], None]:
    """Return a callback that renders a filtered, redacted DSH progress view."""

    formatter = DshTraceTerminalFormatter()

    def _print(method: str, payload: dict[str, Any]) -> None:
        for line in formatter.format(method, payload):
            typer.echo(f"[goaloop][debug][dsh] {redact(line, workspace_root)}")

    return _print


def _progress_line(kind: str, payload: dict[str, object]) -> str | None:
    loop = payload.get("loop")
    loop_part = f" loop={loop}" if loop is not None else ""
    messages = {
        "preprocess:started": f"step=started repo={payload.get('repo')} source={payload.get('source')}",
        "preprocess:krepo_started": f"step=krepo_started file={payload.get('file')}",
        "preprocess:krepo_command": f"step=krepo_command command={_shell_command(payload.get('argv'))}",
        "preprocess:krepo_completed": (
            f"step=krepo_completed incoming_lines={payload.get('incoming_lines')} "
            f"outgoing_lines={payload.get('outgoing_lines')} "
            f"params={payload.get('param_constraints')}"
        ),
        "preprocess:krepo_failed": f"step=krepo_failed reason={payload.get('reason')}",
        "preprocess:done": f"step=completed ready={payload.get('ready')} duration={payload.get('duration')}s",
        "phase:enter": "step=entered",
        "phase:resume": "step=resumed",
        "generation:model_started": f"step=model_generation_started{loop_part}",
        "generation:model_completed": f"step=model_generation_completed{loop_part} files={payload.get('files')}",
        "generation:validation_started": f"step=artifact_validation_started{loop_part}",
        "generation:validation_completed": f"step=artifact_validation_completed{loop_part}",
        "generation:driver_unavailable": f"step=model_generation_failed{loop_part}",
        "generation:model_invalid": f"step=model_output_invalid{loop_part}",
        "generation:policy_rejected": f"step=artifact_policy_rejected{loop_part}",
        "execution:materialized": f"step=candidate_materialized{loop_part}",
        "execution:checkpoint_resumed": (
            f"step=checkpoint_resumed{loop_part} stage={payload.get('stage')}"
        ),
        "execution:cmake_configure_started": f"step=cmake_configure_started{loop_part}",
        "execution:cmake_configure": f"step=cmake_configure_completed{loop_part} exit={payload.get('exit_code')}",
        "execution:cmake_build_started": f"step=cmake_build_started{loop_part}",
        "execution:cmake_build": f"step=cmake_build_completed{loop_part} exit={payload.get('exit_code')}",
        "execution:cmake_library": f"step=cmake_library_selected{loop_part}",
        "execution:compile_started": f"step=compile_started{loop_part}",
        "execution:compile": f"step=compile_completed{loop_part} exit={payload.get('exit_code')}",
        "execution:fuzz_started": f"step=fuzz_started{loop_part} budget={payload.get('seconds')}s",
        "execution:fuzz": (
            f"step=fuzz_completed{loop_part} exit={payload.get('exit_code')} duration={payload.get('duration')}s"
        ),
        "execution:coverage_started": f"step=coverage_started{loop_part}",
        "execution:coverage": f"step=coverage_completed{loop_part} ok={payload.get('ok')}",
        "execution:decided": (
            f"step=decision_completed{loop_part} decision={payload.get('decision')} "
            f"target_hit={payload.get('target_function_hit')} target_cov={payload.get('target_line_coverage')}"
        ),
        "crash:analysis_started": f"step=crash_analysis_started{loop_part} artifacts={payload.get('artifacts')}",
        "crash:analysis": (
            f"step=crash_analysis_completed ownership={payload.get('ownership')} "
            f"reproductions={payload.get('reproductions')}"
        ),
        "report:write_started": f"step=report_write_started status={payload.get('status')}",
        "optimization:completed": (
            f"step=optimization_completed suggestions={payload.get('suggestions')} "
            f"priority={payload.get('highest_priority')} top={payload.get('top_suggestion')}"
        ),
        "report:written": f"step=report_written status={payload.get('status')}",
        "run:terminal": f"step=terminal status={payload.get('status')} reason={payload.get('reason')}",
        "run:resumed": f"step=terminal_cleared previous_status={payload.get('from_status')}",
        "corpus:seed": f"step=seed_corpus copied={payload.get('copied')} ok={payload.get('ok')}",
    }
    return messages.get(kind)


def _shell_command(value: object) -> str:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return "<unavailable>"
    return shlex.join(value)


def _sdk_importable() -> bool:
    import importlib.util

    return importlib.util.find_spec("deepseek_harness") is not None
