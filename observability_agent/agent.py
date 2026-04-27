"""SRE copilot agent — Pydantic AI chat loop (T08+)."""

import json
import logging
import os
import sqlite3
import subprocess
import sys
import threading
import time
from dataclasses import dataclass

from pydantic_ai import Agent, RunContext, UsageLimits
from pydantic_ai.exceptions import UsageLimitExceeded

from observability_agent.db import get_db_path
from observability_agent.models import AgentResponse, DashboardUpdate

# ── Deps ─────────────────────────────────────────────────────────────────────


@dataclass
class Deps:
    db_path: str


# ── Dynamic data discovery ────────────────────────────────────────────────────

_ELABORATE_TRIGGERS = {
    "elaborate",
    "explain",
    "tell me more",
    "detail",
    "describe",
    "walk me through",
    "breakdown",
    "deep dive",
}


def _get_available_data(db_path: str) -> tuple[list[str], list[str]]:
    """Query DB for distinct services and metrics."""
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
        services = [
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT service FROM metrics ORDER BY service"
            ).fetchall()
        ]
        metrics = [
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT name FROM metrics ORDER BY name"
            ).fetchall()
        ]
    return services or [], metrics or []


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

## Querying data (`run_analysis` tool)

Raw rows are NEVER returned to you. Always provide a `script` parameter.

The script receives {{"columns": [...], "rows": [...]}} on stdin and must print a JSON
summary to stdout. Use it to compute: averages, min/max, percentiles, rolling averages,
rate of change, counts, stddev, etc.

Omit `script` only when you need to inspect column names or row count before writing a script.

Allowed stdlib: json, math, statistics, collections, itertools, functools, operator, decimal, fractions.
No file I/O, no network, no subprocess.

Example script skeleton:
```python
import json, sys, statistics
d = json.load(sys.stdin)
cols = d["columns"]
rows = d["rows"]
idx = cols.index("value")
vals = [r[idx] for r in rows]
print(json.dumps({{"mean": statistics.mean(vals), "p99": sorted(vals)[int(len(vals)*0.99)], "max": max(vals)}}))
```
""".strip()


# ── Model ─────────────────────────────────────────────────────────────────────

_MODEL = os.environ.get("AGENT_MODEL", "gpt-5.4")
_USAGE_LIMITS = UsageLimits(request_limit=25)

# ── Tool implementations ──────────────────────────────────────────────────────


_BLOCKED_PATTERNS = [
    "import os",
    "import sys",
    "import subprocess",
    "import socket",
    "import urllib",
    "import http",
    "import requests",
    "import pathlib",
    "import shutil",
    "import glob",
    "open(",
    "__import__",
    "exec(",
    "eval(",
    "compile(",
    "__builtins__",
    "importlib",
]


def _validate_script(script: str) -> str | None:
    """Return error string if script is unsafe, else None."""
    lowered = script.lower()
    for pattern in _BLOCKED_PATTERNS:
        if pattern.lower() in lowered:
            return f"disallowed pattern: {pattern!r}"
    return None


def _run_analysis(ctx: RunContext[Deps], sql: str, script: str | None = None) -> str:
    """Execute a read-only SQL query against the observability DB.

    Raw rows are never returned to avoid bloating context.

    If script is None: returns columns, row count, and a 3-row sample only.
    If script is provided: pipes all rows (up to 500) into the script via stdin
      as JSON {"columns": [...], "rows": [...]} and returns the script's stdout.

    script must read JSON from stdin and print a JSON summary to stdout.
    Allowed stdlib: json, math, statistics, collections, itertools, functools,
    operator, decimal, fractions. No file I/O, no network, no subprocess.

    Returns JSON: {"success": true, "columns": [...], "row_count": N, "sample": [...]}
    or {"success": true, "summary": <script output>}
    or {"success": false, "error": "..."}.
    Row fetch cap: 500. Query timeout: 2 seconds. Script timeout: 5 seconds.
    """
    if script is not None:
        err = _validate_script(script)
        if err:
            return json.dumps({"success": False, "error": f"script rejected — {err}"})

    db_path = ctx.deps.db_path
    try:
        conn = sqlite3.connect(
            f"file:{db_path}?mode=ro", uri=True, check_same_thread=False
        )
        result: dict = {}

        cap = 500 if script is not None else 100

        def _execute() -> None:
            try:
                cur = conn.execute(sql)
                result["rows"] = cur.fetchmany(cap)
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
        rows = result["rows"]

        if script is None:
            return json.dumps(
                {
                    "success": True,
                    "columns": columns,
                    "row_count": len(rows),
                    "sample": rows[:3],
                }
            )

        # Run summarization script
        stdin_payload = json.dumps({"columns": columns, "rows": rows})
        try:
            proc = subprocess.run(
                [sys.executable, "-c", script],
                input=stdin_payload,
                capture_output=True,
                text=True,
                timeout=5,
            )
        except subprocess.TimeoutExpired:
            return json.dumps({"success": False, "error": "script timeout (5s)"})

        if proc.returncode != 0:
            return json.dumps(
                {
                    "success": False,
                    "error": f"script error: {proc.stderr.strip()[:500]}",
                }
            )

        raw = proc.stdout.strip()
        try:
            summary = json.loads(raw)
        except json.JSONDecodeError:
            summary = raw[:2000]

        return json.dumps({"success": True, "summary": summary})

    except Exception as exc:
        return json.dumps({"success": False, "error": str(exc)})


def _update_dashboard(ctx: RunContext[Deps], update: DashboardUpdate) -> str:
    """Update the TUI dashboard state. Only supplied fields are changed (merge semantics).

    update.panels: visible panels — any subset of ["overview","timeseries","histogram","logs"]
    update.timeseries_metric: metric shown in both charts — latency_p99/error_rate/req_per_sec/cpu_usage
    update.timeseries_service: service filter for both charts ("all" = all services)
    update.log_level: "all" or DEBUG/INFO/WARN/ERROR
    update.log_keyword: substring filter on log messages (empty = no filter)
    update.log_service: service filter for logs ("all" or a specific service name)
    update.time_range_minutes: history window — 5, 10, 15, or 30
    update.agent_status: idle/thinking/querying/done/error — shown in TUI footer
    update.agent_last_action: human-readable description of last action shown in TUI footer
    update.frozen: true = freeze UI refresh so user can inspect current view; false = resume live updates
    """
    fields: dict = {k: v for k, v in update.model_dump(exclude_none=True).items()}
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
        tools=[_run_analysis, _update_dashboard],
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
            _trace_logger.info(
                json.dumps(
                    {
                        "timestamp": time.time(),
                        "model": _MODEL,
                        "query": user_input,
                        "accepted": output.accepted,
                        "rejection_reason": output.rejection_reason,
                        "response": output.response,
                        "messages": json.loads(result.new_messages_json()),
                    }
                )
            )
            print(output.response)
            history = result.all_messages()
        except UsageLimitExceeded:
            print("[limit] max tool-call turns reached (25) — ask a simpler question")
        except Exception as exc:
            print(f"[error] {exc}")
