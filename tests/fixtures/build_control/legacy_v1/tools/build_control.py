#!/usr/bin/env python3
"""Stable build-control API and direct CLI entry point.

This is build scaffolding, separate from the product task-state engine.
Paid charges stay reserved until the caller supplies actual settlement evidence.
"""

if __package__:
    from .build_support.cli import main
    from .build_support.ledger import BuildControl
    from .build_support.process import run_command
    from .build_support.storage import SCHEMA_VERSION
    from .build_support.values import MAX_BUDGET_MICRO, MAX_SECONDS, ControlError
else:
    from build_support.cli import main
    from build_support.ledger import BuildControl
    from build_support.process import run_command
    from build_support.storage import SCHEMA_VERSION
    from build_support.values import MAX_BUDGET_MICRO, MAX_SECONDS, ControlError

__all__ = [
    "BuildControl",
    "ControlError",
    "MAX_SECONDS",
    "MAX_BUDGET_MICRO",
    "SCHEMA_VERSION",
    "run_command",
    "main",
]

if __name__ == "__main__":
    raise SystemExit(main())
