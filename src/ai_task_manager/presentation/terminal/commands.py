"""Additive read-only terminal routes; no task or host mutation commands."""

import math
import os

from .snapshot import SessionReader, status_line


def show(service, action, *, ascii=False, color=True, refresh_seconds=1, layout_context=None):
    reader = SessionReader(service)
    if action == "status":
        snapshot = reader.snapshot()
        print(status_line(snapshot, ascii=ascii))
        return 0 if snapshot.available else 6
    if action != "sidebar":
        raise ValueError("View accepts sidebar or status")
    if not math.isfinite(refresh_seconds) or not 0.2 <= refresh_seconds <= 30:
        raise ValueError("View refresh must be between 0.2 and 30 seconds")
    from .sidebar import SidebarApp

    layout = None
    if layout_context:
        from .pane_control import PaneControl

        layout = PaneControl(layout_context, service)

    SidebarApp(
        reader,
        ascii=ascii,
        color=color and "NO_COLOR" not in os.environ,
        refresh_seconds=refresh_seconds,
        layout=layout,
    ).run()
    return 0
