"""Structured JSON logging via structlog.

Every log line carries a ``run_id`` so a full pipeline execution can be
replayed end-to-end with ``jq 'select(.run_id == ...)'``.
"""

from __future__ import annotations

import logging
import sys
import uuid
from pathlib import Path

import structlog


def new_run_id() -> str:
    return uuid.uuid4().hex[:12]


def configure_logging(level: str = "INFO", fmt: str = "json", file: str | None = None) -> None:
    log_level = getattr(logging, level.upper(), logging.INFO)

    # Route stdlib logging (used by structlog and noisy libs like httpx/anthropic)
    # through a shared formatter so file + stdout get the same output.
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if file:
        Path(file).parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(file, encoding="utf-8"))
    logging.basicConfig(level=log_level, format="%(message)s", handlers=handlers, force=True)

    # Suppress noisy third-party transport logs that aren't JSON.
    for noisy in ("httpx", "httpcore", "anthropic", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    shared_processors: list = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    renderer = (
        structlog.processors.JSONRenderer()
        if fmt == "json"
        else structlog.dev.ConsoleRenderer(colors=True)
    )

    structlog.configure(
        processors=[
            *shared_processors,
            # Route into stdlib so our handlers (file + stdout) see the line.
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        processor=renderer, foreign_pre_chain=shared_processors
    )
    for h in handlers:
        h.setFormatter(formatter)


def bind_run_id(run_id: str) -> None:
    structlog.contextvars.bind_contextvars(run_id=run_id)


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
