"""Typer CLI for the goaloop-fuzz workflow."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import typer

from .backend import LocalLinuxBackend, toolchain_capabilities
from .config import load_model_profile, load_validation_profile
from .driver import DeepSeekHarnessDriver
from .models import Capability, FuzzRunRequest, Language, ModelProfile, RunEvent, RunState
from .report import REPORT_FILENAME, VALIDATION_FILENAME
from .storage import ArtifactStore, create_run_id
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


def _find_run_dir(workspace_root: Path, run_id: str) -> Path:
    matches = sorted((workspace_root / "work").glob(f"*/runs/{run_id}"))
    if not matches:
        raise typer.BadParameter(f"run {run_id!r} was not found under {workspace_root / 'work'}")
    return matches[0]


def _load_run_state(run_dir: Path) -> RunState:
    state_path = run_dir / "state.json"
    if not state_path.is_file():
        raise typer.BadParameter(f"run directory {run_dir} has no state.json")
    return RunState.model_validate_json(state_path.read_text(encoding="utf-8"))


@app.command()
def run(
    source: Path = typer.Option(..., "--source", help="source directory below repos/"),
    function: str = typer.Option(..., "--function", help="target function symbol"),
    language: str = typer.Option("auto", "--language", help="auto | c | cpp"),
    profile: str = typer.Option("default", "--profile", help="validation profile name"),
    model_profile: str = typer.Option("default", "--model-profile", help="model profile name"),
    max_generation_loops: int = typer.Option(5, "--max-generation-loops", min=1, max=20),
    fuzz_seconds: int = typer.Option(600, "--fuzz-seconds", min=1, max=86400),
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
    verbose: bool = typer.Option(False, "--verbose", help="print live progress events"),
    workspace: Path | None = typer.Option(None, "--workspace", help="workspace root (default: cwd)"),
) -> None:
    """Run the full four-phase workflow for one target function."""
    ws = _workspace_root(workspace)
    request = FuzzRunRequest(
        source=source,
        function=function,
        language=Language(language),
        profile=profile,
        model_profile=model_profile,
        max_generation_loops=max_generation_loops,
        fuzz_seconds=fuzz_seconds,
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
        cordis=model.cordis,
        base_url=model.base_url,
        workspace_root=ws,
        session_root=private_session,
        run_id=run_id,
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
        on_event=_verbose_event_printer(verbose),
    )
    typer.echo(f"[goaloop] run {run_id} started (source={source}, function={function})")
    try:
        state = controller.run()
    finally:
        controller.close()
    _echo_state(state, run_dir=ArtifactStore(ws, state.project_name, run_id).run_dir)


@app.command()
def resume(
    run_id: str = typer.Option(..., "--run-id", help="run id to resume"),
    model_name: str | None = typer.Option(None, "--model-name", help="override model id"),
    base_url: str | None = typer.Option(None, "--base-url", help="override model endpoint"),
    api_key: str | None = typer.Option(None, "--api-key", help="override model credential"),
    verbose: bool = typer.Option(False, "--verbose", help="print live progress events"),
    workspace: Path | None = typer.Option(None, "--workspace"),
) -> None:
    """Resume a run from its persisted checkpoint."""
    ws = _workspace_root(workspace)
    run_dir = _find_run_dir(ws, run_id)
    state = _load_run_state(run_dir)
    validation = load_validation_profile(state.request.profile, ws)
    model = _apply_model_overrides(load_model_profile(state.request.model_profile, ws), model_name, base_url, api_key)
    private_session = ws / ".private-sessions" / run_id
    driver = DeepSeekHarnessDriver(
        provider=model.provider,
        model=model.model,
        max_tokens=model.max_tokens,
        cordis=model.cordis,
        base_url=model.base_url,
        workspace_root=ws,
        session_root=private_session,
        run_id=run_id,
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
        on_event=_verbose_event_printer(verbose),
    )
    typer.echo(f"[goaloop] resuming {run_id} from phase {state.phase.value}")
    try:
        state = controller.run()
    finally:
        controller.close()
    _echo_state(state, run_dir=run_dir)


@app.command()
def status(
    run_id: str = typer.Option(..., "--run-id", help="run id to inspect"),
    json_output: bool = typer.Option(False, "--json", help="dump RunState as JSON"),
    workspace: Path | None = typer.Option(None, "--workspace"),
) -> None:
    """Show the persisted state of one run."""
    ws = _workspace_root(workspace)
    state = _load_run_state(_find_run_dir(ws, run_id))
    if json_output:
        typer.echo(json.dumps(state.model_dump(mode="json"), ensure_ascii=False, indent=2))
        return
    _echo_state(state, run_dir=_find_run_dir(ws, run_id))


@app.command()
def report(
    run_id: str = typer.Option(..., "--run-id", help="run id to report"),
    fmt: str = typer.Option("markdown", "--format", help="markdown | json"),
    workspace: Path | None = typer.Option(None, "--workspace"),
) -> None:
    """Print the final report of one run."""
    ws = _workspace_root(workspace)
    run_dir = _find_run_dir(ws, run_id)
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
    workspace: Path | None = typer.Option(None, "--workspace"),
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
                "source": entry["source"],
                "function": entry["function"],
                "language": entry.get("language", "auto"),
                "profile": entry.get("profile", "default"),
                "model_profile": entry.get("model_profile", "default"),
                "max_generation_loops": entry.get("max_generation_loops", 5),
                "fuzz_seconds": entry.get("fuzz_seconds", 600),
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
                cordis=model.cordis,
                workspace_root=ws,
                session_root=private_session,
                run_id=run_id,
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
            )
            try:
                state = controller.run()
            finally:
                controller.close()
            metrics_path = ArtifactStore(ws, state.project_name, run_id).run_dir / "research-metrics.json"
            metrics = json.loads(metrics_path.read_text(encoding="utf-8")) if metrics_path.is_file() else {}
            results.append(
                {
                    "run_id": run_id,
                    "source": request.source.as_posix(),
                    "function": request.function,
                    "repetition": repetition,
                    "status": state.terminal_status.value if state.terminal_status else None,
                    "generation_loops_used": state.generation_loop,
                    "first_compile_success": metrics.get("first_compile_success"),
                    "time_to_bug_seconds": metrics.get("time_to_bug_seconds"),
                }
            )

    summary: dict[str, dict[str, int]] = {}
    for item in results:
        key = item["function"]
        bucket = summary.setdefault(key, {})
        bucket[item["status"] or "none"] = bucket.get(item["status"] or "none", 0) + 1
    output = {
        "suite": suite.as_posix(),
        "repetitions": repetitions,
        "duration_seconds": round(time.monotonic() - started, 3),
        "results": results,
        "summary": summary,
    }
    out_path = ws / "evaluate-results.json"
    out_path.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for function, counts in summary.items():
        typer.echo(f"[evaluate] {function}: {counts}")
    typer.echo(f"[evaluate] results written to {out_path}")


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
    typer.echo("[goaloop] 定位: goaloop report --run-id <id> 看完整报告; 事件日志: " + str(run_dir / "events.jsonl"))


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


def _verbose_event_printer(enabled: bool) -> Callable[[RunEvent], None] | None:
    """Return a callback that prints a human-readable line per controller event."""

    def _print(event: RunEvent) -> None:
        payload = event.payload
        line: str
        if event.kind == "preprocess:done":
            line = f"preprocess ready={payload.get('ready')}"
        elif event.kind == "execution:compile":
            line = f"compile loop={payload.get('loop')} exit={payload.get('exit_code')}"
        elif event.kind == "execution:fuzz":
            line = (
                f"fuzz loop={payload.get('loop')} exit={payload.get('exit_code')} duration={payload.get('duration')}s"
            )
        elif event.kind == "execution:coverage":
            line = f"coverage loop={payload.get('loop')} ok={payload.get('ok')}"
        elif event.kind == "execution:decided":
            line = (
                f"decided loop={payload.get('loop')} disposition={payload.get('disposition')} "
                f"decision={payload.get('decision')} target_hit={payload.get('target_function_hit')} "
                f"target_cov={payload.get('target_line_coverage')}"
            )
        elif event.kind == "generation:policy_rejected":
            line = f"policy_rejected loop={payload.get('loop')}: {payload.get('reason')}"
        elif event.kind == "crash:analysis":
            line = (
                f"crash_analysis ownership={payload.get('ownership')} "
                f"reproductions={payload.get('reproductions')} sanitizer={payload.get('sanitizer')}"
            )
        elif event.kind == "run:terminal":
            line = f"terminal status={payload.get('status')}: {payload.get('reason')}"
        elif event.kind == "corpus:seed":
            line = f"corpus_seed ok={payload.get('ok')} copied={payload.get('copied')}"
        else:
            line = f"{event.kind} {json.dumps(payload, ensure_ascii=False)[:200]}"
        typer.echo(f"[goaloop] {line}")

    return _print if enabled else None


def _sdk_importable() -> bool:
    import importlib.util

    return importlib.util.find_spec("deepseek_harness") is not None
