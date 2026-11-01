"""Compatibility alias for the product's single owned-group implementation."""

import sys
from pathlib import Path

# Preserve the source-tree build entrypoint before wheel packaging exists.
_source = str(Path(__file__).resolve().parents[2] / "src")
if _source not in sys.path:
    sys.path.insert(0, _source)
from ai_task_manager.adapters.process import groups  # noqa: E402

sys.modules[__name__] = groups
