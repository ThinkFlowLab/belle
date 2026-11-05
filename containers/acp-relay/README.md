# ACP relay image

This image puts the ACP relay in front of one harness, in the harness's own
container. Belle runs in another container and reaches the relay through a
Unix socket on a volume. No network port serves ACP. See the
[ACP backend guide](../../docs/backends/acp.md).

## Build

Build the harness image first, then the relay on top of it:

```sh
podman build -f containers/docker-agent/Containerfile \
  --ignorefile containers/docker-agent/.containerignore -t localhost/belle:docker-agent-local .
podman build -f containers/acp-relay/Containerfile \
  --build-arg HARNESS_IMAGE=localhost/belle:docker-agent-local --build-arg HARNESS_USER=belle \
  -t localhost/belle:acp-relay-docker-agent .

podman build -f containers/opencode/Containerfile --target plain -t localhost/belle:opencode-plain-local .
podman build -f containers/acp-relay/Containerfile \
  --build-arg HARNESS_IMAGE=localhost/belle:opencode-plain-local --build-arg HARNESS_USER=opencode \
  -t localhost/belle:acp-relay-opencode .
```

For JiuwenSwarm, build on its image (user `app`, ID 1000). The relay runs
beside JiuwenSwarm's Gateway, which `jiuwenswarm-acp` reaches on the
container's loopback:

```sh
podman build -f containers/acp-relay/Containerfile \
  --build-arg HARNESS_IMAGE=localhost/belle:jiuwenswarm-local --build-arg HARNESS_USER=app \
  -t localhost/belle:acp-relay-jiuwenswarm .
podman run -d --name jiuwenswarm --network belle-net -v jiuwenswarm-acp:/run/belle-acp \
  -e BELLE_ACP_AGENT=jiuwenswarm-acp -e API_BASE=... -e API_KEY=... -e MODEL_NAME=... -e MODEL_PROVIDER=... \
  localhost/belle:acp-relay-jiuwenswarm /usr/local/bin/jiuwenswarm-container jiuwenswarm-app
```

## Layout

These rules keep the harness away from Belle's credentials:

- Each harness has its own socket volume. Mount it on `/run/belle-acp` in
  Belle's container and in that harness's container, and in no other.
- The relay creates the socket with a umask of `077`. Belle and the harness
  run as user ID 1000 in these images. A container under another user ID
  cannot connect.
- Give the harness container the model's address and `BELLE_TOOLS_TOKEN`,
  and no platform credential. Mount none of Belle's state in it.
- Put both containers on a private network. The harness reaches Belle's tool
  server there.

`BELLE_ACP_AGENT` is the command that the relay starts for each connection.
For OpenCode:

```sh
podman volume create opencode-acp
podman run -d --name opencode --network belle-net \
  -v opencode-acp:/run/belle-acp \
  -e OPENCODE_CONFIG=/etc/belle-acp/opencode.json -e BELLE_ACP_AGENT="opencode acp" \
  -e API_BASE=... -e API_KEY=... -e MODEL_NAME=... \
  localhost/belle:acp-relay-opencode
```

For Docker Agent, the relay entry reads the conversation's binding first. Its
session store is on a volume, so that a new process finds a session:

```sh
podman volume create docker-agent-acp
podman volume create docker-agent-data
podman run -d --name docker-agent --network belle-net \
  -v docker-agent-acp:/run/belle-acp -v docker-agent-data:/data \
  -e BELLE_ACP_AGENT="/usr/local/bin/belle-acp-relay-entry docker-agent serve acp /etc/belle-acp/docker-agent.yaml --session-db /data/acp-session.db" \
  -e BELLE_TOOLS_URL=http://belle:18300/mcp -e BELLE_TOOLS_TOKEN \
  -e API_BASE=... -e API_KEY=... -e MODEL_NAME=... \
  localhost/belle:acp-relay-docker-agent
```

Belle's container mounts the same socket volume, and selects the ACP route:

```sh
podman run -d --name belle --network belle-net \
  -v opencode-acp:/run/belle-acp \
  -e BELLE_BACKEND=acp -e BELLE_ACP_SOCKET=/run/belle-acp/agent.sock \
  -e BELLE_TOOLS_URL=http://belle:18300/mcp -e BELLE_TOOLS_TOKEN \
  <Belle's platform credentials> \
  localhost/belle:launcher-local python -m belle_agent.launcher --config /etc/belle/config.yaml
```

[`belle-opencode-loopback.yaml`](../../examples/config/belle-opencode-loopback.yaml) and
[`belle-docker-agent-loopback.yaml`](../../examples/config/belle-docker-agent-loopback.yaml)
are such configurations, with the loopback platform. `BELLE_BACKEND` selects the route.
[`contrib/acp/check-v1-layout.sh`](../../contrib/acp/check-v1-layout.sh)
checks each rule of a running layout.

## Both routes from one harness container

During a trial, one harness container can serve both routes, so that Belle
switches between them by `BELLE_BACKEND` alone. Give the relay the harness's
own server command as arguments: the relay then runs beside it. Build the
relay on the OpenCode image with the relay plugin (its default target) for
this use:

```sh
podman run -d --name opencode ... localhost/belle:acp-relay-opencode \
  opencode serve --hostname 0.0.0.0 --port 4096
podman run -d --name docker-agent ... localhost/belle:acp-relay-docker-agent \
  /usr/local/bin/belle-docker-agent
```
