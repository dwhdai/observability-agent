"""Dashboard panel definitions.

## Adding a new panel

1. Subclass ``Panel``.
2. Set ``panel_id`` (matches the name used in ``dashboard_state.panels``).
3. Set ``group`` if the panel should share a horizontal row with others.
4. Implement ``make_widget()``, ``poll()``, and optionally ``on_mount()``.
5. Append the class to ``PANELS`` at the bottom of this file.

No changes to ``tui.py`` are required.
"""

import logging
import sqlite3
import time
from abc import ABC, abstractmethod
from collections import defaultdict
from datetime import datetime
from typing import Any

from rich.text import Text
from textual.widget import Widget
from textual.widgets import DataTable, RichLog
from textual_plotext import PlotextPlot

from observability_agent.models import DashboardState
from observability_agent.synthetic import SERVICES

# ── Constants ─────────────────────────────────────────────────────────────────

_LEVEL_STYLE: dict[str, str] = {
    "DEBUG": "dim",
    "INFO": "green",
    "WARN": "yellow",
    "ERROR": "bold red",
}

# Stable order so each service always gets the same plotext colour.
_SERVICE_ORDER = list(SERVICES)

# Health thresholds: (warn, crit)
_THRESHOLDS: dict[str, tuple[float, float]] = {
    "latency_p99": (100.0, 300.0),  # ms
    "error_rate":  (0.01,  0.05),   # fraction
}

ALL_PANELS: frozenset[str] = frozenset({"overview", "timeseries", "histogram", "logs"})


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


# ── Panel base class ──────────────────────────────────────────────────────────


class Panel(ABC):
    """Base class for all dashboard panels.

    Each panel owns its Textual widget. Call ``make_widget()`` once during
    ``compose()``; the panel stores the reference and uses it directly in
    ``on_mount()`` and ``poll()`` — no app reference required.

    Attributes:
        group: Panels sharing the same non-None group string are placed inside
            a ``Horizontal`` container. The container gets CSS id
            ``#group-<group>``. ``None`` means full-width, vertically stacked.
    """

    group: str | None = None

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path

    @property
    @abstractmethod
    def panel_id(self) -> str:
        """Name used in the ``dashboard_state.panels`` array, e.g. ``'timeseries'``."""

    @property
    def widget_id(self) -> str:
        """CSS selector for this panel's root widget."""
        return f"#{self.panel_id}"

    @abstractmethod
    def make_widget(self) -> Widget:
        """Create and return this panel's Textual widget.

        Implementations must also store a reference so ``on_mount()`` and
        ``poll()`` can use it without querying the app.
        """

    def on_mount(self) -> None:
        """Post-mount initialisation hook (optional). Called after widget tree exists."""

    @abstractmethod
    def poll(self, state: DashboardState) -> None:
        """Refresh panel content from DB. Called every 0.5 s when the panel is visible."""

    def _query_db(self, sql: str, params: tuple = ()) -> list[tuple] | None:
        """Execute a read-only query. Returns rows or ``None`` on error."""
        try:
            with sqlite3.connect(self._db_path) as conn:
                return conn.execute(sql, params).fetchall()
        except Exception:
            logging.warning("DB query failed: %s | params=%s", sql, params, exc_info=True)
            return None


# ── Concrete panels ───────────────────────────────────────────────────────────


class OverviewPanel(Panel):
    """Service health table: p99 latency, error rate, req/s per service."""

    def __init__(self, db_path: str) -> None:
        super().__init__(db_path)
        self._table: DataTable | None = None
        self._col_lat: object = None
        self._col_err: object = None
        self._col_rps: object = None

    @property
    def panel_id(self) -> str:
        return "overview"

    def make_widget(self) -> Widget:
        self._table = DataTable(id=self.panel_id, show_cursor=False)
        return self._table

    def on_mount(self) -> None:
        assert self._table is not None
        _, self._col_lat, self._col_err, self._col_rps = self._table.add_columns(
            "Service", "P99 Latency (ms)", "Error Rate", "Req/s"
        )
        for svc in _SERVICE_ORDER:
            self._table.add_row(svc, "—", "—", "—", key=svc)

    def poll(self, state: DashboardState) -> None:
        assert self._table is not None
        cutoff = time.time() - state.time_range_minutes * 60
        rows = self._query_db(
            "SELECT service, name, AVG(value) FROM metrics"
            " WHERE name IN ('latency_p99', 'error_rate', 'req_per_sec')"
            "   AND timestamp > ?"
            " GROUP BY service, name",
            (cutoff,),
        )
        if rows is None:
            return

        data: dict[str, dict[str, float]] = defaultdict(dict)
        for svc, name, val in rows:
            data[svc][name] = val

        for svc in _SERVICE_ORDER:
            if svc not in data:
                continue
            d = data[svc]
            lat = d.get("latency_p99", 0.0)
            err = d.get("error_rate", 0.0)
            rps = d.get("req_per_sec", 0.0)
            self._table.update_cell(svc, self._col_lat, Text(f"{lat:.1f}", style=_health_style("latency_p99", lat)))
            self._table.update_cell(svc, self._col_err, Text(f"{err*100:.2f}%", style=_health_style("error_rate", err)))
            self._table.update_cell(svc, self._col_rps, Text(f"{rps:.1f}"))


class TimeSeriesPanel(Panel):
    """Time-series chart for the currently selected metric."""

    group = "charts"

    def __init__(self, db_path: str) -> None:
        super().__init__(db_path)
        self._plot: PlotextPlot | None = None

    @property
    def panel_id(self) -> str:
        return "timeseries"

    def make_widget(self) -> Widget:
        self._plot = PlotextPlot(id=self.panel_id)
        return self._plot

    def poll(self, state: DashboardState) -> None:
        assert self._plot is not None
        metric = state.timeseries_metric
        svc_filter = state.timeseries_service
        minutes = state.time_range_minutes
        cutoff = time.time() - minutes * 60
        bucket = max(5, minutes * 60 // 100)

        if svc_filter == "all":
            rows = self._query_db(
                "SELECT ROUND(timestamp / ?) * ? AS ts, AVG(value), service"
                " FROM metrics"
                " WHERE name = ? AND timestamp > ?"
                " GROUP BY ts, service ORDER BY ts ASC",
                (bucket, bucket, metric, cutoff),
            )
        else:
            rows = self._query_db(
                "SELECT ROUND(timestamp / ?) * ? AS ts, AVG(value), service"
                " FROM metrics"
                " WHERE name = ? AND service = ? AND timestamp > ?"
                " GROUP BY ts, service ORDER BY ts ASC",
                (bucket, bucket, metric, svc_filter, cutoff),
            )

        plt = self._plot.plt
        plt.clear_figure()

        if rows is None:
            plt.title(f"{metric}  [{svc_filter}]  last {minutes}m  [query error]")
            self._plot.refresh()
            return

        plt.title(f"{metric}  [{svc_filter}]  last {minutes}m")
        plt.xlabel("minutes")

        if not rows:
            self._plot.refresh()
            return

        series: dict[str, tuple[list[float], list[float]]] = defaultdict(lambda: ([], []))
        for ts, val, svc in rows:
            xs, ys = series[svc]
            xs.append((ts - cutoff) / 60.0)
            ys.append(val)

        for svc in _SERVICE_ORDER:
            if svc not in series:
                continue
            xs, ys = series[svc]
            plt.plot(xs, ys, label=svc)

        self._plot.refresh()


class HistogramPanel(Panel):
    """Metric value distribution histogram."""

    group = "charts"

    def __init__(self, db_path: str) -> None:
        super().__init__(db_path)
        self._plot: PlotextPlot | None = None

    @property
    def panel_id(self) -> str:
        return "histogram"

    def make_widget(self) -> Widget:
        self._plot = PlotextPlot(id=self.panel_id)
        return self._plot

    def poll(self, state: DashboardState) -> None:
        assert self._plot is not None
        metric = state.timeseries_metric
        svc_filter = state.timeseries_service
        cutoff = time.time() - state.time_range_minutes * 60

        if svc_filter == "all":
            rows = self._query_db(
                "SELECT value FROM metrics WHERE name = ? AND timestamp > ?",
                (metric, cutoff),
            )
        else:
            rows = self._query_db(
                "SELECT value FROM metrics WHERE name = ? AND service = ? AND timestamp > ?",
                (metric, svc_filter, cutoff),
            )

        plt = self._plot.plt
        plt.clear_figure()

        if rows is None:
            plt.title(f"{metric} distribution  [{svc_filter}]  [query error]")
            self._plot.refresh()
            return

        plt.title(f"{metric} distribution  [{svc_filter}]")

        if not rows:
            self._plot.refresh()
            return

        plt.hist([r[0] for r in rows], bins=10)
        plt.xlabel(metric)
        self._plot.refresh()


class LogPanel(Panel):
    """Scrolling log viewer with level / service / keyword filters."""

    def __init__(self, db_path: str) -> None:
        super().__init__(db_path)
        self._log_widget: RichLog | None = None
        self._last_log_id: int = 0
        self._log_filter: tuple[str, str, str] = ("", "", "")

    @property
    def panel_id(self) -> str:
        return "logs"

    def make_widget(self) -> Widget:
        self._log_widget = RichLog(highlight=False, markup=False, wrap=True, id=self.panel_id)
        return self._log_widget

    def poll(self, state: DashboardState) -> None:
        assert self._log_widget is not None
        log_level = state.log_level
        log_keyword = state.log_keyword
        log_service = state.log_service
        new_filter = (log_level, log_keyword, log_service)

        if new_filter != self._log_filter:
            self._log_widget.clear()
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
        rows = self._query_db(
            "SELECT id, timestamp, level, service, message FROM logs"
            " WHERE " + where + " ORDER BY id ASC LIMIT 200",
            tuple(params),
        )
        if not rows:
            return

        for row_id, ts, level, service, message in rows:
            style = _LEVEL_STYLE.get(level, "")
            dt = datetime.fromtimestamp(ts).strftime("%H:%M:%S")
            line = Text()
            line.append(f"{dt} │ {level:<5} │ {service:<20} │ {message}", style=style)
            self._log_widget.write(line)
            self._last_log_id = row_id


# ── Panel registry ────────────────────────────────────────────────────────────
# To add a new panel: implement Panel above, then append the class here.

PANELS: list[type[Panel]] = [
    OverviewPanel,
    TimeSeriesPanel,
    HistogramPanel,
    LogPanel,
]
