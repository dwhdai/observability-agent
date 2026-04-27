"""Shared Pydantic models for dashboard state and agent I/O."""

from typing import Literal

from pydantic import BaseModel

MetricName = Literal["latency_p99", "error_rate", "req_per_sec", "cpu_usage"]
ServiceFilter = Literal[
    "all", "api-gateway", "payment-service", "user-service", "inventory-service"
]
LogLevel = Literal["all", "DEBUG", "INFO", "WARN", "ERROR"]
PanelName = Literal["overview", "timeseries", "histogram", "logs"]
AgentStatus = Literal["idle", "thinking", "querying", "done", "error"]
TimeRange = Literal[5, 10, 15, 30]


class DashboardState(BaseModel):
    """Typed dashboard_state row. Source of truth for defaults."""

    panels: list[PanelName] = ["overview", "timeseries", "histogram", "logs"]
    timeseries_metric: MetricName = "latency_p99"
    timeseries_service: ServiceFilter = "all"
    log_level: LogLevel = "all"
    log_keyword: str = ""
    log_service: ServiceFilter = "all"
    time_range_minutes: TimeRange = 30
    agent_status: AgentStatus = "idle"
    agent_last_action: str = ""
    updated_at: float = 0.0


class AgentResponse(BaseModel):
    accepted: bool
    rejection_reason: str | None
    response: str
