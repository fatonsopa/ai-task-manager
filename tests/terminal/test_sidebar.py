"""Application interaction controls; real PTY coverage is a separate layer."""

import asyncio

from textual.containers import VerticalScroll

from ai_task_manager.presentation.terminal.sidebar import Control, Description, SidebarApp
from ai_task_manager.presentation.terminal.snapshot import Snapshot, TaskRow


class Reader:
    def __init__(self, snapshot):
        self.value = snapshot

    def snapshot(self):
        return self.value


def rows(count=2):
    return tuple(
        TaskRow(
            f"task-{i}",
            f"TST-{i}",
            f"Owned task {i}",
            ("A long description with 界 and wrapped words. " * 20)
            if i == 0
            else "Short description",
            "Todo",
            "blocked" if i == 0 else "queued",
            ("task-1", "foreign") if i == 0 else (),
            "confirmed",
        )
        for i in range(count)
    )


def find(app, kind, key=""):
    return next(c for c in app.query(Control) if c.kind == kind and c.key == key)


def test_compact_groups_inline_preview_more_less_dependency_and_mouse():
    async def scenario():
        app = SidebarApp(Reader(Snapshot(rows())), refresh_seconds=30)
        async with app.run_test(size=(42, 20)) as pilot:
            await pilot.pause()
            assert str(find(app, "group", "Todo").content) == "▾ Todo (2)"
            assert "Blocked" not in str(find(app, "task", "task-0").content)
            await pilot.click(find(app, "task", "task-0"))
            await pilot.pause()
            description = app.query_one(Description)
            assert len(description.visible_lines) == 5 and description.total_lines > 5
            assert len(list(app.query(VerticalScroll))) == 1
            await pilot.click(find(app, "more", "task-0"))
            await pilot.pause()
            assert len(app.query_one(Description).visible_lines) > 5
            app.action_scroll_home()
            await pilot.pause()
            find(app, "more", "task-0").focus()
            await pilot.press("enter")
            await pilot.pause()
            assert len(app.query_one(Description).visible_lines) == 5
            await pilot.click(find(app, "dependency", "task-1"))
            await pilot.pause()
            assert "task-1" in app.view.expanded_tasks
            assert not any(c.key == "foreign" for c in app.query(Control))

    asyncio.run(scenario())


def test_keyboard_group_counts_hide_show_narrow_resize_and_unknown_refresh():
    async def scenario():
        reader = Reader(Snapshot(rows()))
        app = SidebarApp(reader, ascii=True, color=False, refresh_seconds=30)
        async with app.run_test(size=(40, 18)) as pilot:
            await pilot.pause()
            find(app, "group", "Todo").focus()
            await pilot.press("enter")
            await pilot.pause()
            assert "Todo" in app.view.collapsed_groups
            assert str(find(app, "group", "Todo").content) == "> Todo (2)"
            assert not [c for c in app.query(Control) if c.kind == "task"]
            await pilot.press("h")
            assert app.view.hidden and not app.query_one("#task-scroll").display
            await pilot.click(find(app, "visibility"))
            assert not app.view.hidden
            await pilot.resize_terminal(10, 10)
            await pilot.pause()
            assert not app.query_one("#task-scroll").display
            assert str(find(app, "visibility").content) == "[show]"
            await pilot.resize_terminal(40, 18)
            await pilot.pause()
            reader.value = Snapshot((), available=False)
            await app.read_snapshot()
            assert "unavailable" in str(app.query_one("#empty").content)
            assert not [c for c in app.query(Control) if c.kind == "task"]

    asyncio.run(scenario())


def test_whole_sidebar_scroll_and_wrapping_recomputed_after_resize():
    async def scenario():
        app = SidebarApp(Reader(Snapshot(rows(20))), refresh_seconds=30)
        async with app.run_test(size=(44, 14)) as pilot:
            await pilot.pause()
            await pilot.click(find(app, "task", "task-0"))
            await pilot.pause()
            before = app.query_one(Description).total_lines
            await pilot.resize_terminal(26, 14)
            await pilot.pause()
            assert app.query_one(Description).total_lines > before
            assert len(app.query_one(Description).visible_lines) == 5
            app.action_scroll_end()
            await pilot.pause()
            viewport = app.query_one("#task-scroll", VerticalScroll)
            assert viewport.scroll_y > 0
            assert len(list(app.query(VerticalScroll))) == 1
            assert viewport.size.height == app.size.height - 1

    asyncio.run(scenario())
