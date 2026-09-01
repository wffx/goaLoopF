"""Generation driver tests: JSON extraction, staleness checks, scripted driver."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from goaloop.driver import (
    DeepSeekHarnessDriver,
    DriverUnavailable,
    GenerationFailure,
    ScriptedGenerationDriver,
    StaleResponseError,
    build_format_retry_prompt,
    build_generation_prompt,
    estimate_tokens,
    extract_json,
)
from goaloop.models import (
    SCHEMA_VERSION,
    CapabilityReport,
    GenerationFeedback,
    GenerationGoal,
    PreprocessResult,
    SourceContext,
)

from .helpers import make_artifact_payload


def _preprocess(run_id: str = "run-d") -> PreprocessResult:
    return PreprocessResult(
        run_id=run_id,
        ready=True,
        project_name="safe",
        source_root="/tmp/ws/repos/safe",
        language="c",
        target_function="safe_parse",
        capability_report=CapabilityReport(platform="Linux", capabilities=[]),
    )


def _goal(run_id: str = "run-d", loop: int = 0) -> GenerationGoal:
    return GenerationGoal(
        run_id=run_id,
        objective="generate a harness",
        target_function="safe_parse",
        acceptance_criteria=["compiles"],
        max_generation_loops=3,
        current_loop=loop,
    )


class TestExtractJson:
    def test_plain_object(self) -> None:
        assert extract_json('{"a": 1}') == {"a": 1}

    def test_fenced_json(self) -> None:
        text = 'Here is the output:\n```json\n{"a": 2}\n```\nThat is all.'
        assert extract_json(text) == {"a": 2}

    def test_prose_around_object(self) -> None:
        text = 'Sure.\n{"a": 3}\nDone.'
        assert extract_json(text) == {"a": 3}

    def test_invalid_raises(self) -> None:
        with pytest.raises((json.JSONDecodeError, ValueError)):
            extract_json("no json at all")


class TestDriverCoercion:
    def test_staleness_rejects_wrong_run(self) -> None:
        driver = DeepSeekHarnessDriver(
            provider="p",
            model="m",
            max_tokens=None,
            cordis=None,
            workspace_root="/tmp/ws",
            session_root="/tmp/ws/.private-sessions/run-d",
            run_id="run-d",
        )
        payload = _full_payload()
        payload["run_id"] = "other-run"
        with pytest.raises(ValueError, match="different run"):
            driver._coerce(json.dumps(payload), _goal(), 1, format_retry=0)

    def test_staleness_rejects_wrong_schema_version_value(self) -> None:
        # A present-but-different schema_version is contamination: fatal, no retry.
        driver = DeepSeekHarnessDriver(
            provider="p",
            model="m",
            max_tokens=None,
            cordis=None,
            workspace_root="/tmp/ws",
            session_root="/tmp/ws/.private-sessions/run-d",
            run_id="run-d",
        )
        payload = _full_payload()
        payload["schema_version"] = "0.9"
        with pytest.raises(StaleResponseError, match="unexpected schema_version"):
            driver._coerce(json.dumps(payload), _goal(), 1, format_retry=0)

    def test_missing_schema_version_is_retryable(self) -> None:
        # A missing envelope field is a fixable output defect (e.g. the model
        # copied the prompt's example object) and must use the format retry
        # instead of killing the run as stale contamination.
        driver = DeepSeekHarnessDriver(
            provider="p",
            model="m",
            max_tokens=None,
            cordis=None,
            workspace_root="/tmp/ws",
            session_root="/tmp/ws/.private-sessions/run-d",
            run_id="run-d",
        )
        payload = _full_payload()
        payload.pop("schema_version")
        with pytest.raises(ValueError, match="missing the required schema_version"):
            driver._coerce(json.dumps(payload), _goal(), 1, format_retry=0)

    def test_missing_run_id_and_phase_are_retryable(self) -> None:
        driver = DeepSeekHarnessDriver(
            provider="p",
            model="m",
            max_tokens=None,
            cordis=None,
            workspace_root="/tmp/ws",
            session_root="/tmp/ws/.private-sessions/run-d",
            run_id="run-d",
        )
        payload = _full_payload()
        payload.pop("run_id")
        with pytest.raises(ValueError, match="missing the required run_id"):
            driver._coerce(json.dumps(payload), _goal(), 1, format_retry=0)
        payload = _full_payload()
        payload.pop("phase")
        with pytest.raises(ValueError, match="missing the required phase"):
            driver._coerce(json.dumps(payload), _goal(), 1, format_retry=0)

    def test_staleness_rejects_wrong_loop(self) -> None:
        driver = DeepSeekHarnessDriver(
            provider="p",
            model="m",
            max_tokens=None,
            cordis=None,
            workspace_root="/tmp/ws",
            session_root="/tmp/ws/.private-sessions/run-d",
            run_id="run-d",
        )
        payload = _full_payload()
        payload["generation_loop"] = 7
        with pytest.raises(ValueError, match="generation_loop"):
            driver._coerce(json.dumps(payload), _goal(), 2, format_retry=0)

    def test_valid_payload_coerces(self) -> None:
        driver = DeepSeekHarnessDriver(
            provider="p",
            model="m",
            max_tokens=None,
            cordis=None,
            workspace_root="/tmp/ws",
            session_root="/tmp/ws/.private-sessions/run-d",
            run_id="run-d",
        )
        payload = _full_payload()
        payload["generation_loop"] = 3
        artifacts = driver._coerce(json.dumps(payload), _goal(), 3, format_retry=0)
        assert artifacts.run_id == "run-d"
        assert artifacts.generation_loop == 3


def _full_payload() -> dict:
    payload = make_artifact_payload("safe", "safe_parse")
    payload.update(
        {
            "schema_version": SCHEMA_VERSION,
            "run_id": "run-d",
            "phase": "harness_generation",
        }
    )
    return payload


class TestScriptedDriver:
    def test_patches_run_metadata(self) -> None:
        payloads = [make_artifact_payload("safe", "safe_parse")]
        driver = ScriptedGenerationDriver(payloads)
        artifacts = driver.generate_artifacts(goal=_goal("run-s"), preprocess=_preprocess("run-s"), feedback=None)
        assert artifacts.run_id == "run-s"
        assert artifacts.generation_loop == 1
        assert artifacts.schema_version == SCHEMA_VERSION
        assert driver.calls == 1

    def test_exhaustion_raises(self) -> None:
        driver = ScriptedGenerationDriver([])
        with pytest.raises(GenerationFailure):
            driver.generate_artifacts(goal=_goal(), preprocess=_preprocess(), feedback=None)

    def test_unavailable_raises(self) -> None:
        driver = ScriptedGenerationDriver([make_artifact_payload("safe", "safe_parse")], unavailable=True)
        with pytest.raises(DriverUnavailable):
            driver.generate_artifacts(goal=_goal(), preprocess=_preprocess(), feedback=None)

    def test_complete_goal_records(self) -> None:
        driver = ScriptedGenerationDriver([])
        driver.complete_goal(goal=_goal(), summary="done")
        assert driver.completed_summaries == ["done"]

    def test_invalid_payload_raises_validation_error(self) -> None:
        payload = make_artifact_payload("safe", "safe_parse")
        payload["summary"] = 123  # wrong type
        driver = ScriptedGenerationDriver([payload])
        with pytest.raises(ValidationError):
            driver.generate_artifacts(goal=_goal(), preprocess=_preprocess(), feedback=None)


class TestPrompt:
    def test_prompt_contains_schema_and_loop(self) -> None:
        prompt = build_generation_prompt(
            goal=_goal(),
            preprocess=_preprocess(),
            feedback=None,
            expected_loop=1,
        )
        assert "GeneratedArtifactSet" in prompt
        assert '"run_id"' in prompt
        assert "generation loop 1" in prompt
        assert '"type":"krepo_query"' in prompt
        assert "non-function dependency" in prompt
        assert "always binds --repo, --function, and --file" in prompt

    def test_build_dir_prompt_requires_only_harness(self) -> None:
        preprocess = _preprocess().model_copy(update={"build_dir": "/tmp/ws/repos/safe"})
        prompt = build_generation_prompt(
            goal=_goal(),
            preprocess=preprocess,
            feedback=None,
            expected_loop=1,
        )
        assert "files must contain exactly one entry whose path is harness.c" in prompt
        assert "Do not generate Makefile" in prompt
        assert "<build-dir>/src/harness.cpp" in prompt
        assert 'LLVMFuzzerTestOneInput with extern "C"' in prompt
        assert "Never invent build commands" in prompt

    def test_retry_prompt_carries_error(self) -> None:
        prompt = build_format_retry_prompt("base", ValueError("bad json"), expected_loop=2)
        assert "bad json" in prompt
        assert "format_retry = 1" in prompt

    def test_latest_feedback_not_duplicated(self) -> None:
        feedback = GenerationFeedback(
            category="needs_regeneration",
            summary="candidate failed to compile: missing header",
            compile_exit_code=1,
        )
        goal = _goal().model_copy(update={"latest_feedback": feedback})
        prompt = build_generation_prompt(
            goal=goal,
            preprocess=_preprocess(),
            feedback=feedback,
            expected_loop=2,
        )
        # The goal dump must not embed latest_feedback when feedback is passed
        # separately, so the feedback appears exactly once in the prompt.
        assert '"latest_feedback"' not in prompt
        assert prompt.count("candidate failed to compile: missing header") == 1
        assert "## Latest execution feedback" in prompt

    def test_latest_feedback_in_goal_only_passed_separately(self) -> None:
        feedback = GenerationFeedback(category="policy", summary="artifacts violate policy")
        goal = _goal().model_copy(update={"latest_feedback": feedback})
        prompt = build_generation_prompt(
            goal=goal,
            preprocess=_preprocess(),
            feedback=None,
            expected_loop=2,
        )
        # When no feedback is passed to this loop, the goal dump keeps it out
        # too: the prompt must not resurrect the previous loop's feedback.
        assert "artifacts violate policy" not in prompt

    def test_estimate_tokens(self) -> None:
        assert estimate_tokens("") == 1
        assert estimate_tokens("x" * 3000) == 1000
        assert estimate_tokens("abcd") == 1


class FakeHarness:
    """Minimal stand-in for the real DeepSeekHarness SDK object."""

    def __init__(self, responses: list[str], *, finish_reason: str = "completed") -> None:
        self.responses = list(responses)
        self.finish_reason = finish_reason
        self.calls: list[tuple[str, str | None]] = []
        self.closed = False

    def run(self, prompt: str, session_id: str | None = None, on_notification=None) -> object:
        from types import SimpleNamespace

        self.calls.append((prompt, session_id))
        if on_notification is not None:
            on_notification(
                SimpleNamespace(
                    method="session.event",
                    payload={"sessionId": session_id, "event": {"type": "assistant/message"}},
                )
            )
        if not self.responses:
            raise RuntimeError("fake harness has no more responses")
        return SimpleNamespace(
            final_response=self.responses.pop(0),
            finish_reason=self.finish_reason,
            events=getattr(self, "events", []),
            notifications=getattr(self, "notifications", []),
        )

    def close(self) -> None:
        self.closed = True


def _real_driver(run_id: str = "run-live") -> DeepSeekHarnessDriver:
    return DeepSeekHarnessDriver(
        provider="deepseek-official",
        model="deepseek-v4-pro",
        max_tokens=None,
        cordis=None,
        workspace_root="/tmp/ws",
        session_root="/tmp/ws/.private-sessions/run-live",
        run_id=run_id,
    )


def _live_payload() -> dict:
    payload = make_artifact_payload("safe", "safe_parse")
    payload.update(
        {
            "schema_version": SCHEMA_VERSION,
            "run_id": "run-live",
            "phase": "harness_generation",
            "generation_loop": 1,
        }
    )
    return payload


class TestDeepSeekHarnessDriver:
    def test_successful_generation(self) -> None:
        driver = _real_driver()
        driver._harness = FakeHarness([json.dumps(_live_payload())])
        artifacts = driver.generate_artifacts(goal=_goal(), preprocess=_preprocess(), feedback=None)
        assert artifacts.generation_loop == 1
        assert driver.format_retries == 0

    def test_generation_resolves_on_demand_krepo_query_in_same_session(self, tmp_path) -> None:
        query_request = {
            "type": "krepo_query",
            "reason": "need the packet layout",
            "queries": [{"operation": "symbol", "symbol": "packet_t", "kind": "typedef"}],
        }

        class FakeQueryService:
            def query(self, query, *, on_command=None):
                if on_command is not None:
                    on_command(["python", "kRepo/main.py", "symbol", query.symbol])
                return {"ok": True, "output": "typedef struct packet packet_t;"}

        traces: list[tuple[str, dict]] = []
        driver = _real_driver()
        driver.configure_run(run_dir=tmp_path / "run")
        driver._krepo_service = FakeQueryService()  # type: ignore[assignment]
        driver.on_trace = lambda method, payload: traces.append((method, payload))
        harness = FakeHarness([json.dumps(query_request), json.dumps(_live_payload())])
        driver._harness = harness

        artifacts = driver.generate_artifacts(goal=_goal(), preprocess=_preprocess(), feedback=None)

        assert artifacts.generation_loop == 1
        assert [session for _, session in harness.calls] == ["run-live-g01", "run-live-g01"]
        assert "typedef struct packet packet_t" in harness.calls[1][0]
        assert any(method == "goaloop.krepo_query.command" for method, _ in traces)

    def test_generation_rejects_excess_krepo_queries(self, tmp_path) -> None:
        query_request = {
            "type": "krepo_query",
            "queries": [
                {"operation": "symbol", "symbol": "ONE"},
                {"operation": "symbol", "symbol": "TWO"},
                {"operation": "symbol", "symbol": "THREE"},
            ],
        }

        class FakeQueryService:
            def query(self, query, *, on_command=None):
                return {"ok": True, "output": query.symbol}

        driver = _real_driver()
        driver.configure_run(run_dir=tmp_path / "run")
        driver._krepo_service = FakeQueryService()  # type: ignore[assignment]
        driver._harness = FakeHarness([json.dumps(query_request)] * 3)

        with pytest.raises(GenerationFailure, match="query budget"):
            driver.generate_artifacts(goal=_goal(), preprocess=_preprocess(), feedback=None)

    def test_streams_sdk_notifications_to_trace_callback(self) -> None:
        traces: list[tuple[str, dict]] = []
        driver = _real_driver()
        driver.on_trace = lambda method, payload: traces.append((method, payload))
        driver._harness = FakeHarness([json.dumps(_live_payload())])

        driver.generate_artifacts(goal=_goal(), preprocess=_preprocess(), feedback=None)

        assert traces[0][0] == "goaloop.model_call.started"
        assert (
            "session.event",
            {"sessionId": "run-live-g01", "event": {"type": "assistant/message"}},
        ) in traces
        assert traces[-1][0] == "goaloop.model_call.completed"

    def test_persists_raw_sdk_trace_without_debug_callback(self, tmp_path) -> None:
        driver = _real_driver()
        run_dir = tmp_path / "run"
        driver.configure_run(run_dir=run_dir)
        driver._harness = FakeHarness([json.dumps(_live_payload())])

        driver.generate_artifacts(goal=_goal(), preprocess=_preprocess(), feedback=None)

        trace_path = run_dir / "logs" / "dsh-trace.jsonl"
        records = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
        assert [record["method"] for record in records] == [
            "goaloop.model_call.started",
            "session.event",
            "goaloop.model_call.completed",
        ]
        assert driver.trace_summary()["model_calls"]["completed"] == 1

    def test_per_loop_session_isolation(self) -> None:
        # Each generation loop must run in its own session so the runtime does
        # not accumulate every previous prompt (each embedding the source).
        driver = _real_driver("run-live")
        loop2 = _live_payload()
        loop2["generation_loop"] = 2
        harness = FakeHarness([json.dumps(_live_payload()), json.dumps(loop2)])
        driver._harness = harness
        driver.generate_artifacts(goal=_goal("run-live"), preprocess=_preprocess("run-live"), feedback=None)
        driver.generate_artifacts(
            goal=_goal("run-live", loop=1), preprocess=_preprocess("run-live"), feedback=None
        )
        assert [session for _, session in harness.calls] == ["run-live-g01", "run-live-g02"]

    def test_retry_shares_loop_session(self) -> None:
        first = _live_payload()
        first["generation_loop"] = 99  # stale loop → ValueError → one retry
        driver = _real_driver()
        driver._harness = FakeHarness([json.dumps(first), json.dumps(_live_payload())])
        artifacts = driver.generate_artifacts(goal=_goal(), preprocess=_preprocess(), feedback=None)
        assert artifacts.generation_loop == 1
        sessions = [session for _, session in driver._harness.calls]
        assert sessions == ["run-live-g01", "run-live-g01"]

    def test_complete_goal_uses_last_generation_session(self) -> None:
        driver = _real_driver()
        harness = FakeHarness([json.dumps(_live_payload()), ""])
        driver._harness = harness
        driver.generate_artifacts(goal=_goal(), preprocess=_preprocess(), feedback=None)
        driver.complete_goal(goal=_goal(), summary="harness_verified")
        assert [session for _, session in harness.calls] == ["run-live-g01", "run-live-g01"]

    def test_input_guard_fails_fast_before_sdk(self) -> None:
        driver = _real_driver()
        driver.max_input_tokens = 1000  # far below any real prompt
        harness = FakeHarness([json.dumps(_live_payload())])
        driver._harness = harness
        with pytest.raises(GenerationFailure, match="estimated at .* input tokens"):
            driver.generate_artifacts(goal=_goal(), preprocess=_preprocess(), feedback=None)
        assert harness.calls == []  # the SDK must never be called

    def test_input_guard_disabled_without_limit(self) -> None:
        driver = _real_driver()
        assert driver.max_input_tokens is None
        driver._harness = FakeHarness([json.dumps(_live_payload())])
        artifacts = driver.generate_artifacts(goal=_goal(), preprocess=_preprocess(), feedback=None)
        assert artifacts.generation_loop == 1

    def test_guard_estimates_real_preprocess(self) -> None:
        # A 96 KiB source context must stay far under a 128K window: this
        # guards the regression that made loop 2 overflow the endpoint.
        driver = _real_driver()
        driver.max_input_tokens = 131071
        big = SourceContext(
            path="src/big.c",
            sha256="0" * 64,
            content=("int safe_parse(const uint8_t *d, size_t s) { return 0; }\n" * 4000)[:96 * 1024],
        )
        preprocess = _preprocess().model_copy(update={"contexts": [big]})
        driver._harness = FakeHarness([json.dumps(_live_payload())])
        artifacts = driver.generate_artifacts(goal=_goal(), preprocess=preprocess, feedback=None)
        assert artifacts.generation_loop == 1

    def test_markdown_fenced_response(self) -> None:
        payload = _live_payload()
        text = f"Here is the output:\n```json\n{json.dumps(payload)}\n```"
        driver = _real_driver()
        driver._harness = FakeHarness([text])
        artifacts = driver.generate_artifacts(goal=_goal(), preprocess=_preprocess(), feedback=None)
        assert artifacts.summary == payload["summary"]

    def test_format_retry_recovers(self) -> None:
        first = _live_payload()
        first["generation_loop"] = 99  # stale loop → ValueError → triggers one retry
        driver = _real_driver()
        driver._harness = FakeHarness([json.dumps(first), json.dumps(_live_payload())])
        artifacts = driver.generate_artifacts(goal=_goal(), preprocess=_preprocess(), feedback=None)
        assert artifacts.generation_loop == 1
        assert driver.format_retries == 1
        assert len(driver._harness.calls) == 2  # original + retry

    def test_missing_schema_version_recovers_via_retry(self) -> None:
        # The exact failure seen on a second validation environment: the model
        # omits schema_version. It must be corrected by the format retry, not
        # terminate the run as stale contamination.
        first = _live_payload()
        first.pop("schema_version")
        driver = _real_driver()
        driver._harness = FakeHarness([json.dumps(first), json.dumps(_live_payload())])
        artifacts = driver.generate_artifacts(goal=_goal(), preprocess=_preprocess(), feedback=None)
        assert artifacts.generation_loop == 1
        assert driver.format_retries == 1
        assert len(driver._harness.calls) == 2

    def test_format_retry_exhausted_raises(self) -> None:
        bad = _live_payload()
        bad["generation_loop"] = 99
        driver = _real_driver()
        driver._harness = FakeHarness([json.dumps(bad), json.dumps(bad)])
        with pytest.raises(GenerationFailure, match="remained invalid"):
            driver.generate_artifacts(goal=_goal(), preprocess=_preprocess(), feedback=None)
        assert driver.format_retries == 1

    def test_stale_run_id_rejected_without_retry(self) -> None:
        payload = _live_payload()
        payload["run_id"] = "some-other-run"  # cross-run contamination: no format retry
        driver = _real_driver()
        driver._harness = FakeHarness([json.dumps(payload)])
        with pytest.raises(GenerationFailure):
            driver.generate_artifacts(goal=_goal(), preprocess=_preprocess(), feedback=None)
        assert driver.format_retries == 0

    def test_empty_response_with_error_reason_is_unavailable(self) -> None:
        # An endpoint error with no response is a retryable environment
        # condition (DriverUnavailable -> BLOCKED), not invalid model output.
        driver = _real_driver()
        driver._harness = FakeHarness([""], finish_reason="error")
        with pytest.raises(DriverUnavailable, match="error"):
            driver.generate_artifacts(goal=_goal(), preprocess=_preprocess(), feedback=None)

    def test_empty_completed_response_reports_finish_reason(self) -> None:
        driver = _real_driver()
        driver._harness = FakeHarness([""], finish_reason="completed")
        with pytest.raises(DriverUnavailable, match="empty response.*completed.*run-live-g01"):
            driver.generate_artifacts(goal=_goal(), preprocess=_preprocess(), feedback=None)
        assert driver.format_retries == 0

    def test_invalid_json_reports_redacted_response_previews(self) -> None:
        driver = _real_driver()
        driver._harness = FakeHarness(["not json sk-123456789", "still not json /tmp/private/file"])
        with pytest.raises(GenerationFailure) as raised:
            driver.generate_artifacts(goal=_goal(), preprocess=_preprocess(), feedback=None)
        message = str(raised.value)
        assert "response_chars=" in message
        assert "redacted_preview=" in message
        assert "sk-123456789" not in message
        assert "/tmp/private/file" not in message
        assert "<redacted>" in message
        assert "<path>" in message

    def test_empty_response_with_max_tokens_is_failure(self) -> None:
        driver = _real_driver()
        driver._harness = FakeHarness([""], finish_reason="max-tokens")
        with pytest.raises(GenerationFailure, match="max-tokens"):
            driver.generate_artifacts(goal=_goal(), preprocess=_preprocess(), feedback=None)

    def test_sdk_exception_is_driver_unavailable(self) -> None:
        driver = _real_driver()
        driver._harness = FakeHarness([])  # raises RuntimeError on run()

        class Boom:
            def run(self, prompt, session_id=None, on_notification=None) -> object:
                raise ConnectionError("network down")

        driver._harness = Boom()
        with pytest.raises(DriverUnavailable, match="SDK call failed"):
            driver.generate_artifacts(goal=_goal(), preprocess=_preprocess(), feedback=None)

    def test_complete_goal_calls_harness(self) -> None:
        driver = _real_driver()
        harness = FakeHarness([""])
        driver._harness = harness
        driver.complete_goal(goal=_goal(), summary="harness_verified")
        assert len(harness.calls) == 1
        assert "mark it complete" in harness.calls[0][0]

    def test_complete_goal_failure_raises(self) -> None:
        driver = _real_driver()

        class Boom:
            def run(self, prompt, session_id=None, on_notification=None) -> object:
                raise TimeoutError("sdk hung")

        driver._harness = Boom()
        with pytest.raises(DriverUnavailable, match="goal completion"):
            driver.complete_goal(goal=_goal(), summary="done")

    def test_close_closes_harness(self) -> None:
        driver = _real_driver()
        harness = FakeHarness([])
        driver._harness = harness
        driver.close()
        assert harness.closed

    def test_open_missing_sdk(self, monkeypatch) -> None:
        import sys

        driver = _real_driver()
        monkeypatch.setitem(sys.modules, "deepseek_harness", None)
        with pytest.raises(DriverUnavailable, match="not installed"):
            driver._open()


class TestCustomModelSupport:
    def test_driver_keeps_base_url(self) -> None:
        driver = _real_driver()
        driver.base_url = "https://proxy.example/v1"
        assert driver.base_url == "https://proxy.example/v1"

    def test_open_passes_base_url_to_sdk(self, monkeypatch) -> None:
        captured: dict = {}

        class FakeSDK:
            def __init__(self, **kwargs):
                captured.update(kwargs)

            def close(self):
                pass

        import sys

        monkeypatch.setitem(sys.modules, "deepseek_harness", type(sys)("dh"))
        import types

        fake_module = types.ModuleType("deepseek_harness")
        fake_module.DeepSeekHarness = FakeSDK
        monkeypatch.setitem(sys.modules, "deepseek_harness", fake_module)

        driver = _real_driver()
        driver.base_url = "https://proxy.example/v1"
        driver._open()
        assert captured.get("base_url") == "https://proxy.example/v1"
        assert captured.get("provider") == "deepseek-official"
        driver.close()


class TestTurnErrorExtraction:
    def test_extracts_reason_fields(self) -> None:
        from goaloop.driver import _extract_turn_error

        events = [
            {"type": "assistant/message", "data": {"message": {"content": []}}},
            {
                "type": "turn/end",
                "data": {"reason": {"kind": "error", "message": "HTTP 401", "detail": "invalid api key"}},
            },
        ]
        detail = _extract_turn_error(events, [])
        assert "401" in detail
        assert "invalid api key" in detail

    def test_extracts_error_notification(self) -> None:
        from types import SimpleNamespace

        from goaloop.driver import _extract_turn_error

        events = [{"type": "turn/end", "data": {"reason": {"kind": "error"}}}]
        notifications = [
            SimpleNamespace(method="model/error", payload={"status": 429, "message": "rate limited"})
        ]
        detail = _extract_turn_error(events, notifications)
        assert "429" in detail
        assert "rate limited" in detail

    def test_no_detail_returns_none(self) -> None:
        from goaloop.driver import _extract_turn_error

        assert _extract_turn_error([], []) is None
        assert _extract_turn_error([{"type": "turn/end", "data": {"reason": {"kind": "error"}}}], []) is None

    def test_error_reason_message_includes_detail(self) -> None:
        driver = _real_driver()
        events = [
            {"type": "turn/end", "data": {"reason": {"kind": "error", "message": "HTTP 429", "detail": "rate limited"}}}
        ]
        harness = FakeHarness([""], finish_reason="error")
        harness.events = events  # type: ignore[attr-defined]
        driver._harness = harness
        with pytest.raises(DriverUnavailable, match="HTTP 429"):
            driver.generate_artifacts(goal=_goal(), preprocess=_preprocess(), feedback=None)
