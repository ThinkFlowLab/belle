"""Belle's log records in JiuwenSwarm's logs."""

import logging

#: The logger that JiuwenSwarm configures, with its console and its log files.
JIUWENSWARM_LOGGER = "jiuwenswarm"


class _ToJiuwenSwarm(logging.Handler):
    """Hand each record to the handlers that JiuwenSwarm's logger has when the record comes."""

    def emit(self, record: logging.LogRecord) -> None:
        # A handler of the root logger gets the record there, as the record goes on to the root.
        root = logging.getLogger().handlers
        for handler in logging.getLogger(JIUWENSWARM_LOGGER).handlers:
            if handler not in root and record.levelno >= handler.level:
                handler.handle(record)


def share_jiuwenswarm_handlers(name: str) -> logging.Logger:
    """Send the records of logger ``name`` to JiuwenSwarm's handlers too, from JiuwenSwarm's own level.

    JiuwenSwarm configures its own logger only, and stops its records there. Another logger reaches none of its
    handlers: Python's last resort prints the warnings without time or level, and drops the rest.
    """
    shared = logging.getLogger(name)
    shared.setLevel(logging.getLogger(JIUWENSWARM_LOGGER).level)
    if not any(isinstance(handler, _ToJiuwenSwarm) for handler in shared.handlers):
        shared.addHandler(_ToJiuwenSwarm())
    return shared
