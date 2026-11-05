# Tests

Install the development dependencies with `pip install '.[dev]'`, then choose a suite:

| Command | Tests | Requires |
| --- | --- | --- |
| `poe tests` | Core, platform, and scripted conformance | Belle development dependencies |
| `poe test-opencode` | OpenCode, launcher, Slack command, and configuration checks | Belle development dependencies. Live checks also need the test containers |
| `poe test-opencode-conformance` | OpenCode conformance cases | OpenCode test container. Without it, the cases skip |
| `poe test-docker-agent` | Docker Agent and configuration checks | Belle development dependencies. Live checks also need the test containers |
| `poe test-docker-agent-server` | Docker Agent adapter and conformance cases | Docker Agent test container. Without it, the cases skip |
| `poe test-mattermost` | Mattermost connector on a fake server | Belle development dependencies and the `mattermost` extra |
| `poe test-mattermost-server` | Mattermost connector against a server | A Mattermost test server, named by `MATTERMOST_TEST_URL` and `MATTERMOST_TEST_ADMIN_TOKEN`. Without them, the cases skip |
| `poe test-matrix` | Matrix platform and its helper protocol, on a stand-in helper process | Belle development dependencies. The helper's own tests run with `cargo test` in its build container |
| `poe test-matrix-server` | Matrix platform and the helper against a server: an encrypted direct message answered encrypted | A Matrix server that takes a registration token, named by `MATRIX_TEST_HOMESERVER` and `MATRIX_TEST_REGISTRATION`, and the helper binary, named by `MATRIX_TEST_HELPER`. Each case registers its own accounts. Without them, the cases skip |
| `poe test-jiuwenswarm` | JiuwenSwarm integration and Slack channel | JiuwenSwarm installed alongside Belle, as in the JiuwenSwarm test image. Without it, the cases skip |
| `poe test-all` | Every suite | Every backend and its test requirements |

The default task ignores optional backend directories and deselects OpenCode and Docker Agent cases in shared modules. OpenCode and Docker Agent tests that need a running container skip when its test environment is absent. The JiuwenSwarm tests skip when the `jiuwenswarm` package is absent. The tests that start Belle's tool server skip when the `mcp` extra is absent, as in the JiuwenSwarm test image. The [test image guide](../containers/test/README.md) builds the JiuwenSwarm test image and starts the harness containers.
Running `pytest` directly collects every suite.
The `tests/scheduling/` suite runs in `poe tests`.
`poe types` runs mypy on `src/`. The test directories are not packages, and several of them hold a module of the same name, such as `conftest.py`. mypy refuses such a tree, so it does not check the tests.

A rig with a `sessions.json` of an earlier store version does not start: the host refuses the store. Reset the rig's state volume, as the [test image guide](../containers/test/README.md#upgrade-a-rig) says.
