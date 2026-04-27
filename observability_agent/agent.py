"""SRE copilot agent — Pydantic AI chat loop (T08+)."""

import json
import logging
import os
import sqlite3
import threading
import time
from dataclasses import dataclass

from pydantic_ai import Agent, RunContext, UsageLimits
from pydantic_ai.exceptions import UsageLimitExceeded

from observability_agent.db import get_db_path
from observability_agent.models import (
    AgentResponse,
    AgentStatus,
    LogLevel,
    MetricName,
    PanelName,
    ServiceFilter,
    TimeRange,
)

# ── Deps ─────────────────────────────────────────────────────────────────────


@dataclass
class Deps:
    db_path: str


# ── Dynamic data discovery ────────────────────────────────────────────────────

_ELABORATE_TRIGGERS = {
    "elaborate", "explain", "tell me more", "detail",
    "describe", "walk me through", "breakdown", "deep dive",
}


def _get_available_data(db_path: str) -> tuple[list[str], list[str]]:
    """Query DB for distinct services and metrics."""
    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
            services = [r[0] for r in conn.execute("SELECT DISTINCT service FROM metrics ORDER BY service").fetchall()]
            metrics = [r[0] for r in conn.execute("SELECT DISTINCT name FROM metrics ORDER BY name").fetchall()]
        return services or [], metrics or []
    except Exception:
        return [], []


# ── System prompt ─────────────────────────────────────────────────────────────


def _build_system_prompt(services: list[str], metrics: list[str]) -> str:
    services_str = ", ".join(services) if services else "(none yet — DB may be empty)"
    metrics_str = ", ".join(metrics) if metrics else "(none yet — DB may be empty)"
    elaborate_str = ", ".join(sorted(_ELABORATE_TRIGGERS))

    return f"""
You are an SRE copilot for a microservices observability platform.

## Scope — STRICT

You ONLY answer questions that can be directly answered using data from the observability
database or by controlling the dashboard. You do NOT answer:
- General SRE/observability educational questions (e.g. "what is P99 latency?", "explain error budgets")
- Questions about external services or systems not in this platform (e.g. GitHub, AWS, third-party APIs)
- Any non-observability/non-SRE topics
- Questions about services or metrics not listed under "Available data" below

If a question is out of scope, set `accepted=false`, populate `rejection_reason` with a brief
explanation, and set `response` to exactly:
"I can only answer questions about your observability data.
Available services: {services_str}.
Available metrics: {metrics_str}.
I can also control dashboard panels, filters, and time range."

## Available data

Services: {services_str}
Metrics: {metrics_str}

## Database schema

### metrics
  id INTEGER PK, timestamp REAL (unix epoch), name TEXT, value REAL, service TEXT, labels TEXT

### logs
  id INTEGER PK, timestamp REAL (unix epoch), level TEXT (DEBUG/INFO/WARN/ERROR), service TEXT, message TEXT

### dashboard_state  (single row, id = 1)
  panels              TEXT   JSON array — controls visible panels: "overview","timeseries","histogram","logs"
  timeseries_metric   TEXT   metric shown in both the time-series chart and histogram
  timeseries_service  TEXT   service filter for both charts ("all" = all services)
  log_level           TEXT   log level filter ("all" or DEBUG/INFO/WARN/ERROR)
  log_keyword         TEXT   substring filter on log message (empty = no filter)
  log_service         TEXT   service filter for logs ("all" = all services)
  time_range_minutes  INT    history window in minutes — 5, 10, 15, or 30
  agent_status        TEXT   one of: idle/thinking/querying/done/error — shown in TUI footer
  agent_last_action   TEXT   human-readable description of last action shown in TUI footer
  frozen              INT    0/1 — when 1, TUI stops refreshing panels so user can inspect current view

## Health thresholds
- latency_p99:  >100ms warn,  >300ms critical
- error_rate:   >1% warn,     >5% critical

## Response format

Default (no elaborate trigger): lead with a table or bullet list. Add 1–2 sentences of
interpretation. No paragraphs. No preamble.

Elaborate triggers — if the user message contains any of these words/phrases:
  {elaborate_str}
→ expanded prose is allowed.

## How to behave
- Investigate by querying the DB (`run_query` tool). Correlate metrics with logs.
- When you spot an issue, update the dashboard (`update_dashboard` tool) to focus on it.
- Narrate: tell the engineer what you found AND mention when you've updated the dashboard.
- When a user asks to "zoom in", "look at", "focus on", or "freeze" a view, set `frozen=true` after
  updating the relevant filters/panels so they can inspect without the display changing under them.
  Set `frozen=false` when they say "unfreeze", "resume", or "continue".
- Keep SQL simple — the DB holds ~30 minutes of data at 5-second resolution.
- Prefer `WHERE timestamp > (unixepoch() - N)` for time filters.
- For accepted responses, set `accepted=true` and `rejection_reason=null`.
""".strip()


# ── Model ─────────────────────────────────────────────────────────────────────

_MODEL = os.environ.get("AGENT_MODEL", "gpt-5.4")
_USAGE_LIMITS = UsageLimits(request_limit=25)

# ── Tool implementations ──────────────────────────────────────────────────────


def _run_query(ctx: RunContext[Deps], sql: str) -> str:
    """Execute a read-only SQL query against the observability DB.

    Returns JSON: {"success": true, "columns": [...], "rows": [...]}
    or {"success": false, "error": "..."} on failure.
    Row cap: 100 rows. Timeout: 2 seconds.
    """
    db_path = ctx.deps.db_path
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, check_same_thread=False)
        result: dict = {}

        def _execute() -> None:
            try:
                cur = conn.execute(sql)
                result["rows"] = cur.fetchmany(100)
                result["desc"] = cur.description
            except Exception as exc:
                result["error"] = str(exc)

        t = threading.Thread(target=_execute, daemon=True)
        t.start()
        t.join(timeout=2.0)
        if t.is_alive():
            conn.interrupt()
            t.join()
            conn.close()
            return json.dumps({"success": False, "error": "query timeout (2s)"})

        conn.close()
        if "error" in result:
            return json.dumps({"success": False, "error": result["error"]})

        columns = [d[0] for d in result["desc"]] if result.get("desc") else []
        return json.dumps({"success": True, "columns": columns, "rows": result["rows"]})

    except Exception as exc:
        return json.dumps({"success": False, "error": str(exc)})


def _update_dashboard(
    ctx: RunContext[Deps],
    panels: list[PanelName] | None = None,
    timeseries_metric: MetricName | None = None,
    timeseries_service: ServiceFilter | None = None,
    log_level: LogLevel | None = None,
    log_keyword: str | None = None,
    log_service: ServiceFilter | None = None,
    time_range_minutes: TimeRange | None = None,
    agent_status: AgentStatus | None = None,
    agent_last_action: str | None = None,
    frozen: bool | None = None,
) -> str:
    """Update the TUI dashboard state. Only supplied fields are changed (merge semantics).

    panels: visible panels — any subset of ["overview","timeseries","histogram","logs"]
    timeseries_metric: metric shown in both charts — latency_p99/error_rate/req_per_sec/cpu_usage
    timeseries_service: service filter for both charts ("all" = all services)
    log_level: "all" or DEBUG/INFO/WARN/ERROR
    log_keyword: substring filter on log messages (empty = no filter)
    log_service: service filter for logs ("all" or a specific service name)
    time_range_minutes: history window — 5, 10, 15, or 30
    agent_status: idle/thinking/querying/done/error — shown in TUI footer
    agent_last_action: human-readable description of last action shown in TUI footer
    frozen: true = freeze UI refresh so user can inspect current view; false = resume live updates
    """
    fields: dict = {k: v for k, v in locals().items() if k != "ctx" and v is not None}
    if "panels" in fields:
        fields["panels"] = json.dumps(fields["panels"])
    if "frozen" in fields:
        fields["frozen"] = int(fields["frozen"])
    fields["updated_at"] = time.time()

    set_clause = ", ".join(f"{k} = ?" for k in fields)
    try:
        with sqlite3.connect(ctx.deps.db_path) as conn:
            conn.execute(
                f"UPDATE dashboard_state SET {set_clause} WHERE id = 1",
                list(fields.values()),
            )
        return json.dumps({"success": True})
    except Exception as exc:
        return json.dumps({"success": False, "error": str(exc)})


_trace_logger = logging.getLogger("observability_agent.trace")


# ── Chat loop ─────────────────────────────────────────────────────────────────


def run_agent() -> None:
    db_path = get_db_path()
    deps = Deps(db_path=db_path)
    history: list = []

    services, metrics = _get_available_data(db_path)
    system_prompt = _build_system_prompt(services, metrics)

    agent: Agent[Deps, AgentResponse] = Agent(
        model=_MODEL,
        deps_type=Deps,
        output_type=AgentResponse,
        system_prompt=system_prompt,
        tools=[_run_query, _update_dashboard],
    )

    print(f"SRE Copilot  [{_MODEL}]  type 'exit' to quit\n")

    while True:
        try:
            user_input = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not user_input:
            continue
        if user_input.lower() in {"exit", "quit"}:
            break

        print("agent> ", end="", flush=True)
        try:
            result = agent.run_sync(
                user_input,
                deps=deps,
                message_history=history,
                usage_limits=_USAGE_LIMITS,
            )
            output: AgentResponse = result.output
            _trace_logger.info(json.dumps({
                "timestamp": time.time(),
                "model": _MODEL,
                "query": user_input,
                "accepted": output.accepted,
                "rejection_reason": output.rejection_reason,
                "response": output.response,
                "messages": json.loads(result.new_messages_json()),
            }))
            print(output.response)
            history = result.all_messages()
        except UsageLimitExceeded:
            print("[limit] max tool-call turns reached (25) — ask a simpler question")
        except Exception as exc:
            print(f"[error] {exc}")
