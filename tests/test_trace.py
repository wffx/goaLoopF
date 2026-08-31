"""Raw DSH trace persistence and aggregation tests."""

from __future__ import annotations

import json
from pathlib import Path

from goaloop.trace import DshTraceRecorder, DshTraceTerminalFormatter


def test_terminal_formatter_aggregates_stream_chunks() -> None:
    formatter = DshTraceTerminalFormatter(stream_report_chars=5)
    first = formatter.format(
        "session.event",
        {
            "sessionId": "s",
            "event": {
                "type": "assistant/chunk",
                "data": {"turn": 1, "step": 2, "chunk": {"type": "reasoning-delta", "text": "abc"}},
            },
        },
    )
    second = formatter.format(
        "session.event",
        {
            "sessionId": "s",
            "event": {
                "type": "assistant/chunk",
                "data": {"turn": 1, "step": 2, "chunk": {"type": "reasoning-delta", "text": "def"}},
            },
        },
    )

    assert first == []
    assert second == ["model streaming turn=1 step=2 reasoning=6 chars answer=0 chars chunks=2"]


def test_terminal_formatter_summarizes_artifact_without_dumping_files() -> None:
    formatter = DshTraceTerminalFormatter()
    artifact = {
        "phase": "harness_generation",
        "generation_loop": 2,
        "candidate_ready": True,
        "summary": "candidate ready",
        "files": [{"path": "harness.c", "content": "very large source"}],
    }

    lines = formatter.format(
        "session.event",
        {
            "sessionId": "s",
            "event": {
                "type": "assistant/message",
                "data": {
                    "turn": 1,
                    "step": 2,
                    "message": {"content": [{"type": "text", "text": json.dumps(artifact)}]},
                },
            },
        },
    )

    assert lines == ["assistant artifact turn=1 step=2 loop=2 ready=True files=1 summary=candidate ready"]
    assert "very large source" not in lines[0]


def test_terminal_formatter_summarizes_tool_call_and_result() -> None:
    formatter = DshTraceTerminalFormatter()
    call = formatter.format(
        "session.event",
        {
            "sessionId": "s",
            "event": {
                "type": "tool/call",
                "data": {"turn": 1, "step": 1, "name": "create_goal", "arguments": '{"rounds":3}'},
            },
        },
    )
    result = formatter.format(
        "session.event",
        {
            "sessionId": "s",
            "event": {
                "type": "tool/result",
                "data": {
                    "turn": 1,
                    "step": 1,
                    "message": {
                        "content": [
                            {
                                "type": "tool-result",
                                "isError": False,
                                "content": [{"type": "text", "text": "goal created"}],
                            }
                        ]
                    },
                },
            },
        },
    )

    assert call == ['tool call turn=1 step=1 name=create_goal args={"rounds":3}']
    assert result == ["tool result turn=1 step=1 error=False output=12 chars preview=goal created"]


def test_records_raw_payload_without_redaction(tmp_path: Path) -> None:
    recorder = DshTraceRecorder(run_id="run-trace", logs_dir=tmp_path / "logs")
    secret = "sk-raw-secret-123456789"
    source_path = "/private/repo/src/target.c"

    recorder.record(
        "session.event",
        {
            "sessionId": "run-trace-g01",
            "event": {
                "type": "assistant/message",
                "content": f"token={secret} source={source_path}",
            },
        },
    )

    record = json.loads(recorder.trace_path.read_text(encoding="utf-8"))
    assert secret in record["payload"]["event"]["content"]
    assert source_path in record["payload"]["event"]["content"]


def test_summary_aggregates_model_and_tool_events(tmp_path: Path) -> None:
    recorder = DshTraceRecorder(run_id="run-trace", logs_dir=tmp_path / "logs")
    recorder.record(
        "goaloop.model_call.started",
        {"prompt_chars": 900, "estimated_input_tokens": 300},
    )
    recorder.record(
        "session.event",
        {"event": {"type": "tool/call", "name": "query_krepo_symbol"}},
    )
    recorder.record(
        "session.event",
        {"event": {"type": "tool/result", "name": "query_krepo_symbol"}},
    )
    recorder.record(
        "goaloop.model_call.completed",
        {"duration_seconds": 1.25, "finish_reason": "completed", "response_chars": 450},
    )

    summary = recorder.snapshot()

    assert summary["event_count"] == 4
    assert summary["tool_calls"] == 1
    assert summary["tool_results"] == 1
    assert summary["model_calls"] == {
        "started": 1,
        "completed": 1,
        "failed": 0,
        "duration_seconds": 1.25,
        "prompt_chars": 900,
        "estimated_input_tokens": 300,
        "response_chars": 450,
        "finish_reasons": {"completed": 1},
    }
    persisted = json.loads(recorder.summary_path.read_text(encoding="utf-8"))
    assert persisted == summary


def test_resume_rebuilds_existing_trace_and_continues_sequence(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    first = DshTraceRecorder(run_id="run-trace", logs_dir=logs)
    first.record("session.status", {"status": "running"})

    resumed = DshTraceRecorder(run_id="run-trace", logs_dir=logs)
    resumed.record("session.status", {"status": "idle"})

    records = [json.loads(line) for line in resumed.trace_path.read_text(encoding="utf-8").splitlines()]
    assert [record["sequence"] for record in records] == [1, 2]
    assert resumed.snapshot()["methods"] == {"session.status": 2}
