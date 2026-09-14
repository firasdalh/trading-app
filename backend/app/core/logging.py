"""Structured logging setup.

Logs are emitted as single-line JSON so they can be shipped to any aggregator. We never
log secrets — config secrets are redacted at the source (see ``Settings.public_dict``),
and callers must not pass API keys into ``extra``.
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone

_RESERVED = set(
    logging.makeLogRecord({}).__dict__.keys()
) | {"message", "asctime", "taskName"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        # Attach any structured extras the caller passed -- but never let one overwrite a core field.
        # `extra={"level": 4358.08}` (a price level) used to replace "level": "INFO" with the price,
        # so those records had no severity at all and vanished from any filter on level. A clashing
        # extra is kept under a prefixed name instead of being dropped.
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[f"extra_{key}" if key in payload else key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO") -> None:
    root = logging.getLogger()
    root.setLevel(level.upper())
    # Replace handlers so reloads don't stack duplicates.
    root.handlers.clear()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
    # Quiet noisy libraries a touch.
    logging.getLogger("uvicorn.access").setLevel("WARNING")
    # APScheduler logs two INFO lines ("Running job" + "executed successfully") for EVERY tick of
    # every job. With a 10s monitor, a 15s conditional tick, a 20s scan and more, that was ~80% of
    # desktop.log -- about 200k lines a week of "a timer fired" -- and the reason the file passed
    # 469 MB. Its WARNINGs (a job overran, or was skipped) are the part worth keeping.
    logging.getLogger("apscheduler").setLevel("WARNING")


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
