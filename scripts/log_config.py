"""
Centralized logging configuration.

MIGRATION NOTE: Delegates to statsplusplus.utils.logging for the core setup.
Maintains backward compatibility for scripts that import get_logger from here.

Usage:
    from log_config import get_logger
    log = get_logger("refresh")
    log = get_logger("web")
"""

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_LOG_DIR = _ROOT / "data" / "logs"
_configured: dict = {}


def get_logger(name: str, level: int = logging.DEBUG) -> logging.Logger:
    """Get or create a named logger with file + console handlers."""
    if name in _configured:
        return _configured[name]

    _LOG_DIR.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger(f"statspp.{name}")
    logger.setLevel(level)
    logger.propagate = False

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-5s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # File handler
    fh = RotatingFileHandler(
        _LOG_DIR / f"{name}.log", maxBytes=5_000_000, backupCount=3,
        encoding="utf-8",
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    # Console handler
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(ch)

    _configured[name] = logger
    return logger
