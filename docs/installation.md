# Install Belle Agent

## Package

From this repository:

```sh
pip install .           # Belle's launcher and Second Brain
pip install '.[slack]'   # Adds Socket Mode and the packaged Slack skill dependencies
pip install '.[mattermost]'   # Adds the Mattermost WebSocket client (aiohttp)
pip install '.[discord]'   # Adds the Discord gateway client (aiohttp)
pip install '.[matrix]'    # Adds nothing; Matrix also needs `belle-matrix-helper`, see docs/platforms/matrix.md
pip install '.[acp]'      # Adds the Agent Client Protocol SDK, for `backend: acp`
pip install '.[tui]'      # Adds the operator TUI, `belle-tui`
pip install '.[dev]'      # Adds the test, lint and type-check tools
pip install '.[mcp]'      # Serves Belle tools to HTTP MCP clients
pip install '.[openjiuwen]'  # Adds the OpenJiuwen host integration
```

Belle's launcher runs the Second Brain without OpenJiuwen. The `openjiuwen`
extra is for an OpenJiuwen host or JiuwenSwarm integration. Set
`second_brain.enabled: false` to disable the feature.
The OpenCode and Docker Agent backends need the `mcp` extra. The launcher image
installs it. The JiuwenSwarm extension uses its host's dependencies instead.

Belle is a library and JiuwenSwarm extension package. Under JiuwenSwarm it does not start its own server.
On an OpenCode or a Docker Agent server it starts with
`python -m belle_agent.launcher`. See the [OpenCode backend](backends/opencode.md)
and the [Docker Agent backend](backends/docker-agent.md).
For a full Slack agent, install the matching
[JiuwenSwarm and agent-core `belle/20261013` branches](integrations/jiuwenswarm.md#build-branches),
then follow [Use Belle with JiuwenSwarm](integrations/jiuwenswarm.md).

## What the package provides

| Area | Module | Role |
| --- | --- | --- |
| Turn progress | `belle_agent.progress` | Tool, subagent, reasoning, and compression state for platform renderers |
| Tool workflows | `belle_agent.tools.workflow` | Numeric limits and partial results before a deadline |
| Structured text | `belle_agent.structured_text` | Bounded traversal and deduplication of platform-selected fields |
| Slack text and blocks | `belle_agent.platforms.slack` | Normalization, rich text, Block Kit, question inputs |
| Audience and workspaces | `belle_agent.platforms.slack.history_policy` | Audience checks, metadata keys, `select_slack_workspace` |
| History and post | `belle_agent.platforms.slack.history`, `.post` | Reading and writing. The host supplies workspace settings and a session download directory |
| OpenJiuwen tools | `belle_agent.integrations.openjiuwen.tools` | Register with an OpenJiuwen host |
| OpenJiuwen plugin | `belle_agent.integrations.openjiuwen.agent_plugin` | [Mount packaged skills, rails, and tools](integrations/openjiuwen-plugin.md) |
| JiuwenSwarm extensions | `belle_agent.integrations.jiuwenswarm.extensions` | Agent plugin, Slack channel override, Mattermost and Matrix channels, second-brain gateway adapter |
| OpenCode backend | `belle_agent.integrations.opencode` | Runs turns on an OpenCode v2 server; Belle handles Second Brain parsing and model calls |

File downloads use `httpx` at runtime.

## Configuration

See [Belle configuration](config.md) for `load_config(path)` and the YAML file.
Select that file with `BELLE_AGENT_CONFIG_FILE` in the process environment.

## Slack app manifests

Tiered manifests ship under `belle_agent/platforms/slack/manifests/` (and in the source tree at `src/belle_agent/platforms/slack/manifests/`).
Source maintainers can check them with `python3 scripts/generate_slack_manifest.py --check`.
Creating the Slack app and collecting tokens is part of [JiuwenSwarm setup](integrations/jiuwenswarm.md#1-slack-app) and the [Slack connector guide](platforms/slack.md).
