#!/bin/sh
# Serve the ACP relay on BELLE_ACP_SOCKET: one BELLE_ACP_AGENT process per connection, with its standard input
# and output as two pipes. The socket is the relay user's only (umask 077).
# Without arguments the relay is the container's process. With arguments it runs beside them.
set -eu
: "${BELLE_ACP_SOCKET:?set BELLE_ACP_SOCKET}" "${BELLE_ACP_AGENT:?set BELLE_ACP_AGENT}"
if [ "$#" -eq 0 ]; then
    exec socat "UNIX-LISTEN:${BELLE_ACP_SOCKET},fork,unlink-early,umask=077" "EXEC:${BELLE_ACP_AGENT},pipes"
fi
socat "UNIX-LISTEN:${BELLE_ACP_SOCKET},fork,unlink-early,umask=077" "EXEC:${BELLE_ACP_AGENT},pipes" &
exec "$@"
