"""Logging configuration — per-session agent trace files."""
import logging
import time
from pathlib import Path

from observability_agent.db import get_db_path

_TRACE_LOGGER = "observability_agent.trace"


class _JsonlFileHandler(logging.FileHandler):
    """FileHandler that emits only the log message (already a JSON string)."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            stream = self.stream
            stream.write(record.getMessage() + "\n")
            stream.flush()
        except Exception:
            self.handleError(record)


def configure_logging() -> None:
    """Set up trace logger → agent_traces/agent_trace_<timestamp>.jsonl next to DB."""
    db_path = Path(get_db_path())
    traces_dir = db_path.parent / "agent_traces"
    traces_dir.mkdir(exist_ok=True)

    trace_file = traces_dir / f"agent_trace_{int(time.time())}.jsonl"

    handler = _JsonlFileHandler(trace_file, encoding="utf-8")
    handler.setLevel(logging.DEBUG)

    logger = logging.getLogger(_TRACE_LOGGER)
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    logger.propagate = False
