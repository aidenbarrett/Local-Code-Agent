"""Fixture-driven Textual shell for the Session Hub.

This module is presentation only.  It consumes deterministic read models and
conversation prose supplied by its caller.  It does not call a model, admit a
task, execute a tool, decide a verdict, or mutate repository state.

Live controller/service wiring is deliberately a later integration slice.  The
first shell establishes the mandatory panes, responsive layout and one-way input
boundary against stable product-owned view state.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from textual import events
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.widgets import Input, Static

from .task_read_model import TaskSnapshot, project_tasks
from .watch_read_model import WatchSnapshot, project_watches


_THEME_FILE = Path(__file__).resolve().parents[2] / "ui" / "themes" / "lca.json"
_REQUIRED_ROLES = frozenset(
    {
        "background",
        "surface",
        "foreground",
        "accent",
        "secondary",
        "warn",
        "fail",
        "verified",
        "muted",
        "focus",
    }
)
_LAYOUTS = ("wide", "medium", "compact")
_RECENT_TASK_LIMIT = 3


class PaletteError(RuntimeError):
    """The checked-in UI palette is missing or malformed."""


@dataclass(frozen=True)
class ConversationEntry:
    role: str
    text: str

    def __post_init__(self) -> None:
        if self.role not in {"user", "assistant", "system"}:
            raise ValueError("conversation role must be user, assistant or system")
        if not isinstance(self.text, str):
            raise TypeError("conversation text must be a string")


@dataclass(frozen=True)
class HubViewState:
    conversation: tuple[ConversationEntry, ...] = ()
    tasks: tuple[TaskSnapshot, ...] = ()
    watches: tuple[WatchSnapshot, ...] = ()
    route_summary: str | None = None
    status: str = "Ready"

    def __post_init__(self) -> None:
        object.__setattr__(self, "conversation", tuple(self.conversation))
        object.__setattr__(self, "tasks", tuple(self.tasks))
        object.__setattr__(self, "watches", tuple(self.watches))
        if self.route_summary is not None and not self.route_summary.strip():
            raise ValueError("route_summary must be nonempty when supplied")
        if not isinstance(self.status, str) or not self.status.strip():
            raise ValueError("status must be nonempty")


class HubInputSubmitted(Message):
    """UI-only input message; receiving it grants no execution authority."""

    def __init__(self, text: str) -> None:
        super().__init__()
        self.text = text


def load_palette(name: str = "neon", *, path: Path = _THEME_FILE) -> dict[str, str]:
    """Load one named palette and fail closed on malformed checked-in state."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PaletteError(f"cannot load Session Hub palette: {exc}") from exc
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise PaletteError("Session Hub palette must be schema_version 1")
    palette = raw.get(name)
    if not isinstance(palette, dict):
        raise PaletteError(f"unknown Session Hub palette {name!r}")
    keys = set(palette)
    if keys != _REQUIRED_ROLES:
        missing = sorted(_REQUIRED_ROLES - keys)
        extra = sorted(keys - _REQUIRED_ROLES)
        raise PaletteError(f"palette roles mismatch; missing={missing}, extra={extra}")
    values: dict[str, str] = {}
    for role, value in palette.items():
        if (
            not isinstance(value, str)
            or len(value) != 7
            or not value.startswith("#")
            or any(ch not in "0123456789abcdefABCDEF" for ch in value[1:])
        ):
            raise PaletteError(f"palette role {role!r} must be #RRGGBB")
        values[role] = value.upper()
    return values


def layout_mode(width: int) -> str:
    if not isinstance(width, int) or isinstance(width, bool) or width <= 0:
        raise ValueError("terminal width must be a positive integer")
    if width >= 120:
        return "wide"
    if width >= 80:
        return "medium"
    return "compact"


def build_view_state(
    conversation: Sequence[ConversationEntry],
    events_: Iterable[dict],
    *,
    route_summary: str | None = None,
    status: str = "Ready",
) -> HubViewState:
    """Project durable events once, before presentation touches them."""
    materialised = list(events_)
    return HubViewState(
        conversation=tuple(conversation),
        tasks=tuple(project_tasks(materialised)),
        watches=tuple(project_watches(materialised)),
        route_summary=route_summary,
        status=status,
    )


def render_conversation(entries: Sequence[ConversationEntry]) -> str:
    if not entries:
        return "No conversation yet."
    labels = {"user": "YOU", "assistant": "LCA", "system": "SYSTEM"}
    blocks: list[str] = []
    for entry in entries:
        blocks.append(f"{labels[entry.role]}\n{entry.text}")
    return "\n\n".join(blocks)


def _active_task(tasks: Sequence[TaskSnapshot]) -> TaskSnapshot | None:
    for task in reversed(tuple(tasks)):
        if not task.terminal:
            return task
    return None if not tasks else tuple(tasks)[-1]


def _recent_terminal_tasks(
    tasks: Sequence[TaskSnapshot],
    current: TaskSnapshot,
    *,
    limit: int = _RECENT_TASK_LIMIT,
) -> tuple[TaskSnapshot, ...]:
    """Return bounded prior terminal tasks with their canonical durable IDs intact."""
    prior = [
        task
        for task in reversed(tuple(tasks))
        if task.task_id != current.task_id and task.terminal
    ]
    return tuple(prior[:limit])


def render_activity(state: HubViewState) -> str:
    task = _active_task(state.tasks)
    lines: list[str] = []
    if state.route_summary:
        lines.append(f"Route: {state.route_summary}")
    if task is None:
        lines.extend(("Task: none", "Skill: none", "Tool: none", "Verdict: none"))
        return "\n".join(lines)

    lines.append(f"Task: {task.task_id}")
    lines.append(f"Route: {task.origin_kind}" if not state.route_summary else f"Origin: {task.origin_kind}")
    lines.append(f"Skill: {task.skill or 'none'}")
    lines.append(f"State: {task.state}")

    if task.endpoint is not None:
        endpoint = task.endpoint
        queue = "" if endpoint.queue_position is None else f" · queue {endpoint.queue_position}"
        lines.append(f"Endpoint: {endpoint.role} {endpoint.state}{queue}")

    open_tools = task.frozen_open_tools()
    if open_tools:
        names = ", ".join(tool.tool_name for tool in open_tools)
        lines.append(f"Tool: running {names}")
    elif task.last_tool is not None:
        suffix = task.last_tool.execution or "finished"
        lines.append(f"Tool: {task.last_tool.tool_name} · {suffix}")
    else:
        lines.append("Tool: none")

    if task.cancel_requested:
        lines.append("Cancellation: requested")

    if task.verdict is None:
        lines.append("Verdict: pending")
    else:
        reason = "" if not task.verdict_reason else f" · {task.verdict_reason}"
        lines.append(f"Verdict: {task.verdict}{reason}")
        # These lines are deterministic controller output.  Do not rewrite them.
        lines.extend(task.verdict_lines)

    if task.faults:
        reason, message = task.faults[-1]
        lines.append(f"Fault: {reason} · {message}")

    recent = _recent_terminal_tasks(state.tasks, task)
    if recent:
        lines.extend(("", "Recent tasks (copy full ID for follow-up):"))
        for prior in recent:
            verdict = prior.verdict or "NO_VERDICT"
            lines.append(f"{prior.task_id} · {prior.state} · {verdict}")
    return "\n".join(lines)


def _watch_row(watch: WatchSnapshot) -> str:
    run = watch.last_run
    if run is None:
        result = "no runs"
    else:
        result = f"{run.status}/{run.verdict} · {run.comparison}"
    next_due = watch.next_due_utc or "not scheduled"
    return f"{watch.job_id[:8]}  {watch.state:<9}  {result}  · next {next_due}"


def render_watches(watches: Sequence[WatchSnapshot]) -> str:
    if not watches:
        return "No watches configured"
    return "\n".join(_watch_row(watch) for watch in watches)


def _css(palette: Mapping[str, str]) -> str:
    return f"""
    Screen {{
        background: {palette['background']};
        color: {palette['foreground']};
    }}
    #title {{
        height: 3;
        padding: 1 2;
        background: {palette['surface']};
        color: {palette['secondary']};
        text-style: bold;
    }}
    #layout-warning {{
        display: none;
        height: 3;
        padding: 1 2;
        color: {palette['warn']};
        background: {palette['surface']};
        text-style: bold;
    }}
    #workspace {{
        height: 1fr;
    }}
    #workspace.wide {{
        layout: horizontal;
    }}
    #workspace.medium, #workspace.compact {{
        layout: vertical;
    }}
    #conversation-column {{
        width: 55%;
        height: 1fr;
        border: round {palette['accent']};
    }}
    #side-column {{
        width: 45%;
        height: 1fr;
    }}
    #workspace.medium #conversation-column,
    #workspace.medium #side-column,
    #workspace.compact #conversation-column,
    #workspace.compact #side-column {{
        width: 100%;
    }}
    #workspace.medium #conversation-column {{
        height: 3fr;
    }}
    #workspace.medium #side-column {{
        height: 2fr;
    }}
    #workspace.compact #conversation-column {{
        height: 3;
        border: none;
    }}
    #workspace.compact #conversation {{
        display: none;
    }}
    #workspace.compact #side-column {{
        height: 1fr;
    }}
    #conversation {{
        height: 1fr;
        padding: 1 2;
        overflow-y: auto;
    }}
    #composer {{
        dock: bottom;
        height: 3;
        border: tall {palette['focus']};
        background: {palette['surface']};
        color: {palette['foreground']};
    }}
    #activity {{
        height: 1fr;
        min-height: 8;
        padding: 1 2;
        border: round {palette['secondary']};
        overflow-y: auto;
    }}
    #watch {{
        height: 1fr;
        min-height: 8;
        padding: 1 2;
        border: round {palette['accent']};
        overflow-y: auto;
    }}
    #status {{
        height: 1;
        padding: 0 1;
        background: {palette['surface']};
        color: {palette['muted']};
    }}
    """


class SessionHubApp(App):
    """Textual presentation shell with no controller authority."""

    TITLE = "Local Code Agent · Session Hub"
    CSS = _css(load_palette("neon"))

    def __init__(self, state: HubViewState | None = None, *, palette_name: str = "neon") -> None:
        self.view_state = state or HubViewState()
        palette = load_palette(palette_name)
        # Textual reads CSS from the instance as well as the class; keep one source
        # of named colour roles while allowing the high-contrast fixture path.
        self.CSS = _css(palette)
        super().__init__()

    def compose(self) -> ComposeResult:
        yield Static("LOCAL CODE AGENT · SESSION HUB", id="title", markup=False)
        yield Static(
            "Compact layout: conversation body hidden; activity, watch summary and composer remain visible.",
            id="layout-warning",
            markup=False,
        )
        with Horizontal(id="workspace"):
            with Vertical(id="conversation-column"):
                yield Static(render_conversation(self.view_state.conversation), id="conversation", markup=False)
                yield Input(placeholder="Message Local Code Agent…", id="composer")
            with Vertical(id="side-column"):
                yield Static(render_activity(self.view_state), id="activity", markup=False)
                yield Static(render_watches(self.view_state.watches), id="watch", markup=False)
        yield Static(self.view_state.status, id="status", markup=False)

    def on_mount(self) -> None:
        self._apply_layout(self.size.width)

    def on_resize(self, event: events.Resize) -> None:
        self._apply_layout(event.size.width)

    def _apply_layout(self, width: int) -> None:
        mode = layout_mode(width)
        workspace = self.query_one("#workspace")
        workspace.set_classes(mode)
        warning = self.query_one("#layout-warning")
        warning.display = mode == "compact"

    def replace_state(self, state: HubViewState) -> None:
        """Replace already-derived presentation state on the Textual thread."""
        if not isinstance(state, HubViewState):
            raise TypeError("Session Hub state must be HubViewState")
        self.view_state = state
        self.query_one("#conversation", Static).update(render_conversation(state.conversation))
        self.query_one("#activity", Static).update(render_activity(state))
        self.query_one("#watch", Static).update(render_watches(state.watches))
        self.query_one("#status", Static).update(state.status)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value
        event.input.value = ""
        if text.strip():
            self.post_message(HubInputSubmitted(text))


__all__ = [
    "ConversationEntry",
    "HubInputSubmitted",
    "HubViewState",
    "PaletteError",
    "SessionHubApp",
    "build_view_state",
    "layout_mode",
    "load_palette",
    "render_activity",
    "render_conversation",
    "render_watches",
]
