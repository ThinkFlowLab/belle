# Belle + OpenCode quick start

## One command

[`compose.yaml`](compose.yaml) starts both containers on a private network.
`docker compose` takes the same commands as `podman compose`, which needs
`podman-compose` or `docker-compose` installed.

In an empty directory, put this `compose.yaml`, write `model.env` and
`belle.env` as the steps below give them, and a `.env` that names the
release, such as `v1.0.0`:

```dotenv
BELLE_VERSION=v<version>
```

```sh
podman compose up -d
podman compose logs -f belle
```

Compose pulls both images and builds nothing. The file sets the values of
`pair-opencode.env` itself, and reads no pairing file.
`OPENCODE_SERVER_PASSWORD` and `BELLE_RELAY_TOKEN` default to fixed trial values
that this repository publishes. No port is published, so only the two
containers reach each other. Before you publish a port or connect another
container to the network, add your own values to `.env`, and keep it mode
`0600`:

```sh
chmod 600 .env
printf 'OPENCODE_SERVER_PASSWORD=%s\nBELLE_RELAY_TOKEN=%s\n' "$(openssl rand -hex 32)" "$(openssl rand -hex 32)" >> .env
```

Named volumes keep the state: `opencode-home` on `/home/opencode`, and
`belle-state` and `belle-data` on Belle's two directories.
`podman compose down -v` removes them. On Mattermost, the server reaches
Belle at `http://belle:18400` once it is on the network
`belle-opencode_default`.

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

The images get the tags of the steps below, `localhost/belle:opencode-local` and
`localhost/belle:launcher-local`. Without `--build`, Compose uses those
images when they exist and builds them when they do not.

## Step by step

Build both images from the repository root. The Belle image includes the
shared Belle example config; the OpenCode image includes its provider config
and Belle relay plugin. Neither image contains credentials.

```sh
podman build -f containers/opencode/Containerfile -t localhost/belle:opencode-local .
podman build -f containers/launcher/Containerfile -t localhost/belle:launcher-local .
```

The OpenCode build accepts `OPENCODE_VERSION`, and for each architecture
`OPENCODE_TARGET_AMD64` with `OPENCODE_SHA512_AMD64`, and `OPENCODE_TARGET_ARM64`
with `OPENCODE_SHA512_ARM64`. Change the checksums with the version or a target.

Create three files with mode `0600`. Use literal values: Podman does not expand
`${NAME}` inside `--env-file`.

`model.env` (the harness container only):

```dotenv
API_BASE=https://openrouter.ai/api/v1
API_KEY=replace-me
MODEL_NAME=replace-me
```

`pair-opencode.env` (both containers):

```dotenv
BELLE_BACKEND=opencode
OPENCODE_URL=http://belle-opencode:4096
OPENCODE_SERVER_PASSWORD=replace-with-a-random-secret
BELLE_TOOLS_URL=http://belle:18300/mcp
BELLE_RELAY_URL=http://belle:18200
BELLE_RELAY_TOKEN=replace-with-another-random-secret
```

`belle.env` (Belle only):

```dotenv
SLACK_BOT_TOKEN=xoxb-replace-me
SLACK_APP_TOKEN=xapp-replace-me
# Optional decision model:
# DECISION_API_BASE=https://openrouter.ai/api/v1
# DECISION_API_KEY=replace-me
# DECISION_MODEL_NAME=replace-me
```

For Mattermost, `belle.env` holds the Mattermost variables instead. See
[the Mattermost guide](../../docs/platforms/mattermost.md#quick-start-with-containers).
For Matrix, it holds the Matrix variables, and Belle needs its state and
data volumes. The first start of a Matrix account writes its recovery key
and stops. See
[the Matrix guide](../../docs/platforms/matrix.md#quick-start-with-containers).

Start OpenCode before Belle. Use a Slack app token that no other running
connector uses.

```sh
podman network create belle-net
podman run -d --name belle-opencode --network belle-net \
  --env-file model.env --env-file pair-opencode.env \
  localhost/belle:opencode-local
podman run -d --name belle --network belle-net \
  --env-file pair-opencode.env --env-file belle.env \
  localhost/belle:launcher-local
podman logs belle
```

The launcher image has no default backend. A pairing file without
`BELLE_BACKEND` stops Belle with "Belle config backend must name a backend". For
such a file, for example `pair.env`, add the line and recreate the Belle
container:

```sh
echo 'BELLE_BACKEND=opencode' >> pair.env
podman rm -f belle
podman run -d --name belle --network belle-net \
  --env-file pair.env --env-file belle.env \
  localhost/belle:launcher-local
```

The [Docker Agent guide](../docker-agent/README.md) uses the same launcher
image and the same `model.env`. To change harness, remove both containers.
Then start the other harness container and the launcher container with that
harness's pairing file.

Belle's bundled config answers direct messages and uses ambient participation
in channels. It denies OpenCode's own file, shell, web and subagent tools: see
[Tool access](../../docs/backends/opencode.md#tool-access) before you allow them.
To change its scopes or other settings, mount your config file at
`/home/belle/.config/belle-agent/config.yaml`. See
[the config guide](../../docs/config.md) for its keys.

The commands above keep state inside disposable containers. For a persistent
deployment, mount writable volumes at `/home/opencode`,
`/home/belle/.local/share/belle-agent`, and
`/home/belle/.local/state/belle-agent`. A mounted config directory hides the
baked config, so supply a `config.yaml` in it. OpenCode does not watch files in
this image; restart it after changing mounted plugins, skills, or config.
Recreate containers after changing an env file; `podman restart` does not
reload it.
