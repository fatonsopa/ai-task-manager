"""Interactive companion pane with one scroll region and local view state."""

import asyncio
import os
from dataclasses import dataclass, field

from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.widgets import Static

from .snapshot import ACTIVITIES, Snapshot
from .text import rendered_lines


@dataclass
class ViewState:
    hidden: bool = False
    collapsed_groups: set[str] = field(default_factory=set)
    expanded_tasks: set[str] = field(default_factory=set)
    full_descriptions: set[str] = field(default_factory=set)


class Control(Static, can_focus=True):
    class Selected(Message):
        def __init__(self, kind, key):
            super().__init__()
            self.kind, self.key = kind, key

    def __init__(self, label, kind, key="", **options):
        super().__init__(label, markup=False, **options)
        self.kind, self.key = kind, key

    def activate(self):
        self.post_message(self.Selected(self.kind, self.key))

    def on_click(self, event: events.Click):
        event.stop()
        self.activate()

    def on_key(self, event: events.Key):
        if event.key in {"enter", "space"}:
            event.stop()
            self.activate()


class Description(Static):
    def __init__(self, body, full):
        super().__init__(markup=False)
        self.body, self.full = body, full
        self.visible_lines = []
        self.total_lines = 0

    def on_resize(self):
        self.reflow()

    def on_mount(self):
        self.call_after_refresh(self.reflow)

    def reflow(self):
        lines = rendered_lines(self.body, max(1, self.content_size.width))
        self.total_lines = len(lines)
        self.visible_lines = lines if self.full else lines[:5]
        self.update(Text("\n".join(self.visible_lines), overflow="crop", no_wrap=True))
        parent = self.parent
        if parent is not None:
            parent.query_one(".more", Control).display = len(lines) > 5


class SidebarApp(App):
    CSS = """
    Screen { background: $surface; overflow: hidden; }
    #pane-header { height: 1; width: 100%; }
    #pane-title { width: 1fr; height: 1; text-style: bold; }
    #visibility { width: 8; height: 1; text-align: right; }
    #task-scroll { height: 1fr; width: 100%; scrollbar-size: 1 1; padding: 0; }
    Control { height: 1; width: 100%; overflow: hidden; }
    Control:focus { background: $boost; text-style: bold; }
    .group { text-style: bold; margin-top: 1; }
    .card { height: auto; width: 100%; }
    .details { height: auto; width: 100%; padding: 0 0 0 2; }
    Description { height: auto; width: 100%; }
    .activity { height: auto; width: 100%; }
    .more { color: $accent; }
    .dependency { height: auto; color: $accent; }
    #empty { height: auto; }
    """
    BINDINGS = [
        ("j", "focus_next", "Next"),
        ("k", "focus_previous", "Previous"),
        ("down", "focus_next", "Next"),
        ("up", "focus_previous", "Previous"),
        ("h", "toggle_sidebar", "Hide/show"),
        ("escape", "hide_sidebar", "Hide"),
        ("pageup", "scroll_page_up", "Page up"),
        ("pagedown", "scroll_page_down", "Page down"),
        ("home", "scroll_home", "Top"),
        ("end", "scroll_end", "Bottom"),
        ("q", "quit", "Close view"),
    ]

    def __init__(self, reader, *, ascii=False, color=True, refresh_seconds=1, layout=None):
        no_color = not color or "NO_COLOR" in os.environ
        previous = os.environ.get("NO_COLOR")
        if no_color:
            os.environ["NO_COLOR"] = "1"
        try:
            super().__init__(ansi_color=True)
        finally:
            if previous is None:
                os.environ.pop("NO_COLOR", None)
            else:
                os.environ["NO_COLOR"] = previous
        self.reader, self.ascii = reader, ascii
        self.use_color = not no_color
        self.refresh_seconds, self.layout = refresh_seconds, layout
        self.view = ViewState()
        self.snapshot = Snapshot(())
        self.refreshing = False

    def compose(self) -> ComposeResult:
        with Horizontal(id="pane-header"):
            yield Static("Session tasks", id="pane-title", markup=False)
            yield Control("[hide]", "visibility", id="visibility")
        yield VerticalScroll(id="task-scroll")

    async def on_mount(self):
        await self.read_snapshot()
        self.set_interval(self.refresh_seconds, self.read_snapshot)

    async def read_snapshot(self):
        if self.refreshing:
            return
        self.refreshing = True
        try:
            snapshot = await asyncio.to_thread(self.reader.snapshot)
            if snapshot != self.snapshot or not self.query_one("#task-scroll").children:
                self.snapshot = snapshot
                await self.rebuild()
        finally:
            self.refreshing = False

    async def rebuild(self):
        viewport = self.query_one("#task-scroll", VerticalScroll)
        old_scroll = viewport.scroll_y
        focus = self.focused
        focus_key = (focus.kind, focus.key) if isinstance(focus, Control) else None
        await viewport.remove_children()
        authorized = {task.id: task for task in self.snapshot.tasks}
        self.view.expanded_tasks.intersection_update(authorized)
        self.view.full_descriptions.intersection_update(authorized)
        if not self.snapshot.available or not self.snapshot.tasks:
            await viewport.mount(
                Static(
                    "Session view unavailable"
                    if not self.snapshot.available
                    else "No tasks in this session",
                    id="empty",
                    markup=False,
                )
            )
        for status, rows in self.snapshot.groups():
            opened = status not in self.view.collapsed_groups
            arrow = ("v" if opened else ">") if self.ascii else ("▾" if opened else "▸")
            await viewport.mount(
                Control(f"{arrow} {status} ({len(rows)})", "group", status, classes="group")
            )
            if not opened:
                continue
            for task in rows:
                label = Text(f"{task.symbol(self.ascii)} {task.identifier} {task.title}")
                if self.use_color:
                    label.stylize(ACTIVITIES[task.activity][3], 0, len(task.symbol(self.ascii)))
                row = Control(label, "task", task.id)
                row.tooltip = f"{task.identifier}: {task.title}; {ACTIVITIES[task.activity][2]}"
                await viewport.mount(row)
                if task.id not in self.view.expanded_tasks:
                    continue
                full = task.id in self.view.full_descriptions
                description = Description(task.body, full)
                more = Control(
                    "[show less]" if full else "[show more]", "more", task.id, classes="more"
                )
                details = Vertical(description, more, classes="details")
                await viewport.mount(details)
                if Text(task.title).cell_len > viewport.content_size.width:
                    await details.mount(
                        Static(Text(task.title), classes="activity"), before=description
                    )
                activity_label = ACTIVITIES[task.activity][2]
                if activity_label.casefold() != status.casefold():
                    await details.mount(
                        Static(
                            Text(f"{task.symbol(self.ascii)} {activity_label}"), classes="activity"
                        )
                    )
                for dependency in task.dependencies:
                    target = authorized.get(dependency)
                    if target:
                        await details.mount(
                            Control(
                                Text(f"Dependency: {target.identifier} {target.title}"),
                                "dependency",
                                target.id,
                                classes="dependency",
                            )
                        )
                    else:
                        await details.mount(
                            Static("Dependency unavailable in this session", markup=False)
                        )
        if focus_key:
            for control in self.query(Control):
                if (control.kind, control.key) == focus_key:
                    control.focus(scroll_visible=False)
                    break
        viewport.scroll_to(y=old_scroll, animate=False, immediate=True)

    async def on_control_selected(self, message: Control.Selected):
        if message.kind == "visibility":
            self.action_toggle_sidebar()
            return
        if message.kind == "group":
            collection = self.view.collapsed_groups
        elif message.kind == "task":
            collection = self.view.expanded_tasks
        elif message.kind == "more":
            collection = self.view.full_descriptions
        elif message.kind == "dependency":
            if message.key not in {task.id for task in self.snapshot.tasks}:
                return
            self.view.expanded_tasks.add(message.key)
            self.view.collapsed_groups = {
                group
                for group in self.view.collapsed_groups
                if group
                != next(task.status for task in self.snapshot.tasks if task.id == message.key)
            }
            await self.rebuild()
            for control in self.query(Control):
                if control.kind == "task" and control.key == message.key:
                    control.focus()
                    break
            return
        else:
            return
        if message.key in collection:
            collection.remove(message.key)
        else:
            collection.add(message.key)
        await self.rebuild()

    def action_toggle_sidebar(self):
        effective_hidden = self.view.hidden or self.size.width < 16
        self.view.hidden = not effective_hidden
        self.apply_visibility()
        if self.layout:
            self.layout.resize(self.view.hidden)

    def apply_visibility(self):
        hidden = self.view.hidden or self.size.width < 16
        self.query_one("#task-scroll").display = not hidden
        self.query_one("#pane-title").display = not hidden
        self.query_one("#visibility", Control).update(Text("[show]" if hidden else "[hide]"))

    def on_resize(self):
        if self.is_mounted:
            self.apply_visibility()

    def action_hide_sidebar(self):
        if not self.view.hidden:
            self.action_toggle_sidebar()

    def action_scroll_page_up(self):
        self.query_one("#task-scroll", VerticalScroll).scroll_page_up(animate=False)

    def action_scroll_page_down(self):
        self.query_one("#task-scroll", VerticalScroll).scroll_page_down(animate=False)

    def action_scroll_home(self):
        self.query_one("#task-scroll", VerticalScroll).scroll_home(animate=False)

    def action_scroll_end(self):
        self.query_one("#task-scroll", VerticalScroll).scroll_end(animate=False)
