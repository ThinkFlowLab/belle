# Test image

This image adds Belle's tests and the test tools to a launcher image. Do not
deploy it. Run each command from the repository root. The image, container,
network and volume names are examples.

## Build

```sh
podman build -f containers/launcher/Containerfile -t localhost/belle:launcher-local .
podman build -f containers/test/Containerfile \
  --build-arg LAUNCHER_IMAGE=localhost/belle:launcher-local \
  -t localhost/belle:test-local .
```

### With JiuwenSwarm

The JiuwenSwarm tests need the `jiuwenswarm` package. Build the
[JiuwenSwarm image](../jiuwenswarm/README.md#build), at the JiuwenSwarm and
Agent Core refs that it pins, and add the tests to it. Its user is `app`.
JiuwenSwarm pins MCP 1.x, so the test extras leave out `mcp`:

```sh
podman build -f containers/jiuwenswarm/Containerfile \
  --ignorefile containers/jiuwenswarm/.containerignore \
  -t localhost/belle:jiuwenswarm-local .
podman build -f containers/test/Containerfile \
  --build-arg LAUNCHER_IMAGE=localhost/belle:jiuwenswarm-local --build-arg TEST_USER=app \
  --build-arg TEST_EXTRAS=dev,slack,matrix,tui,discord,acp \
  -t localhost/belle:test-jiuwenswarm-local .
```

## Upgrade a rig

Belle refuses a `sessions.json` of an earlier store version, and the host does not start. To upgrade a rig, delete its state volume. The harness conversations of the old store go with the harness container's volume.

## Unit suites

The suites run with no network. The image holds the tasks of
[the testing guide](../../docs/testing.md). Give the task after the image:

```sh
podman run --rm --network none localhost/belle:test-local poe tests
podman run --rm --network none localhost/belle:test-local poe test-docker-agent
podman run --rm --network none localhost/belle:test-local poe test-opencode
```

The checks that need a harness container skip. `poe test-all` runs every
suite. The launcher test image has neither JiuwenSwarm nor OpenJiuwen, so
tests that require either host skip. The JiuwenSwarm test image runs those
tests. JiuwenSwarm pins MCP 1.x, so that image has no `mcp` extra, and the
tests that start Belle's tool server skip there:

```sh
podman run --rm --network none localhost/belle:test-jiuwenswarm-local poe test-jiuwenswarm
podman run --rm --network none localhost/belle:test-jiuwenswarm-local poe test-all
```

## Integration checks

The test process serves a stand-in model on port 14099, Belle's relay endpoint
on port 18200 and Belle's tool server on port 18300. The harness container
reaches them by the name of the test container. The checks use no provider key.
Both containers are on an internal network:

```sh
podman network create --internal test-net
```

### Docker Agent

The checks use Belle's agent YAML with a second model, an expert and a
subagent. Write it into a volume, then start Docker Agent with it:

```sh
podman build -f containers/docker-agent/Containerfile \
  --ignorefile containers/docker-agent/.containerignore \
  -t localhost/belle:docker-agent-local .
podman run --rm --network none --volume test-agent-yaml:/out:U \
  localhost/belle:test-local python tests/docker_agent/agent_yaml.py /out/belle.yaml
export DOCKER_AGENT_AUTH_TOKEN=$(openssl rand -hex 16) BELLE_RELAY_TOKEN=$(openssl rand -hex 16)
podman run -d --name test-agent --network test-net \
  --volume test-agent-yaml:/etc/docker-agent:ro \
  --env DOCKER_AGENT_AUTH_TOKEN --env BELLE_RELAY_TOKEN \
  --env BELLE_RELAY_URL=http://test-belle:18200 \
  --env BELLE_TOOLS_URL=http://test-belle:18300/mcp \
  --env API_BASE=http://test-belle:14099/v1 --env API_KEY=standin --env MODEL_NAME=standin \
  localhost/belle:docker-agent-local
podman run --rm --name test-belle --network test-net --tmpfs /state \
  --env DOCKER_AGENT_AUTH_TOKEN --env BELLE_RELAY_TOKEN \
  --env DAB_AGENT_URL=http://test-agent:8080 --env DAB_PEER=test-belle --env OCB_TEST_TIMEOUT=120 \
  localhost/belle:test-local poe test-docker-agent-server
podman rm -f test-agent
podman volume rm test-agent-yaml
```

### OpenCode

Build the image with the relay plugin and the plain image:

```sh
podman build -f containers/opencode/Containerfile -t localhost/belle:opencode-local .
podman build -f containers/opencode/Containerfile --target plain \
  -t localhost/belle:opencode-plain-local .
```

The checks run in three passes. Each pass uses a new OpenCode container:

| Pass | OpenCode image | Add to the test container |
| --- | --- | --- |
| Relay plugin, stand-in relay endpoint | `opencode-local` | nothing |
| No plugin | `opencode-plain-local` | `--env OCB_PLAIN=1` |
| Relay plugin, Belle's relay endpoint | `opencode-local` | `--env OCB_PLUGIN=1` |

The first pass:

```sh
export OPENCODE_SERVER_PASSWORD=$(openssl rand -hex 16) BELLE_RELAY_TOKEN=$(openssl rand -hex 16)
podman run -d --name test-opencode --network test-net \
  --env OPENCODE_SERVER_PASSWORD --env BELLE_RELAY_TOKEN \
  --env BELLE_RELAY_URL=http://test-belle:18200 \
  --env API_BASE=http://test-belle:14099/v1 --env API_KEY=standin --env MODEL_NAME=standin/scripted \
  --env OPENCODE_CONFIG_CONTENT='{"provider":{"configured":{"models":{"standin/second":{}}}},"experimental":{"todo":true}}' \
  localhost/belle:opencode-local
podman run --rm --name test-belle --network test-net --tmpfs /state \
  --env OPENCODE_SERVER_PASSWORD --env BELLE_RELAY_TOKEN \
  --env OCB_OPENCODE_URL=http://test-opencode:4096 --env OCB_PEER=test-belle \
  --env OCB_STATE=/state --env OCB_TEST_TIMEOUT=120 \
  localhost/belle:test-local python -m pytest -q tests/opencode tests/conformance
podman rm -f test-opencode
```

After the last check, remove the network:

```sh
podman network rm test-net
```

## Variables

The tests read these variables. `tests/conftest.py` skips each check whose
variables are not set.

| Variable | Meaning |
| --- | --- |
| `OCB_TEST_TIMEOUT` | The seconds one test can take. Unset, no test has a time limit. |
| `DAB_AGENT_URL` | The Docker Agent server. Set, the checks that need it run. |
| `DOCKER_AGENT_AUTH_TOKEN` | The bearer token of that server. |
| `DAB_PEER` | The name of the test container. Docker Agent reaches Belle's tool server there. Default `belle`. `BELLE_TOOLS_URL` replaces the whole URL. |
| `OCB_OPENCODE_URL` | The OpenCode server. Set, the OpenCode checks run. |
| `OPENCODE_SERVER_PASSWORD` | The password of that server. |
| `BELLE_RELAY_TOKEN` | The relay token. Use the value of the harness container. |
| `OCB_PEER` | The name of the test container. OpenCode reaches Belle's tool server there. |
| `OCB_PLAIN`, `OCB_PLUGIN` | `1` selects the second or the third OpenCode pass. |
| `OCB_STATE`, `DAB_STATE` | The directory of the test artefacts. Default `/state`. |
| `OCB_COLDSTART` | `1` runs `tests/opencode/test_cold_start.py`. Use a new OpenCode server, and run nothing else against it first. |
| `OCB_PROVIDER` | `1` runs the provider checks. OpenCode then gets the provider in its `model.env` and a network with a route out. |
| `OCB_PROXY` | The recording proxy, `tests/opencode/recording_proxy.py`, between OpenCode and the provider. Set, the provider validation runs. |
| `OCB_STAGE`, `OCB_RUN`, `DAB_RUN` | The stage and the run of the restart checks. Between two stages, the runner restarts a container or kills one. |
