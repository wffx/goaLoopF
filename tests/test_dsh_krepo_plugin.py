"""Smoke tests for the standalone native DSH kRepo Cordis plugin."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


def test_native_plugin_registers_and_executes_bound_tool(tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for the DSH plugin smoke test")
    session_id = "run-native-g01"
    binding_root = tmp_path / "bindings"
    binding_root.mkdir()
    binding_path = binding_root / f"{hashlib.sha256(session_id.encode()).hexdigest()}.json"
    binding_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "session_id": session_id,
                "python_executable": "/usr/bin/python3",
                "repo_root": "/tmp/repo",
                "target_function": "parse_packet",
                "target_file": "src/packet.c",
            }
        ),
        encoding="utf-8",
    )
    plugin = Path("dsh-plugins/krepo-query/index.mjs").resolve().as_uri()
    script = f"""
import {{ createKrepoQueryTool }} from {json.dumps(plugin)};
const commandSpecs = [];
const stdoutPayload = JSON.stringify({{
  ok: true,
  output: "typedef int packet_t;",
  cache_hit: false,
  command: "python kRepo/main.py symbol packet_t"
}});
const definition = createKrepoQueryTool(async (file, args, options) => {{
  commandSpecs.push({{ file, args, cwd: options.cwd }});
  return {{ stdout: stdoutPayload, stderr: "" }};
}});
const signal = new AbortController().signal;
const value = await definition.execute(
  {{
    symbol: "packet_t",
    repo: "/tmp/repo",
    function: "parse_packet",
    file: "src/packet.c",
    kind: "typedef"
  }},
  {{
    callId: "call-1",
    rootCallId: "call-1",
    signal,
    agent: {{ session: {{
      header: {{ id: {json.dumps(session_id)} }},
      events: [{{ type: "tool/call", data: {{ callId: "call-1", turn: 2, step: 3 }} }}]
    }} }}
  }}
);
const valueWithoutKind = await definition.execute(
  {{
    symbol: "packet_t",
    repo: "/tmp/repo",
    function: "parse_packet",
    file: "src/packet.c"
  }},
  {{
    callId: "call-2",
    rootCallId: "call-2",
    signal,
    agent: {{ session: {{ header: {{ id: {json.dumps(session_id)} }}, events: [] }} }}
  }}
);
console.log(JSON.stringify({{
  name: definition.name,
  required: definition.parameters.required,
  file: commandSpecs[0].file,
  argv: commandSpecs[0].args,
  cwd: commandSpecs[0].cwd,
  argvWithoutKind: commandSpecs[1].args,
  value,
  valueWithoutKind
}}));
"""
    environment = os.environ.copy()
    environment["GOALOOP_KREPO_BINDINGS_DIR"] = str(binding_root)

    completed = subprocess.run(
        [node, "--input-type=module", "--eval", script],
        cwd=Path(__file__).parent.parent,
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result["name"] == "query_krepo_symbol"
    assert result["required"] == ["symbol", "repo", "function", "file"]
    assert result["file"] == "/usr/bin/python3"
    assert result["argv"][:2] == ["-m", "goaloop.krepo_tool_bridge"]
    assert result["argv"][-10:] == [
        "--symbol",
        "packet_t",
        "--repo",
        "/tmp/repo",
        "--function",
        "parse_packet",
        "--file",
        "src/packet.c",
        "--kind",
        "typedef",
    ]
    assert result["cwd"] == "/tmp/repo"
    assert result["value"]["output"] == "typedef int packet_t;"
    assert result["value"]["function"] == "parse_packet"
    assert "--kind" not in result["argvWithoutKind"]
    assert "kind" not in result["valueWithoutKind"]
