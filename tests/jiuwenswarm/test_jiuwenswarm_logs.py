"""Belle's log records reach the handlers that JiuwenSwarm configures."""

import logging

import pytest

from belle_agent.integrations.jiuwenswarm.logs import JIUWENSWARM_LOGGER, share_jiuwenswarm_handlers


class _Kept(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


@pytest.fixture
def jiuwenswarm():
    """JiuwenSwarm's logger at INFO, as ``setup_logger`` leaves it, restored afterwards with Belle's."""
    host, belle = logging.getLogger(JIUWENSWARM_LOGGER), logging.getLogger("belle_agent")
    saved = (host.level, list(host.handlers), belle.level, list(belle.handlers))
    host.handlers[:] = []
    host.setLevel(logging.INFO)
    belle.handlers[:] = []
    belle.setLevel(logging.NOTSET)
    yield host
    host.setLevel(saved[0])
    host.handlers[:] = saved[1]
    belle.setLevel(saved[2])
    belle.handlers[:] = saved[3]


def test_an_info_record_of_belle_reaches_a_handler_that_jiuwenswarm_has_now_once(jiuwenswarm):
    share_jiuwenswarm_handlers("belle_agent")
    share_jiuwenswarm_handlers("belle_agent")
    later = _Kept()
    jiuwenswarm.addHandler(later)

    connector = logging.getLogger("belle_agent.platforms.matrix.connector")
    connector.info("matrix gave no turn to %d message(s)", 2)
    connector.debug("below JiuwenSwarm's level")

    assert later.messages == ["matrix gave no turn to 2 message(s)"]


def test_a_handler_of_the_root_logger_too_gets_a_record_once(jiuwenswarm):
    both = _Kept()
    jiuwenswarm.addHandler(both)
    logging.getLogger().addHandler(both)
    try:
        share_jiuwenswarm_handlers("belle_agent")
        logging.getLogger("belle_agent.host").warning("matrix could not start")
    finally:
        logging.getLogger().removeHandler(both)

    assert both.messages == ["matrix could not start"]
