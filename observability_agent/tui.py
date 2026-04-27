import json
import logging
import sqlite3
import threading
import time
from typing import cast

from textual.app import App, ComposeResult
from textual.containers import Horizontal
from textual.widgets import Footer, Header, Static

from observability_agent.db import get_db_path, reset_dashboard_state
from observability_agent.models import DashboardState, TimeRange
from observability_agent.panels import ALL_PANELS, PANELS, Panel
from observability_agent.synthetic import backfill, reset_scenario, stream


class ObservabilityTUI(App):
    TITLE = "Observability Dashboard"
    BINDINGS = [("q", "quit", "Quit"), ("r", "reset", "Reset view"), ("f", "toggle_freeze", "Freeze")]

    CSS = """
    Screen {
        layout: vertical;
    }

    #overview {
        height: 8;
        border: solid $accent;
    }

    #group-charts {
        height: 40%;
        layout: horizontal;
    }

    #timeseries {
        width: 1fr;
        border: solid $accent;
    }

    #histogram {
        width: 1fr;
        border: solid $accent;
    }

    #logs {
        height: 1fr;
        border: solid $accent;
        scrollbar-gutter: stable;
    }

    #agent-status {
        height: 1;
        background: $surface;
        color: $text-muted;
        padding: 0 1;
    }
    """

    def __init__(self, db_path: str, scenario: int | None = None) -> None:
        super().__init__()
        self._db_path = db_path
        self._scenario = scenario
        self._stop_event = threading.Event()
        self._panels: list[Panel] = [P(db_path) for P in PANELS]

    def compose(self) -> ComposeResult:
        yield Header()
        emitted_groups: set[str] = set()
        for panel in self._panels:
            if panel.group is None:
                yield panel.make_widget()
            elif panel.group not in emitted_groups:
                group_panels = [p for p in self._panels if p.group == panel.group]
                yield Horizontal(
                    *[p.make_widget() for p in group_panels],
                    id=f"group-{panel.group}",
                )
                emitted_groups.add(panel.group)
        yield Static("Agent: idle", id="agent-status")
        yield Footer()

    def on_mount(self) -> None:
        reset_dashboard_state(self._db_path)
        for panel in self._panels:
            panel.on_mount()
        self._start_data_thread()
        self.set_interval(0.5, self._poll)

    def action_reset(self) -> None:
        reset_dashboard_state(self._db_path)

    def action_toggle_freeze(self) -> None:
        try:
            with sqlite3.connect(self._db_path) as conn:
                row = conn.execute("SELECT frozen FROM dashboard_state WHERE id = 1").fetchone()
                current = bool(row[0]) if row else False
                conn.execute(
                    "UPDATE dashboard_state SET frozen = ? WHERE id = 1",
                    (0 if current else 1,),
                )
        except Exception:
            logging.warning("Failed to toggle freeze", exc_info=True)

    def _start_data_thread(self) -> None:
        db_path = self._db_path
        stop_event = self._stop_event
        scenario = self._scenario

        def _run() -> None:
            reset_scenario(scenario)
            backfill(db_path)
            stream(db_path, interval_sec=1.0, stop_event=stop_event)

        threading.Thread(target=_run, daemon=True).start()

    def _read_state(self) -> DashboardState | None:
        try:
            with sqlite3.connect(self._db_path) as conn:
                row = conn.execute(
                    "SELECT panels, timeseries_metric, timeseries_service,"
                    "       log_level, log_keyword, log_service,"
                    "       time_range_minutes,"
                    "       agent_status, agent_last_action, frozen, updated_at"
                    " FROM dashboard_state WHERE id = 1"
                ).fetchone()
        except Exception:
            logging.warning(
                "Failed to read dashboard_state from %s", self._db_path, exc_info=True
            )
            return None
        if not row:
            return None
        (
            panels_json,
            timeseries_metric,
            timeseries_service,
            log_level,
            log_keyword,
            log_service,
            time_range_minutes,
            agent_status,
            agent_last_action,
            frozen,
            updated_at,
        ) = row
        try:
            panels = json.loads(panels_json)
        except Exception:
            logging.warning(
                "Failed to parse panels JSON: %r", panels_json, exc_info=True
            )
            panels = list(ALL_PANELS)
        try:
            return DashboardState(
                panels=panels,
                timeseries_metric=timeseries_metric or "latency_p99",
                timeseries_service=timeseries_service or "all",
                log_level=log_level or "all",
                log_keyword=log_keyword or "",
                log_service=log_service or "all",
                time_range_minutes=cast(TimeRange, int(time_range_minutes) if time_range_minutes else 30),
                agent_status=agent_status or "idle",
                agent_last_action=agent_last_action or "",
                frozen=bool(frozen),
                updated_at=updated_at or 0.0,
            )
        except Exception:
            logging.warning(
                "Failed to construct DashboardState, using defaults", exc_info=True
            )
            return DashboardState()

    def _poll(self) -> None:
        state = self._read_state()
        if state is None:
            return
        visible = set(state.panels)

        # Individual panel visibility
        for panel in self._panels:
            self.query_one(panel.widget_id).display = panel.panel_id in visible

        # Group container visibility — show iff any member is visible
        seen_groups: set[str] = set()
        for panel in self._panels:
            if panel.group and panel.group not in seen_groups:
                group_panels = [p for p in self._panels if p.group == panel.group]
                self.query_one(f"#group-{panel.group}").display = any(
                    p.panel_id in visible for p in group_panels
                )
                seen_groups.add(panel.group)

        # Refresh visible panels (skipped when frozen)
        if not state.frozen:
            for panel in self._panels:
                if panel.panel_id in visible:
                    panel.poll(state)

        self._poll_agent_status(state)

    def _poll_agent_status(self, state: DashboardState) -> None:
        if state.updated_at:
            age = time.time() - state.updated_at
            age_str = f"{int(age)}s ago" if age < 60 else f"{int(age / 60)}m ago"
            time_part = f"  [{age_str}]"
        else:
            time_part = ""

        parts = ["[FROZEN]" if state.frozen else "", f"Agent: {state.agent_status}"]
        if state.agent_last_action:
            parts.append(f"— {state.agent_last_action}")
        parts.append(time_part)

        self.query_one("#agent-status", Static).update("  ".join(p for p in parts if p))

    def on_unmount(self) -> None:
        self._stop_event.set()


def run_tui(scenario: int | None = None) -> None:
    app = ObservabilityTUI(db_path=get_db_path(), scenario=scenario)
    app.run()
