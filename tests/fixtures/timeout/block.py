"""An async test that blocks its event loop past OCB_TEST_TIMEOUT. ``tests/test_test_timeout.py`` runs it."""

import time


async def test_blocks():
    time.sleep(30)
