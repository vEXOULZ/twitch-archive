"""Logging setup shared by archive-api and archive-worker, on vex-platform's structlog setup.

One format for everything, uvicorn included: pass ``log_config=None`` to
uvicorn so its loggers propagate here instead of installing their own
handlers and format. A job's records carry ``job`` (and ``step``) as fields,
from the ``extra`` of the job context's ``LoggerAdapter``.
"""

from __future__ import annotations

import logging
from typing import Literal

from vex_platform.logging import configure_logging

QUIET = ("httpx", "httpcore", "googleapiclient.discovery_cache")  # chatty at INFO


def setup(level: str, fmt: Literal["json", "console"] = "console") -> None:
    """Log to stdout at ``level`` (a name such as ``INFO``, any case), one JSON object per
    line with ``fmt="json"``."""
    configure_logging(level, fmt)
    # On the handler too: a logger may be set lower for another handler (the worker's
    # job event log records INFO whatever this level is), and stdout should not follow it.
    for handler in logging.getLogger().handlers:
        handler.setLevel(level.upper())
    for name in QUIET:
        logging.getLogger(name).setLevel(logging.WARNING)
