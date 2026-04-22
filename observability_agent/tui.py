"""Textual TUI — time series chart + log viewer (T04/T05)."""
from __future__ import annotations

import sqlite3
import threading
import time
from collections import defaultdict
from datetime import datetime

from rich.text import Text
from textual.app import App, ComposeResult
from textual.widgets import Footer, Header, RadioButton, RadioSet, RichLog
from textual_plotext import PlotextPlot

from observability_agent.db import get_db_path
from observability_agent.synthetic import SERVICES, backfill, reset_scenario, stream

_LEVEL_STYLE: dict[str, str] = {
    "DEBUG": "dim",
    "INFO": "green",
    "WARN": "yellow",
    "ERROR": "bold red",
}

_WINDOW_OPTIONS: list[int] = [5, 10, 15, 30]  # minutes

# Stable colour order so each service always gets the same plotext colour
_SERVICE_ORDER = list(SERVICES)


class ObservabilityTUI(App):
    TITLE = "Observability Dashboard"
    BINDINGS = [("q", "quit", "Quit")]

    CSS = """
    Screen {
        layout: vertical;
    }

    #chart {
        height: 55%;
        border: solid $accent;
    }

    #time-selector {
        height: auto;
        padding: 0 1;
        background: $surface;
    }

    #log-viewer {
        height: 1fr;
        border: solid $accent;
        scrollbar-gutter: stable;
    }
    """

    def __init__(self, db_path: str) -> None:
        super().__init__()
        self._db_path = db_path
        self._last_log_id: int = 0
        self._chart_minutes: int = _WINDOW_OPTIONS[0]
        self._stop_event = threading.Event()

    def compose(self) -> ComposeResult:
        yield Header()
        yield PlotextPlot(id="chart")
        yield RadioSet(
            *[RadioButton(f"{m}m", value=(m == _WINDOW_OPTIONS[0])) for m in _WINDOW_OPTIONS],
            id="time-selector",
        )
        yield RichLog(highlight=False, markup=False, wrap=True, id="log-viewer")
        yield Footer()

    def on_mount(self) -> None:
        self._start_data_thread()
        self.set_interval(0.5, self._poll)

    def on_radio_set_changed(self, event: RadioSet.Changed) -> None:
        self._chart_minutes = _WINDOW_OPTIONS[event.index]

    def _start_data_thread(self) -> None:
        db_path = self._db_path
        stop_event = self._stop_event

        def _run() -> None:
            reset_scenario()
            backfill(db_path)
            stream(db_path, interval_sec=1.0, stop_event=stop_event)

        threading.Thread(target=_run, daemon=True).start()

    # ── Poll ────────────────────────────────────────────────────────────────

    def _poll(self) -> None:
        self._poll_chart()
        self._poll_logs()

    # ── Chart ────────────────────────────────────────────────────────────────

    def _poll_chart(self) -> None:
        try:
            conn = sqlite3.connect(self._db_path)
            state = conn.execute(
                "SELECT timeseries_metric, timeseries_service"
                " FROM dashboard_state WHERE id = 1"
            ).fetchone()
            if not state:
                conn.close()
                return
            metric, svc_filter = state
            cutoff = time.time() - self._chart_minutes * 60

            # Downsample to ~100 points across the window to avoid block-fill rendering
            bucket = max(5, self._chart_minutes * 60 // 100)

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
            conn.close()
        except Exception:
            return

        if not rows:
            return

        # Group into per-service series; x = elapsed minutes from window start
        series: dict[str, tuple[list[float], list[float]]] = defaultdict(lambda: ([], []))
        for ts, val, svc in rows:
            xs, ys = series[svc]
            xs.append((ts - cutoff) / 60.0)
            ys.append(val)

        chart = self.query_one("#chart", PlotextPlot)
        plt = chart.plt
        plt.clear_figure()
        plt.title(f"{metric}  [{svc_filter}]  last {self._chart_minutes}m")
        plt.xlabel("minutes")

        for svc in _SERVICE_ORDER:
            if svc not in series:
                continue
            xs, ys = series[svc]
            plt.plot(xs, ys, label=svc)

        chart.refresh()

    # ── Logs ────────────────────────────────────────────────────────────────

    def _poll_logs(self) -> None:
        try:
            conn = sqlite3.connect(self._db_path)
            rows = conn.execute(
                "SELECT id, timestamp, level, service, message FROM logs"
                " WHERE id > ? ORDER BY id ASC LIMIT 200",
                (self._last_log_id,),
            ).fetchall()
            conn.close()
        except Exception:
            return

        if not rows:
            return

        log_widget = self.query_one("#log-viewer", RichLog)
        for row_id, ts, level, service, message in rows:
            style = _LEVEL_STYLE.get(level, "")
            dt = datetime.fromtimestamp(ts).strftime("%H:%M:%S")
            line = Text()
            line.append(f"{dt} │ {level:<5} │ {service:<20} │ {message}", style=style)
            log_widget.write(line)
            self._last_log_id = row_id

    def on_unmount(self) -> None:
        self._stop_event.set()


def run_tui() -> None:
    app = ObservabilityTUI(db_path=get_db_path())
    app.run()
