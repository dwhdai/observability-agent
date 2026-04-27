# Observability Agent

SRE copilot — terminal dashboard with synthetic metrics/logs and an AI agent that can query data and control the dashboard.

## Install

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
make install
```

## Usage

### TUI Dashboard

```bash
make tui                  # random scenario
make tui SCENARIO=1       # Cascade Failure (api-gateway → payment-service)
make tui SCENARIO=2       # Memory Leak (user-service OOM)
make tui SCENARIO=3       # LLM Rate Limiting (agent-app → llm-router 429s)
```

Keybindings: `r` reset view · `f` freeze · `q` quit

### AI Agent

Run in a separate terminal while the TUI is open:

```bash
make agent
```

The agent reads from the same SQLite database and can query metrics/logs and update the dashboard in real time.

## Demo Queries

**Overview**
```
what services are running?
what's the current health of all services?
```

**Scenario 1 — Cascade Failure**
```
which service has the highest error rate?
show me payment-service latency over the last 15 minutes
are there any errors in api-gateway logs?
```

**Scenario 2 — Memory Leak**
```
is user-service showing signs of a memory leak?
show me cpu usage for user-service
filter logs to ERROR level for user-service
```

**Scenario 3 — LLM Rate Limiting**
```
why is agent-app latency spiking?
show me llm-router error rate
how many 429 errors has llm-router returned in the last 10 minutes?
focus the dashboard on llm-router latency
```

**General**
```
show me the top errors in the last 5 minutes
what changed in the last 10 minutes?
focus the timeseries on error_rate for all services
```
