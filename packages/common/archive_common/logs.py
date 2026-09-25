"""Logging setup shared by archive-api and archive-worker.

One format for everything, uvicorn included: pass ``log_config=None`` to
uvicorn so its loggers propagate here instead of installing their own
handlers and format.
"""

from __future__ import annotations

import logging

FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
QUIET = ("httpx", "httpcore", "googleapiclient.discovery_cache")  # chatty at INFO


class _Formatter(logging.Formatter):
    """Prefixes the message with ``[job N]`` for records logged through a job's context."""

    def formatMessage(self, record: logging.LogRecord) -> str:
        job = getattr(record, "job", None)
        if job is not None:
            record.message = f"[job {job}] {record.message}"
        return super().formatMessage(record)


def setup(level: str) -> None:
    """Log to stderr at ``level`` (a name such as ``INFO``, any case)."""
    handler = logging.StreamHandler()
    handler.setFormatter(_Formatter(FORMAT))
    # On the handler too: a logger may be set lower for another handler (the worker's
    # job event log records INFO whatever this level is), and stderr should not follow it.
    handler.setLevel(level.upper())
    logging.basicConfig(level=level.upper(), handlers=[handler], force=True)
    for name in QUIET:
        logging.getLogger(name).setLevel(logging.WARNING)
