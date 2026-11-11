"""Structured (one JSON object per line) logging, so logs can be filtered by lead id."""

import json
import logging
from datetime import UTC, datetime

# Extra fields we allow callers to attach via `logger.info(..., extra={...})`.
CONTEXT_FIELDS = ("lead_id", "source", "destination", "attempt", "status", "intent", "error")


class JsonFormatter(logging.Formatter):
    """Render log records as single-line JSON with any known context fields."""

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for field in CONTEXT_FIELDS:
            value = getattr(record, field, None)
            if value is not None:
                entry[field] = value
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


def configure_logging(level: str = "INFO") -> None:
    """Install the JSON formatter on the root logger (idempotent)."""
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())
    # httpx logs every request at INFO; keep it quieter than our own events.
    logging.getLogger("httpx").setLevel(logging.WARNING)
