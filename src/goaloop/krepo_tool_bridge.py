"""Narrow stdio bridge used by the native DSH kRepo query tool."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .krepo import KRepoError, execute_krepo_tool_query


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--binding", type=Path, required=True)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--function", required=True)
    parser.add_argument("--file", required=True)
    parser.add_argument("--kind")
    args = parser.parse_args()
    try:
        result = execute_krepo_tool_query(
            args.binding,
            symbol=args.symbol,
            kind=args.kind,
            repo_root=args.repo,
            target_function=args.function,
            target_file=args.file,
        )
    except (KRepoError, OSError, ValueError) as exc:
        result = {"ok": False, "error": str(exc)}
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
