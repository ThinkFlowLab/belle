"""``python -m belle_agent.platforms.loopback``: the command that drives a running loopback platform."""

import sys

from belle_agent.platforms.loopback.control import main

sys.exit(main())
