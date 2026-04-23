"""Synthetic metric + log generator with two anomaly scenarios."""

import random
import sqlite3
import threading
import time

SERVICES = ["api-gateway", "payment-service", "user-service", "inventory-service"]
METRICS = ["latency_p99", "error_rate", "req_per_sec", "cpu_usage"]

# Healthy baseline values
_BASE: dict[str, dict[str, float]] = {
    "api-gateway": {
        "latency_p99": 50.0,
        "error_rate": 0.005,
        "req_per_sec": 500.0,
        "cpu_usage": 0.30,
    },
    "payment-service": {
        "latency_p99": 150.0,
        "error_rate": 0.005,
        "req_per_sec": 200.0,
        "cpu_usage": 0.35,
    },
    "user-service": {
        "latency_p99": 80.0,
        "error_rate": 0.005,
        "req_per_sec": 300.0,
        "cpu_usage": 0.30,
    },
    "inventory-service": {
        "latency_p99": 60.0,
        "error_rate": 0.005,
        "req_per_sec": 150.0,
        "cpu_usage": 0.25,
    },
}

# Gaussian noise as fraction of base value
_NOISE: dict[str, float] = {
    "latency_p99": 0.06,
    "error_rate": 0.15,
    "req_per_sec": 0.03,
    "cpu_usage": 0.02,
}

# Log message templates per service / level
_LOG_MSGS: dict[str, dict[str, list[str]]] = {
    "api-gateway": {
        "DEBUG": [
            "Request received: GET /api/v1/products",
            "Cache lookup: HIT for key products:{pid}",
            "Routing request to upstream: payment-service",
            "keepalive ping sent to payment-service",
        ],
        "INFO": [
            "Request completed: 200 OK in {latency}ms",
            "Health check: OK",
            "Connection pool: {pool}/100 active",
            "TLS handshake complete: client 10.0.{a}.{b}",
        ],
        "WARN": [
            "Upstream latency elevated: {latency}ms",
            "Rate limiting applied to client 10.0.{a}.{b}",
            "Retry 1/3 to payment-service",
            "Circuit breaker: half-open state",
        ],
        "ERROR": [
            "timeout waiting for payment-service",
            "upstream connection refused: payment-service:8080",
            "retry limit exceeded for payment-service",
            "circuit breaker OPEN: payment-service",
        ],
    },
    "payment-service": {
        "DEBUG": [
            "Processing payment request: txn-{txn}",
            "DB query plan: idx_users_id on users",
            "Cache miss: payment config v{ver}",
            "Acquiring connection from pool ({pool}/50)",
        ],
        "INFO": [
            "Payment processed: txn-{txn} in {latency}ms",
            "DB connection pool: {pool}/50 active",
            "Reconciliation task complete: {n} records",
            "Idempotency cache hit: txn-{txn}",
        ],
        "WARN": [
            "DB query slow: {latency}ms (threshold 100ms)",
            "Connection pool utilisation: {pool}%",
            "High error rate detected: {rate}%",
            "Batch retry queue depth: {n}",
        ],
        "ERROR": [
            "Payment failed: timeout on DB write txn-{txn}",
            "connection pool exhausted",
            "DB connection refused: postgres:5432",
            "Saga rollback triggered: txn-{txn}",
        ],
    },
    "user-service": {
        "DEBUG": [
            "Session validated: user-{uid}",
            "Cache lookup: user profile user-{uid}",
            "Auth token refresh: user-{uid}",
            "RBAC check: user-{uid} resource=orders",
        ],
        "INFO": [
            "User authenticated: user-{uid} in {latency}ms",
            "Profile updated: user-{uid}",
            "GC stats: heap={heap}MB collected={coll}MB pause={pause}ms",
            "Scheduled job: session-cleanup complete ({n} sessions)",
        ],
        "WARN": [
            "heap usage {heap}%",
            "GC pause {pause}ms (threshold 200ms)",
            "High memory pressure detected",
            "Live object count: {n}k — possible leak",
        ],
        "ERROR": [
            "OOM killed: heap usage exceeded limit",
            "service restarted after OOM",
            "GC overhead limit exceeded",
            "Out of memory: kill process user-service pid={pid}",
        ],
    },
    "inventory-service": {
        "DEBUG": [
            "Inventory lookup: product-{pid}",
            "Cache hit: stock count product-{pid}",
            "Webhook received: supplier-{pid}",
        ],
        "INFO": [
            "Stock updated: product-{pid} qty={qty}",
            "Batch sync complete: {n} products",
            "Reorder triggered: product-{pid} qty<{qty}",
        ],
        "WARN": [
            "Low stock warning: product-{pid} qty={qty}",
            "Supplier sync delay: {delay}s",
            "Cache eviction rate high: {rate}%",
        ],
        "ERROR": [
            "Inventory sync failed: connection timeout",
            "Stock update conflict: product-{pid}",
            "DB deadlock detected: retrying",
        ],
    },
}


def _fmt(tmpl: str) -> str:
    return tmpl.format(
        txn=random.randint(100000, 999999),
        uid=random.randint(1000, 9999),
        latency=random.randint(10, 600),
        pool=random.randint(1, 100),
        rate=round(random.uniform(0.1, 12.0), 1),
        heap=random.randint(50, 95),
        pause=random.randint(20, 500),
        coll=random.randint(10, 200),
        pid=random.randint(1, 1000),
        qty=random.randint(0, 500),
        n=random.randint(10, 1000),
        delay=random.randint(1, 30),
        ver=random.randint(1, 5),
        a=random.randint(0, 10),
        b=random.randint(1, 254),
    )


# ── Scenario engine ─────────────────────────────────────────────────────────


def _lerp(a: float, b: float, t: float) -> float:
    t = max(0.0, min(1.0, t))
    return a + (b - a) * t


def _compute_metric(service: str, metric: str, minutes: float, scenario: int) -> float:
    """Return metric value at `minutes` minutes into the demo for the given scenario."""
    base = _BASE[service][metric]
    noise = random.gauss(0, base * _NOISE[metric])

    # Scenario 1: Cascade Failure — payment-service degrades, api-gateway follows
    if scenario == 1:
        if service == "payment-service":
            if metric == "error_rate" and minutes >= 15:
                base = _lerp(0.005, 0.08, (minutes - 15) / 4.0)
            elif metric == "latency_p99" and minutes >= 18:
                base = _lerp(150.0, 480.0, (minutes - 18) / 3.0)
        elif service == "api-gateway":
            if metric == "latency_p99" and minutes >= 20:
                base = _lerp(50.0, 140.0, (minutes - 20) / 6.0)
            elif metric == "error_rate" and minutes >= 21:
                base = _lerp(0.005, 0.03, (minutes - 21) / 5.0)

    # Scenario 2: Memory Leak — user-service slow-burn OOM, restart, resume
    elif scenario == 2 and service == "user-service":
        # CPU climbs from minute 10
        if metric == "cpu_usage":
            if 10 <= minutes < 24:
                base = _lerp(0.30, 0.92, (minutes - 10) / 14.0)
            elif 24 <= minutes < 26:  # restart — brief recovery
                base = _lerp(0.92, 0.32, (minutes - 24) / 2.0)
            elif minutes >= 26:  # resumes climbing
                base = _lerp(0.32, 0.75, (minutes - 26) / 8.0)
        elif metric == "latency_p99":
            if 18 <= minutes < 24:
                base = _lerp(80.0, 320.0, (minutes - 18) / 6.0)
            elif 24 <= minutes < 26:
                base = _lerp(320.0, 80.0, (minutes - 24) / 2.0)
            elif minutes >= 26:
                base = _lerp(80.0, 260.0, (minutes - 26) / 6.0)
        elif metric == "error_rate":
            if 22 <= minutes < 24:
                base = _lerp(0.005, 0.15, (minutes - 22) / 2.0)
            elif 24 <= minutes < 26:
                base = _lerp(0.15, 0.005, (minutes - 24) / 2.0)
            elif minutes >= 26:
                base = _lerp(0.005, 0.12, (minutes - 26) / 6.0)

    value = base + noise
    if metric == "error_rate":
        value = max(0.0, min(1.0, value))
    elif metric == "cpu_usage":
        value = max(0.0, min(1.0, value))
    else:
        value = max(0.0, value)
    return value


def _anomaly_log(
    service: str, minutes: float, scenario: int
) -> tuple[str, str] | None:
    """Return (level, message) if an anomaly log should be injected, else None."""
    if scenario == 1:
        if service == "payment-service" and minutes >= 15 and random.random() < 0.35:
            msg = random.choice(
                [
                    "connection pool exhausted",
                    "DB connection refused: postgres:5432",
                    "Payment failed: timeout on DB write txn-{txn}",
                    "Saga rollback triggered: txn-{txn}",
                ]
            )
            return ("ERROR", _fmt(msg))
        if service == "api-gateway" and minutes >= 20 and random.random() < 0.35:
            msg = random.choice(
                [
                    "timeout waiting for payment-service",
                    "retry limit exceeded for payment-service",
                    "circuit breaker OPEN: payment-service",
                    "upstream connection refused: payment-service:8080",
                ]
            )
            return ("ERROR", _fmt(msg))
    elif scenario == 2:
        if service == "user-service":
            if 22 <= minutes < 26 and random.random() < 0.50:
                msg = random.choice(
                    [
                        "heap usage 89%",
                        "GC pause 450ms (threshold 200ms)",
                        "OOM killed: heap usage exceeded limit",
                        "service restarted after OOM",
                        "Out of memory: kill process user-service pid={pid}",
                    ]
                )
                level = (
                    "ERROR"
                    if "OOM" in msg or "killed" in msg or "kill" in msg
                    else "WARN"
                )
                return (level, _fmt(msg))
            elif 18 <= minutes < 22 and random.random() < 0.35:
                msg = random.choice(
                    [
                        "heap usage {heap}%",
                        "GC pause {pause}ms (threshold 200ms)",
                        "Live object count: {n}k — possible leak",
                    ]
                )
                return ("WARN", _fmt(msg))
    return None


def _level_weights(error_rate: float) -> list[float]:
    if error_rate > 0.05:
        return [0.05, 0.20, 0.30, 0.45]
    elif error_rate > 0.01:
        return [0.10, 0.35, 0.40, 0.15]
    return [0.20, 0.62, 0.16, 0.02]


def _gen_log(
    service: str, ts: float, minutes: float, scenario: int, metric_vals: dict
) -> tuple:
    """Return (timestamp, level, service, message)."""
    injected = _anomaly_log(service, minutes, scenario)
    if injected:
        level, msg = injected
        return (ts, level, service, msg)

    weights = _level_weights(metric_vals.get("error_rate", 0.005))
    level = random.choices(["DEBUG", "INFO", "WARN", "ERROR"], weights=weights)[0]
    templates = _LOG_MSGS.get(service, {}).get(level, ["log entry"])
    return (ts, level, service, _fmt(random.choice(templates)))


# ── Module-level scenario state (shared between backfill + stream) ───────────

_demo_epoch: float | None = None
_scenario: int | None = None
_state_lock = threading.Lock()


def _ensure_scenario(minutes: int = 30) -> tuple[float, int]:
    global _demo_epoch, _scenario
    with _state_lock:
        if _demo_epoch is None:
            _demo_epoch = time.time() - minutes * 60
        if _scenario is None:
            _scenario = random.randint(1, 2)
        return _demo_epoch, _scenario


def reset_scenario(scenario: int | None = None) -> None:
    """Force a new scenario (call before backfill on fresh start).

    Args:
        scenario: 1 = Cascade Failure, 2 = Memory Leak, None = random.
    """
    global _demo_epoch, _scenario
    with _state_lock:
        _demo_epoch = None
        _scenario = scenario


# ── Public API ───────────────────────────────────────────────────────────────


def backfill(db_path: str, minutes: int = 30, resolution_sec: int = 5) -> None:
    """Insert historical data covering the last `minutes` minutes."""
    demo_epoch, scenario = _ensure_scenario(minutes)
    now = time.time()

    metric_rows: list[tuple] = []
    log_rows: list[tuple] = []

    t = demo_epoch
    while t <= now:
        mins = (t - demo_epoch) / 60.0
        for service in SERVICES:
            mv: dict[str, float] = {}
            for metric in METRICS:
                v = _compute_metric(service, metric, mins, scenario)
                mv[metric] = v
                metric_rows.append((t, metric, v, service, "{}"))
            num_logs = random.randint(0, 2)
            for _ in range(num_logs):
                jitter = random.uniform(0, resolution_sec)
                log_rows.append(_gen_log(service, t + jitter, mins, scenario, mv))
        t += resolution_sec

    with sqlite3.connect(db_path) as conn:
        conn.executemany(
            "INSERT INTO metrics (timestamp, name, value, service, labels) VALUES (?,?,?,?,?)",
            metric_rows,
        )
        conn.executemany(
            "INSERT INTO logs (timestamp, level, service, message) VALUES (?,?,?,?)",
            log_rows,
        )


def stream(
    db_path: str,
    interval_sec: float = 1.0,
    stop_event: threading.Event | None = None,
) -> None:
    """Generate live data points until `stop_event` is set (or forever)."""
    demo_epoch, scenario = _ensure_scenario()

    while stop_event is None or not stop_event.is_set():
        now = time.time()
        mins = (now - demo_epoch) / 60.0

        metric_rows: list[tuple] = []
        log_rows: list[tuple] = []

        for service in SERVICES:
            mv: dict[str, float] = {}
            for metric in METRICS:
                v = _compute_metric(service, metric, mins, scenario)
                mv[metric] = v
                metric_rows.append((now, metric, v, service, "{}"))
            if random.random() < 0.6:
                log_rows.append(_gen_log(service, now, mins, scenario, mv))

        with sqlite3.connect(db_path) as conn:
            conn.executemany(
                "INSERT INTO metrics (timestamp, name, value, service, labels) VALUES (?,?,?,?,?)",
                metric_rows,
            )
            if log_rows:
                conn.executemany(
                    "INSERT INTO logs (timestamp, level, service, message) VALUES (?,?,?,?)",
                    log_rows,
                )

        time.sleep(interval_sec)
