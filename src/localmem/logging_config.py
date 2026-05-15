"""LOCALMEM logging — structured text or JSON output with optional file rotation."""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import LocalmemConfig


class JSONFormatter(logging.Formatter):
    """Outputs one JSON object per log line."""

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "timestamp": datetime.fromtimestamp(
                record.created, tz=timezone.utc
            ).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info and record.exc_info[0] is not None:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


def setup_logging(
    config: LocalmemConfig,
    *,
    level_override: str | None = None,
) -> None:
    """Configure the root logger from LocalmemConfig.logging settings.

    Args:
        config: Full LOCALMEM config.
        level_override: Override the config-level setting (e.g. "WARNING" for CLI).
    """
    cfg = config.logging
    level = getattr(logging, (level_override or cfg.level).upper(), logging.INFO)

    root = logging.getLogger()
    root.setLevel(level)

    # Clear any existing handlers (prevents duplicate output on re-init)
    root.handlers.clear()

    # Formatter
    if cfg.format == "json":
        formatter = JSONFormatter()
    else:
        formatter = logging.Formatter(
            "%(asctime)s %(name)s [%(levelname)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

    # Console handler (always present)
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    root.addHandler(console)

    # File handler (optional)
    if cfg.file:
        Path(cfg.file).parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            cfg.file,
            maxBytes=cfg.max_bytes,
            backupCount=cfg.backup_count,
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
