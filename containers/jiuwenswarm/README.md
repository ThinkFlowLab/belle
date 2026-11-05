# Bundled Belle + JiuwenSwarm

This image runs JiuwenSwarm with the selected Agent Core build and Belle's
JiuwenSwarm extensions. It builds the Web UI, then installs all three Python
packages into one environment, with Belle's `slack`, `mattermost`, and
`matrix` extras. It builds `belle-matrix-helper` on Debian Bookworm, which
matches the Python runtime's glibc. It includes Belle's example config and a
JiuwenSwarm config that turns on the Slack, Mattermost, and Matrix channels,
without credentials. Each channel starts when its settings are present.
JiuwenSwarm fills the remaining config fields from its template at startup.
The config also permits Belle's Slack extension to replace the built-in
channel.
The default command starts JiuwenSwarm's four services in one container.

## Build

Build from this repository's root:

```sh
podman build -f containers/jiuwenswarm/Containerfile \
  --ignorefile containers/jiuwenswarm/.containerignore \
  -t localhost/belle:jiuwenswarm-local .
```

Use these `--build-arg` names to select the JiuwenSwarm and Agent Core sources:

- `JIUWENSWARM_REPO`
- `JIUWENSWARM_REF`
- `AGENT_CORE_REPO`
- `AGENT_CORE_REF`

## Check without Slack

Check the image you tagged above:

```sh
podman run --rm --network none --read-only \
  --entrypoint python \
  localhost/belle:jiuwenswarm-local \
  -c 'import jiuwenswarm, openjiuwen, belle_agent; print("imports ok")'
```

## Run with the bundled configs

Create `belle.env` with permissions `0600` and your model and Slack tokens.
For Mattermost, see [Run with Mattermost](#run-with-mattermost).
Uncomment the decision-model lines if you use one:

```dotenv
# Chat model
API_BASE=https://openrouter.ai/api/v1
API_KEY=replace-me
MODEL_NAME=replace-me
MODEL_PROVIDER=OpenAI
ENDPOINT_PROFILE=openrouter
# Decision model (optional)
# DECISION_API_BASE=https://openrouter.ai/api/v1
# DECISION_API_KEY=replace-me
# DECISION_MODEL_NAME=replace-me
# Slack
SLACK_BOT_TOKEN=xoxb-replace-me
SLACK_APP_TOKEN=xapp-replace-me
```

Use literal values in this file. Podman does not expand `${NAME}` references
inside `--env-file`. The bundled YAML expands its references from the resulting
container environment. Start the container:

```sh
podman run -d --name belle --env-file belle.env localhost/belle:jiuwenswarm-local
```

The image includes an empty JiuwenSwarm `.env`, so its template credentials
cannot replace values passed by Podman. The entrypoint recreates the empty
`.env` and default JiuwenSwarm config if a mounted workspace hides them.
Removing this container also removes its unmounted sessions and other state.

## Persistent state and configuration

Recommended directories to persist:

| Environment variable | Default container path | Purpose |
| --- | --- | --- |
| `JIUWENSWARM_DATA_DIR` | `/home/app/.jiuwenswarm` | JiuwenSwarm config, sessions, cron jobs, and extension links. |
| `JIUWENSWARM_TASKS_DIR` | `/home/app/Documents/JiuwenSwarm` | Projectless task files. |
| `BELLE_AGENT_CONFIG_DIR` | `/home/app/.config/belle-agent` | Baked Belle `config.yaml`, or a mounted replacement. |
| `BELLE_AGENT_DATA_DIR` | `/home/app/.local/share/belle-agent` | Second Brain corpus and other durable Belle data. |
| `BELLE_AGENT_STATE_DIR` | `/home/app/.local/state/belle-agent` | Matrix encryption stores and Belle runtime state. Persist it separately from the recovery-key directory. |

Create the host directories:

```sh
mkdir -p "$HOME/belle-jiuwenswarm/jiuwenswarm/config" \
  "$HOME/belle-jiuwenswarm/tasks" \
  "$HOME/belle-jiuwenswarm/belle-config" \
  "$HOME/belle-jiuwenswarm/belle-data" \
  "$HOME/belle-jiuwenswarm/belle-state"
```

Put JiuwenSwarm's `config.yaml` in `~/belle-jiuwenswarm/jiuwenswarm/config/`
and Belle's `config.yaml` in `~/belle-jiuwenswarm/belle-config/`. The mounts
hide the image's files at those paths. An empty Belle config mount will not
fall back to the baked file. A custom JiuwenSwarm config must set
`channels.slack.enabled: true` and list `channels.slack` under
`extensions.allow_overrides`. For Mattermost, it must also turn on
`channels.mattermost`, as the image's config does. A config that an older
image wrote has no `channels.mattermost`. For Matrix, set
`channels.matrix.enabled` and persist Belle's state directory. Set
`MATRIX_*_FILE` variables for Matrix secrets because JiuwenSwarm's shell tool
inherits environment values.

The run below reuses the same `belle.env`. Set `DECISION_API_BASE`,
`DECISION_API_KEY`, and `DECISION_MODEL_NAME` there only if using a decision
model.

> [!WARNING]
> JiuwenSwarm creates `jiuwenswarm/config/.env` for port settings. If it
> contains credential keys, they override values from Podman's `--env-file`.
> Remove those duplicate keys before starting the container.

Once the Slack app and config files are ready, run:

```sh
podman run -d --name belle \
  --userns=keep-id --user "$(id -u):$(id -g)" \
  --env-file "$HOME/belle-jiuwenswarm/belle.env" \
  --volume "$HOME/belle-jiuwenswarm/jiuwenswarm:/home/app/.jiuwenswarm:Z" \
  --volume "$HOME/belle-jiuwenswarm/tasks:/home/app/Documents/JiuwenSwarm:Z" \
  --volume "$HOME/belle-jiuwenswarm/belle-config:/home/app/.config/belle-agent:ro,Z" \
  --volume "$HOME/belle-jiuwenswarm/belle-data:/home/app/.local/share/belle-agent:Z" \
  --volume "$HOME/belle-jiuwenswarm/belle-state:/home/app/.local/state/belle-agent:Z" \
  localhost/belle:jiuwenswarm-local
```

> [!WARNING]
> Recreate the container after changing the host `.env`. A restart does not
> reload `--env-file`.

## Run with Mattermost

The image serves Mattermost with no config change. Its JiuwenSwarm config
sets `channels.mattermost.enabled: ${MATTERMOST_URL}`, so the channel is on
when the container has `MATTERMOST_URL`. Belle enables its
`platforms.mattermost` section when the container has both
`MATTERMOST_URL` and `MATTERMOST_BOT_TOKEN`. Without `MATTERMOST_URL`, the Gateway logs
`channels.mattermost disabled`, and Slack runs as before.

First set up the bot, its token and the `/belle` command, as
[Set up on an existing Mattermost server](../../docs/platforms/mattermost.md#set-up-on-an-existing-mattermost-server)
says. Give the setup `--listener http://belle:18400`: the name of this
container, and the port of Belle's action listener. The setup writes
`out/mattermost.env`. Where the server runs in a container on this host, run
the setup with `--network belle-net` too, after the network commands below.

Write `belle.env` as above, with the model lines and without the Slack lines
if Slack is not used. Then start the container on a network that the server
reaches. Where the server runs in a container on the same host, connect it
to that network first, as the commented line shows:

```sh
podman network create belle-net
# podman network connect belle-net <server container>
podman run -d --name belle --network belle-net \
  --env-file belle.env --env-file out/mattermost.env \
  localhost/belle:jiuwenswarm-local
podman logs -f belle
```

The Gateway runs Belle's action listener on port 18400. The server sends
button clicks and `/belle` to it, at `MATTERMOST_ACTIONS_URL`. On a shared
network, `http://belle:18400` is that address. The server calls a private
address only where its `AllowedUntrustedInternalConnections` setting names
the host, here `belle`: see
[The server's reach to Belle's listener](../../docs/platforms/mattermost.md#4-the-servers-reach-to-belles-listener).

Where the server runs on this host outside a container, publish the port on
the loopback address only, with `-p 127.0.0.1:18400:18400`, give the setup
`--listener http://127.0.0.1:18400`, and name `127.0.0.1` in the server's
`AllowedUntrustedInternalConnections`.

The log shows `[App] channel mattermost registered by belle-agent` when the
channel starts. Send the bot a direct message, or mention it in a channel
where it is a member. [Mattermost channel](../../docs/integrations/jiuwenswarm.md#mattermost-channel-experimental)
lists what the channel does not do on JiuwenSwarm.

For the persistent run above, add `--network belle-net` and
`--env-file out/mattermost.env`. A custom Belle config needs the
`platforms.mattermost` section of the example config.

## Run with Matrix

The image links Belle's Matrix channel extension. Set `MATRIX_ENABLED=true`
to turn it on, including when `platforms.matrix.accounts` lists several
accounts. Belle's `platforms.matrix` settings stay in
`belle-config/config.yaml`. The image installs the helper on `PATH` as
`belle-matrix-helper`.

Persist `/home/app/.local/state/belle-agent` for the encrypted Matrix store.
Keep the recovery-key output on a separate mount, outside the store directory.
Set secret paths such as `MATRIX_STORE_PASSPHRASE_FILE` and
`MATRIX_PASSWORD_FILE` in `belle.env`, then mount those files read-only at the
named container paths. Use `MATRIX_RECOVERY_KEY_FILE` to recover an existing
device. See [Matrix settings](../../docs/config.md#matrix-experimental) and the
[Matrix setup guide](../../docs/platforms/matrix.md).
