from __future__ import annotations

import logging
import os
import re
import sys
from pathlib import Path


class SafeFormatter(logging.Formatter):
    """Keep operational logs structured without recording request content."""

    def format(self, record: logging.LogRecord) -> str:
        record.message = record.getMessage()
        rendered = super().format(record)
        return re.sub(
            r"(?i)(?:bearer\s+|(?:xox[baprs]-|nvapi-))[A-Za-z0-9._-]+",
            "[REDACTED]",
            rendered,
        )


def configure_logging(level: str = "INFO", file_path: str | None = None) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if file_path:
        path = Path(file_path).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(path))
    formatter = SafeFormatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    for handler in handlers:
        handler.setFormatter(formatter)
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        handlers=handlers,
        force=True,
    )


def configure_logging_from_env() -> None:
    configure_logging(os.getenv("LOG_LEVEL", "INFO"), os.getenv("LOG_FILE") or None)
