"""Loglama ayarları: hem terminale hem de logs/bot.log dosyasına yazar."""
from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from config import LOG_DIR

LOGGER_NAME = "tradebot"


def setup_logging(level: str = "INFO", log_file: Path = LOG_DIR / "bot.log") -> logging.Logger:
    # Windows terminallerinde emoji/Türkçe karakterler hata vermesin diye
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.handlers.clear()
    logger.propagate = False

    fmt = logging.Formatter("%(asctime)s | %(levelname)-7s | %(message)s", "%Y-%m-%d %H:%M:%S")

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    logger.addHandler(console)

    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(log_file, maxBytes=2_000_000, backupCount=5, encoding="utf-8")
        file_handler.setFormatter(fmt)
        logger.addHandler(file_handler)
    except OSError as exc:  # Log dosyası açılamazsa bot yine de çalışsın
        logger.warning("Log dosyası açılamadı (%s): %s", log_file, exc)

    return logger


def get_logger() -> logging.Logger:
    return logging.getLogger(LOGGER_NAME)
