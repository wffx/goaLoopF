"""Coverage attribution tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from goaloop.coverage import CoverageMeasurementError, _parse_export


def _export_payload(source_root: Path) -> dict:
    safe_c = str(source_root / "src" / "safe.c")
    return {
        "data": [
            {
                "files": [
                    {
                        "filename": safe_c,
                        "segments": [
                            [1, 1, 2, 1, 1],
                            [4, 1, 3, 1, 1],
                            [5, 1, 0, 1, 1],
                            [6, 1, 0, 0, 0],
                        ],
                    },
                    {
                        "filename": "/other/project/not-target.c",
                        "segments": [[1, 1, 9, 1, 1]],
                    },
                ],
                "functions": [
                    {"name": "safe_parse", "count": 5, "filename": safe_c},
                    {"name": "LLVMFuzzerTestOneInput", "count": 5, "filename": "/candidate/harness.c"},
                ],
            }
        ]
    }


def test_parse_export_target_hit_and_coverage(tmp_path: Path) -> None:
    source_root = tmp_path / "repos" / "safe"
    metrics = _parse_export(_export_payload(source_root), source_root=source_root, target_function="safe_parse")
    assert metrics.target_function_hit is True
    assert metrics.target_line_coverage == 66.67  # 2 of 3 counted lines covered
    assert metrics.target_line_delta == 66.67


def test_parse_export_missing_target_function(tmp_path: Path) -> None:
    source_root = tmp_path / "repos" / "safe"
    metrics = _parse_export(_export_payload(source_root), source_root=source_root, target_function="other_fn")
    assert metrics.target_function_hit is False


def test_parse_export_no_attribution_raises(tmp_path: Path) -> None:
    payload = {
        "data": [
            {
                "files": [
                    {
                        "filename": "/candidate/harness.c",
                        "segments": [[1, 1, 2, 1, 1]],
                    }
                ],
                "functions": [],
            }
        ]
    }
    with pytest.raises(CoverageMeasurementError, match="cannot be attributed"):
        _parse_export(payload, source_root=tmp_path / "repos" / "safe", target_function="f")


def test_parse_export_empty_raises(tmp_path: Path) -> None:
    with pytest.raises(CoverageMeasurementError, match="contains no files"):
        _parse_export({"data": [{"files": [], "functions": []}]}, source_root=tmp_path, target_function="f")
