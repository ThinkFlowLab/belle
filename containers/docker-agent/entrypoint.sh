#!/bin/sh
# Start `docker-agent serve api` with Belle's agent YAML.
# Arguments that start with `-` are added to the serve command. Other arguments run as a command.
set -eu

case "${1:--}" in
    -*) ;;
    *) exec "$@" ;;
esac

# The server takes its bearer token only as a flag. The variable is removed so that hook commands do not get it.
token=${DOCKER_AGENT_AUTH_TOKEN:?set DOCKER_AGENT_AUTH_TOKEN}
unset DOCKER_AGENT_AUTH_TOKEN
# Belle reads the same variable for backends.docker_agent.max_request_bytes. Unset, both use 1 MiB.
if [ -n "${DOCKER_AGENT_MAX_REQUEST_BYTES:-}" ]; then
    set -- --max-request-size "$DOCKER_AGENT_MAX_REQUEST_BYTES" "$@"
fi
exec docker-agent serve api /etc/docker-agent/belle.yaml --listen 0.0.0.0:8080 \
    --session-db /data/session.db --auth-token "$token" "$@"
