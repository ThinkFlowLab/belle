# Belle + Docker Agent quick start

This image runs `docker-agent serve api` with Belle's agent YAML and Belle's
packaged skills. It holds a pinned Docker Agent release, `curl` for the relay
hooks, and no credentials. Belle runs in the launcher image beside it.

## One command

[`compose.yaml`](compose.yaml) starts both containers on a private network.
`docker compose` takes the same commands as `podman compose`, which needs
`podman-compose` or `docker-compose` installed.

> [!NOTE]
> This repository publishes no images. [Build them from a clone](#build-from-a-clone)
> and set `BELLE_REGISTRY=localhost` and `BELLE_VERSION=local`, or push them to
> your own registry and name it below.

In an empty directory, put this `compose.yaml`, write `model.env` and
`belle.env` as the steps below give them, and a `.env` that names the
images:

```dotenv
BELLE_REGISTRY=<registry>/<namespace>
BELLE_VERSION=v<version>
```

```sh
podman compose up -d
podman compose logs -f belle
```

Compose pulls both images and builds nothing. The file sets the values of
`pair-docker-agent.env` itself, and reads no pairing file.
`DOCKER_AGENT_AUTH_TOKEN` and `BELLE_RELAY_TOKEN` default to fixed trial values
that this repository publishes. No port is published, so only the two
containers reach each other. Before you publish a port or connect another
container to the network, add your own values to `.env`, and keep it mode
`0600`:

```sh
chmod 600 .env
printf 'DOCKER_AGENT_AUTH_TOKEN=%s\nBELLE_RELAY_TOKEN=%s\n' "$(openssl rand -hex 32)" "$(openssl rand -hex 32)" >> .env
```

Named volumes keep the state: `docker-agent-data` on `/data`, and
`belle-state` and `belle-data` on Belle's two directories.
`podman compose down -v` removes them. On Mattermost, the server reaches
Belle at `http://belle:18400` once it is on the network
`belle-docker-agent_default`.

### Build from a clone

In a clone, write the env files in this directory, with this `.env`:

```dotenv
BELLE_REGISTRY=localhost
BELLE_VERSION=local
```

Then build both images and start the containers from this directory:

```sh
podman compose up -d --build
```

The images get the tags of the steps below, `localhost/belle:docker-agent-local` and
`localhost/belle:launcher-local`. Without `--build`, Compose uses those
images when they exist and builds them when they do not.

## Build

Build both images from the repository root:

```sh
podman build -f containers/docker-agent/Containerfile \
  --ignorefile containers/docker-agent/.containerignore \
  -t localhost/belle:docker-agent-local .
podman build -f containers/launcher/Containerfile -t localhost/belle:launcher-local .
```

Build arguments of the Docker Agent image:

| Argument | Default | Meaning |
| --- | --- | --- |
| `DEBIAN_TAG` | `bookworm-slim` | The base image. |
| `DOCKER_AGENT_VERSION` | the pinned release | The release tag of the binary. |
| `DOCKER_AGENT_SHA256_AMD64`, `DOCKER_AGENT_SHA256_ARM64` | the published digests of the pinned release | The build fails when the binary does not match. Change them with the version. |
| `AGENT_YAML` | `src/belle_agent/integrations/docker_agent/agent/belle.yaml` | The agent YAML in the image. `.containerignore` admits only the default file, so build without `--ignorefile` to use another file. |

## Try it

Create three files with mode `0600`. Use literal values: Podman does not expand
`${NAME}` inside `--env-file`. `openssl rand -hex 32` prints a value for each
secret.

`model.env` (the harness container only):

```dotenv
API_BASE=https://openrouter.ai/api/v1
API_KEY=replace-me
MODEL_NAME=replace-me
```

`pair-docker-agent.env` (both containers):

```dotenv
BELLE_BACKEND=docker_agent
DOCKER_AGENT_URL=http://belle-docker-agent:8080
DOCKER_AGENT_AUTH_TOKEN=replace-with-a-random-secret
BELLE_TOOLS_URL=http://belle:18300/mcp
BELLE_RELAY_URL=http://belle:18200
BELLE_RELAY_TOKEN=replace-with-another-random-secret
DOCKER_AGENT_MAX_REQUEST_BYTES=16777216
```

`belle.env` (Belle only):

```dotenv
SLACK_BOT_TOKEN=xoxb-replace-me
SLACK_APP_TOKEN=xapp-replace-me
```

For Mattermost, `belle.env` holds the Mattermost variables instead. See
[the Mattermost guide](../../docs/platforms/mattermost.md#quick-start-with-containers).
For Matrix, it holds the Matrix variables, and Belle needs its state and
data volumes. The first start of a Matrix account writes its recovery key
and stops. See
[the Matrix guide](../../docs/platforms/matrix.md#quick-start-with-containers).

Start Docker Agent before Belle. Use a Slack app token that no other running
connector uses.

```sh
podman network create belle-net
podman run -d --name belle-docker-agent --network belle-net --read-only \
  --env-file model.env --env-file pair-docker-agent.env \
  localhost/belle:docker-agent-local
podman run -d --name belle --network belle-net \
  --env-file pair-docker-agent.env --env-file belle.env \
  localhost/belle:launcher-local
podman logs belle
```

The [OpenCode guide](../opencode/README.md) uses the same launcher image and the
same `model.env`. To change harness, remove both containers. Then start the
other harness container and the launcher container with that harness's pairing
file.

Belle's bundled config answers direct messages and uses ambient participation
in channels. To change its scopes or other settings, mount your config file at
`/home/belle/.config/belle-agent/config.yaml`. See
[the config guide](../../docs/config.md) for its keys.

The commands above keep state inside disposable containers. For a persistent
deployment, mount writable volumes at `/data` in the Docker Agent container,
and at `/home/belle/.local/share/belle-agent` and
`/home/belle/.local/state/belle-agent` in the Belle container. Belle's state
directory holds the Docker Agent session of each conversation, and its data
directory holds the second-brain corpus. A mounted config directory hides the
baked config, so supply a `config.yaml` in it. Recreate a container after you
change an env file: `podman restart` does not read the file again.

## Variables

| Variable | Read by | Meaning |
| --- | --- | --- |
| `API_BASE`, `API_KEY`, `MODEL_NAME` | the agent YAML | The model: an endpoint that speaks the OpenAI chat completions API. |
| `DOCKER_AGENT_AUTH_TOKEN` | the entry script, and Belle | The server's bearer token. Required. The server gets it as `--auth-token`, and the hook commands do not get it. |
| `BELLE_TOOLS_URL` | the agent YAML, and Belle | Where Docker Agent reaches Belle's tool server. |
| `BELLE_RELAY_URL`, `BELLE_RELAY_TOKEN` | the relay hooks, and Belle | Belle's hook endpoint and its token. |
| `DOCKER_AGENT_MAX_REQUEST_BYTES` | the entry script, and Belle | The largest turn request, files included. The server gets it as `--max-request-size`. Belle reads it as `backends.docker_agent.max_request_bytes`. Unset, both use 1048576. |

The image sets `TELEMETRY_ENABLED=false`, `DOCKER_AGENT_AUTO_UPDATE=0` and
`DOCKER_AGENT_AUTO_INSTALL=false`. Arguments that start with `-` go to
`docker-agent serve api`, for example `--session-workingdir-root /data`.

## State and a read-only container

The server listens on port 8080 of the container network. A request without
the token gets HTTP 401. The server still prints at start that the API has no
authentication: it prints this for every address other than loopback. It keeps its
sessions in `/data/session.db`. Mount a volume at `/data` to keep the sessions
when the container is recreated, for example `--volume belle-docker-agent-data:/data`.
Docker Agent's configuration and cache directories are in `/tmp`, and the
server writes large tool results there. With `--read-only`, Podman mounts a
tmpfs at `/tmp`. With a runtime that does not, add `--tmpfs /tmp`.

Belle's agent YAML is `/etc/docker-agent/belle.yaml`. To declare more models or
agents, mount a directory that holds your copy as `belle.yaml` at
`/etc/docker-agent`. See the [Docker Agent guide](../../docs/backends/docker-agent.md).

## Trust domains

The shipped agent has no shell or file tool. If you add such a toolset, a
conversation can read the server's environment, which holds the provider key
and the relay token. Run one Docker Agent server per trust domain, or deny those
tools in Belle's scopes.
