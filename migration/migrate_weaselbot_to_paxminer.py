#!/usr/bin/env python3
"""Deprecated wrapper — use migration/paxminer_migrate.py --phase weaselbot."""

from __future__ import annotations

import sys
from pathlib import Path


def _rewrite_argv(argv: list[str]) -> list[str]:
    new_argv = ["paxminer_migrate.py"]
    i = 1
    while i < len(argv):
        arg = argv[i]
        if arg == "--env":
            new_argv.extend(["--env", argv[i + 1]])
            i += 2
            continue
        if arg in ("--force", "--drop-weaselbot-schema"):
            new_argv.append(arg)
            i += 1
            continue
        if arg.startswith("--env="):
            new_argv.extend(["--env", arg.split("=", 1)[1]])
            i += 1
            continue
        i += 1
    if "--env" not in new_argv:
        print("error: --env test|prod is required", file=sys.stderr)
        raise SystemExit(2)
    new_argv.extend(["--phase", "weaselbot"])
    return new_argv


def main() -> int:
    print(
        "DEPRECATED: use migration/paxminer_migrate.py --phase weaselbot ...",
        file=sys.stderr,
    )
    migration_dir = Path(__file__).resolve().parent
    if str(migration_dir) not in sys.path:
        sys.path.insert(0, str(migration_dir))
    sys.argv = _rewrite_argv(sys.argv)
    from paxminer_migrate import main as orchestrator_main

    return orchestrator_main()


if __name__ == "__main__":
    raise SystemExit(main())
