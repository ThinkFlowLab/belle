#!/bin/sh
# The relay runs this for each connection. The first line is the conversation's binding, the rest is ACP.
# The agent command follows as arguments and gets the binding in BELLE_TURN.
# POSIX read reads no byte past the newline, so the agent gets each ACP byte.
read -r BELLE_TURN || exit 1
export BELLE_TURN
exec "$@"
