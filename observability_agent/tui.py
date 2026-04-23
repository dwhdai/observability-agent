"""Textual TUI — 4-panel observability dashboard (T04/T05/T06/T07)."""

import json
import sqlite3
import threading
import time
from collections import defaultdict
from datetime import datetime
from typing import Any

from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Horizontal
from textual.widgets import DataTable, Footer, Header, RichLog, Static
from textual_plotext import PlotextPlot

from observability_agent.db import get_db_path, reset_dashboard_state
from observability_agent.synthetic import SERVICES, backfill, reset_scenario, stream

_LEVEL_STYLE: dict[str, str] = {
    "DEBUG": "dim",
    "INFO": "green",
    "WARN": "yellow",
    "ERROR": "bold red",
}

# Stable colour order so each service always gets the same plotext colour
_SERVICE_ORDER = list(SERVICES)

# Health thresholds: (warn, crit)
_THRESHOLDS: dict[str, tuple[float, float]] = {
    "latency_p99": (100.0, 300.0),  # ms
    "error_rate":  (0.01,  0.05),   # fraction
}

_ALL_PANELS = {"overview", "timeseries", "histogram", "logs"}


def _health_style(metric: str, value: float) -> str:
    thresholds = _THRESHOLDS.get(metric)
    if thresholds is None:
        return ""
    warn, crit = thresholds
    if value >= crit:
        return "bold red"
    if value >= warn:
        return "yellow"
    return "green"


class ObservabilityTUI(App):
    TITLE = "Observability Dashboard"
    BINDINGS = [("q", "quit", "Quit"), ("r", "reset", "Reset view")]

    CSS = """
    Screen {
        layout: vertical;
    }

    #overview {
        height: 8;
        border: solid $accent;
    }

    #charts-row {
        height: 40%;
        layout: horizontal;
    }

    #chart {
        width: 1fr;
        border: solid $accent;
    }

    #histogram {
        width: 1fr;
        border: solid $accent;
    }

    #log-viewer {
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
        self._last_log_id: int = 0
        self._stop_event = threading.Event()
        # Column keys set in _init_overview_table
        self._col_lat: object = None
        self._col_err: object = None
        self._col_rps: object = None
        # Cached log filter — detect changes to clear+refetch
        self._log_filter: tuple[str, str, str] = ("", "", "")

    def compose(self) -> ComposeResult:
        yield Header()
        yield DataTable(id="overview", show_cursor=False)
        yield Horizontal(
            PlotextPlot(id="chart"),
            PlotextPlot(id="histogram"),
            id="charts-row",
        )
        yield RichLog(highlight=False, markup=False, wrap=True, id="log-viewer")
        yield Static("Agent: idle", id="agent-status")
        yield Footer()

    def on_mount(self) -> None:
        reset_dashboard_state(self._db_path)
        self._init_overview_table()
        self._start_data_thread()
        self.set_interval(0.5, self._poll)

    def action_reset(self) -> None:
        reset_dashboard_state(self._db_path)

    def _init_overview_table(self) -> None:
        table = self.query_one("#overview", DataTable)
        _, self._col_lat, self._col_err, self._col_rps = table.add_columns(
            "Service", "P99 Latency (ms)", "Error Rate", "Req/s"
        )
        for svc in _SERVICE_ORDER:
            table.add_row(svc, "—", "—", "—", key=svc)

    def _start_data_thread(self) -> None:
        db_path = self._db_path
        stop_event = self._stop_event
        scenario = self._scenario

        def _run() -> None:
            reset_scenario(scenario)
            backfill(db_path)
            stream(db_path, interval_sec=1.0, stop_event=stop_event)

        threading.Thread(target=_run, daemon=True).start()

    # ── State ────────────────────────────────────────────────────────────────

    def _read_state(self) -> dict[str, Any] | None:
        try:
            with sqlite3.connect(self._db_path) as conn:
                row = conn.execute(
                    "SELECT panels, timeseries_metric, timeseries_service,"
                    "       histogram_metric, histogram_service,"
                    "       log_level, log_keyword, log_service,"
                    "       time_range_minutes,"
                    "       agent_status, agent_last_action, updated_at"
                    " FROM dashboard_state WHERE id = 1"
                ).fetchone()
        except Exception:
            return None
        if not row:
            return None
        (
            panels_json,
            ts_metric, ts_service,
            hist_metric, hist_service,
            log_level, log_keyword, log_service,
            time_range,
            agent_status, agent_last_action, updated_at,
        ) = row
        try:
            panels: set[str] = set(json.loads(panels_json))
        except Exception:
            panels = set(_ALL_PANELS)
        return {
            "panels": panels,
            "ts_metric": ts_metric,
            "ts_service": ts_service,
            "hist_metric": hist_metric,
            "hist_service": hist_service,
            "log_level": log_level or "all",
            "log_keyword": log_keyword or "",
            "log_service": log_service or "all",
            "minutes": int(time_range) if time_range else 30,
            "agent_status": agent_status or "idle",
            "agent_last_action": agent_last_action or "",
            "updated_at": updated_at or 0.0,
        }

    # ── Poll ────────────────────────────────────────────────────────────────

    def _poll(self) -> None:
        state = self._read_state()
        if state is None:
            return
        panels = state["panels"]
        self._apply_panel_visibility(panels)
        if "overview" in panels:
            self._poll_overview(state)
        if "timeseries" in panels:
            self._poll_chart(state)
        if "histogram" in panels:
            self._poll_histogram(state)
        if "logs" in panels:
            self._poll_logs(state)
        self._poll_agent_status(state)

    def _poll_agent_status(self, state: dict[str, Any]) -> None:
        status = state["agent_status"]
        last_action = state["agent_last_action"]
        updated_at = state["updated_at"]

        if updated_at:
            age = time.time() - updated_at
            if age < 60:
                age_str = f"{int(age)}s ago"
            else:
                age_str = f"{int(age / 60)}m ago"
            time_part = f"  [{age_str}]"
        else:
            time_part = ""

        parts = [f"Agent: {status}"]
        if last_action:
            parts.append(f"— {last_action}")
        parts.append(time_part)

        self.query_one("#agent-status", Static).update("  ".join(p for p in parts if p))

    # ── Panel visibility ─────────────────────────────────────────────────────

    def _apply_panel_visibility(self, panels: set[str]) -> None:
        self.query_one("#overview").display = "overview" in panels

        show_chart = "timeseries" in panels
        show_hist = "histogram" in panels
        self.query_one("#chart").display = show_chart
        self.query_one("#histogram").display = show_hist
        self.query_one("#charts-row").display = show_chart or show_hist

        self.query_one("#log-viewer").display = "logs" in panels

    # ── Overview ─────────────────────────────────────────────────────────────

    def _poll_overview(self, state: dict[str, Any]) -> None:
        cutoff = time.time() - state["minutes"] * 60
        try:
            with sqlite3.connect(self._db_path) as conn:
                rows = conn.execute(
                    "SELECT service, name, AVG(value) FROM metrics"
                    " WHERE name IN ('latency_p99', 'error_rate', 'req_per_sec')"
                    "   AND timestamp > ?"
                    " GROUP BY service, name",
                    (cutoff,),
                ).fetchall()
        except Exception:
            return

        data: dict[str, dict[str, float]] = defaultdict(dict)
        for svc, name, val in rows:
            data[svc][name] = val

        table = self.query_one("#overview", DataTable)
        for svc in _SERVICE_ORDER:
            if svc not in data:
                continue
            d = data[svc]
            lat = d.get("latency_p99", 0.0)
            err = d.get("error_rate", 0.0)
            rps = d.get("req_per_sec", 0.0)
            table.update_cell(svc, self._col_lat, Text(f"{lat:.1f}", style=_health_style("latency_p99", lat)))
            table.update_cell(svc, self._col_err, Text(f"{err*100:.2f}%", style=_health_style("error_rate", err)))
            table.update_cell(svc, self._col_rps, Text(f"{rps:.1f}"))

    # ── Time-series chart ────────────────────────────────────────────────────

    def _poll_chart(self, state: dict[str, Any]) -> None:
        metric = state["ts_metric"]
        svc_filter = state["ts_service"]
        minutes = state["minutes"]
        cutoff = time.time() - minutes * 60
        bucket = max(5, minutes * 60 // 100)

        try:
            with sqlite3.connect(self._db_path) as conn:
                if svc_filter == "all":
                    rows = conn.execute(
                        "SELECT ROUND(timestamp / ?) * ? AS ts, AVG(value), service"
                        " FROM metrics"
                        " WHERE name = ? AND timestamp > ?"
                        " GROUP BY ts, service ORDER BY ts ASC",
                        (bucket, bucket, metric, cutoff),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT ROUND(timestamp / ?) * ? AS ts, AVG(value), service"
                        " FROM metrics"
                        " WHERE name = ? AND service = ? AND timestamp > ?"
                        " GROUP BY ts, service ORDER BY ts ASC",
                        (bucket, bucket, metric, svc_filter, cutoff),
                    ).fetchall()
        except Exception:
            return

        if not rows:
            return

        series: dict[str, tuple[list[float], list[float]]] = defaultdict(lambda: ([], []))
        for ts, val, svc in rows:
            xs, ys = series[svc]
            xs.append((ts - cutoff) / 60.0)
            ys.append(val)

        chart = self.query_one("#chart", PlotextPlot)
        plt = chart.plt
        plt.clear_figure()
        plt.title(f"{metric}  [{svc_filter}]  last {minutes}m")
        plt.xlabel("minutes")

        for svc in _SERVICE_ORDER:
            if svc not in series:
                continue
            xs, ys = series[svc]
            plt.plot(xs, ys, label=svc)

        chart.refresh()

    # ── Histogram ────────────────────────────────────────────────────────────

    def _poll_histogram(self, state: dict[str, Any]) -> None:
        metric = state["hist_metric"]
        svc_filter = state["hist_service"]
        cutoff = time.time() - state["minutes"] * 60

        try:
            with sqlite3.connect(self._db_path) as conn:
                if svc_filter == "all":
                    rows = conn.execute(
                        "SELECT value FROM metrics WHERE name = ? AND timestamp > ?",
                        (metric, cutoff),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT value FROM metrics"
                        " WHERE name = ? AND service = ? AND timestamp > ?",
                        (metric, svc_filter, cutoff),
                    ).fetchall()
        except Exception:
            return

        if not rows:
            return

        values = [r[0] for r in rows]

        hist = self.query_one("#histogram", PlotextPlot)
        plt = hist.plt
        plt.clear_figure()
        plt.title(f"{metric} distribution  [{svc_filter}]")
        plt.hist(values, bins=10)
        plt.xlabel(metric)
        hist.refresh()

    # ── Logs ────────────────────────────────────────────────────────────────

    def _poll_logs(self, state: dict[str, Any]) -> None:
        log_level = state["log_level"]
        log_keyword = state["log_keyword"]
        log_service = state["log_service"]
        new_filter = (log_level, log_keyword, log_service)

        log_widget = self.query_one("#log-viewer", RichLog)

        if new_filter != self._log_filter:
            log_widget.clear()
            self._last_log_id = 0
            self._log_filter = new_filter

        conditions = ["id > ?"]
        params: list[Any] = [self._last_log_id]

        if log_level != "all":
            conditions.append("level = ?")
            params.append(log_level)
        if log_service != "all":
            conditions.append("service = ?")
            params.append(log_service)
        if log_keyword:
            conditions.append("message LIKE ?")
            params.append(f"%{log_keyword}%")

        where = " AND ".join(conditions)

        try:
            with sqlite3.connect(self._db_path) as conn:
                rows = conn.execute(
                    f"SELECT id, timestamp, level, service, message FROM logs"
                    f" WHERE {where} ORDER BY id ASC LIMIT 200",
                    params,
                ).fetchall()
        except Exception:
            return

        if not rows:
            return

        for row_id, ts, level, service, message in rows:
            style = _LEVEL_STYLE.get(level, "")
            dt = datetime.fromtimestamp(ts).strftime("%H:%M:%S")
            line = Text()
            line.append(f"{dt} │ {level:<5} │ {service:<20} │ {message}", style=style)
            log_widget.write(line)
            self._last_log_id = row_id

    def on_unmount(self) -> None:
        self._stop_event.set()


def run_tui(scenario: int | None = None) -> None:
    app = ObservabilityTUI(db_path=get_db_path(), scenario=scenario)
    app.run()
