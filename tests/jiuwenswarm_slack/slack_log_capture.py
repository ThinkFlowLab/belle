"""Capture records directly from a Slack logger for tests."""

import logging
from collections.abc import Iterator
from contextlib import contextmanager


@contextmanager
def captured_logs(logger_name: str) -> Iterator[list[logging.LogRecord]]:
    records: list[logging.LogRecord] = []
    logger = logging.getLogger(logger_name)
    handler = logging.Handler()
    handler.emit = records.append  # type: ignore[method-assign]
    previous_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        yield records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)
