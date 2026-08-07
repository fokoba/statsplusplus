"""Centralized logging configuration.

Provides a single entry point for getting loggers with consistent formatting.
Logs to both console and rotating file (when data directory exists).
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

_configured = False


def setup_logging(log_dir: Path | None = None, level: int = logging.INFO) -> None:
    """Configure root logger with console + optional file handler.

    Safe to call multiple times — only configures once.

    Args:
        log_dir: Directory for log files. If None, logs to console only.
        level: Logging level (default INFO).
    """
    global _configured
    if _configured:
        return
    _configured = True

    root = logging.getLogger("statspp")
    root.setLevel(level)

    fmt = logging.Formatter(
        "%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Console handler (always)
    console = logging.StreamHandler(sys.stderr)
    console.setLevel(level)
    console.setFormatter(fmt)
    root.addHandler(console)

    # File handler (when log_dir provided and writable)
    if log_dir is not None:
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            fh = RotatingFileHandler(
                log_dir / "statspp.log",
                maxBytes=5_000_000,
                backupCount=3,
            )
            fh.setLevel(level)
            fh.setFormatter(fmt)
            root.addHandler(fh)
        except OSError:
            pass  # Can't write logs — continue without file handler


def get_logger(name: str) -> logging.Logger:
    """Get a logger under the statspp namespace.

    Args:
        name: Logger name suffix (e.g., "web", "evaluation", "refresh").

    Returns:
        Logger instance like "statspp.web".
    """
    return logging.getLogger(f"statspp.{name}")
