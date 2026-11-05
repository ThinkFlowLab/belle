# Container images

## Quick start

The OpenCode and Docker Agent harnesses each have a `compose.yaml` that starts
Belle beside its harness on a private network. It works with `podman compose`
and with `docker compose`. `podman compose` needs a provider:
`podman-compose` or `docker-compose`. JiuwenSwarm runs in one container and
needs no compose file.

> [!NOTE]
> This repository publishes no images. [Build them from a clone](#build-from-a-clone)
> and set `BELLE_REGISTRY=localhost` and `BELLE_VERSION=local`, or push them to
> your own registry and name it below.

In an empty directory, put the harness's `compose.yaml`, then write
`model.env` and `belle.env` as the harness guide gives them, and a `.env`
that names the images:

```dotenv
BELLE_REGISTRY=<registry>/<namespace>
BELLE_VERSION=v<version>
```

```sh
podman compose up -d
podman compose logs -f belle
```

Compose pulls `<registry>/<namespace>/belle:<role>-v<version>` for each
container and builds nothing. Compose reads `.env` at each command, and
stops with a message that names `BELLE_REGISTRY` or `BELLE_VERSION` when
one is not set.

The file needs no pairing file: it sets the pairing values itself. Its two
secrets default to trial values that this repository publishes, so anyone
knows them. The file publishes no port, so only the two containers can use
them, and both hold them in their environment. Before you publish a port or
add a container to that network, set your own values in `.env`. The harness
guide names the two variables.

Named volumes keep the state of both containers. `podman compose down` keeps
them, and `podman compose down -v` removes them. `belle.env` holds the
variables of the platform, as its guide gives them. On Matrix the
`belle-state` volume holds the encryption store, and the first start of the
account writes the recovery key and stops. See
[Matrix](../docs/platforms/matrix.md#quick-start-with-containers). Never
remove the `belle-state` volume of a Matrix deployment without its recovery
key.

### Build from a clone

In a clone of this repository, the same file builds the images. Write the
env files in the harness's directory, `containers/opencode` or
`containers/docker-agent`, and set the tags of a local build in `.env`:

```dotenv
BELLE_REGISTRY=localhost
BELLE_VERSION=local
```

```sh
cd containers/opencode        # or containers/docker-agent
podman compose up -d --build
```

`--build` builds both images from the clone, as `localhost/belle:<role>-local`,
the tags of the step-by-step guides. Without it, Compose uses a local image of
that tag when one exists, and builds when none does. A `compose.yaml` outside
a clone cannot build: it pulls only.

The step-by-step commands of each guide start the same containers one by one.

## Images

Build from the repository root. Each directory has a Containerfile and a usage guide.

| Role | Directory | Use |
| --- | --- | --- |
| `launcher` | [launcher](launcher/README.md) | Run Belle beside a separate harness. One image serves each harness. |
| `opencode` | [opencode](opencode/README.md) | Run OpenCode with Belle's relay plugin. |
| `docker-agent` | [docker-agent](docker-agent/README.md) | Run Docker Agent with Belle's agent YAML. |
| `jiuwenswarm` | [jiuwenswarm](jiuwenswarm/README.md) | Run Belle and JiuwenSwarm in one container. |
| `slack-helper` | [slack-helper](slack-helper/README.md) | Set up a Slack app with the Slack CLI. |
| `mattermost-helper` | [mattermost-helper](mattermost-helper/README.md) | Set up Belle's bot, its token and the `/belle` command on a Mattermost server. |
| `tui` | [tui](tui/Containerfile) | View Belle status through its local socket; see the [TUI guide](../docs/tui.md). |
| `test` | [test](test/README.md) | Run Belle's tests, alone or beside a harness container. |

The `launcher` image runs beside `opencode` or `docker-agent`. Each harness guide
gives a pairing file for both containers and a `model.env` for the harness. The
`model.env` is the same for both harnesses. To change harness, change the
pairing file and the harness container.

Use one image repository with a role-prefixed tag:

```text
<registry>/<namespace>/belle:<role>-v<version>
```

For a local build, use `localhost/belle:<role>-local`. Published images from one tested set share a version, such as `launcher-v1.0.0` and `opencode-v1.0.0`. Keep published tags immutable.

For a release, set the package version in `pyproject.toml`, then run
`python scripts/sync_version.py`. The `--tag` option prints its image-tag
suffix, such as `v1.0.0`. Set `BELLE_VERSION` to that suffix when building or
running the published images. Image tags are assigned outside the package.

Each runtime image includes Belle's license under
`/usr/share/licenses/belle-agent/`. Images that bundle another executable
include its license and notices under `/usr/share/licenses/`. Installed Python
packages retain their `.dist-info` directories, including any license files
supplied by those packages.
The Matrix helper also includes `THIRD_PARTY.tsv` and packaged Rust license
files. For crates without packaged license files, the image retains
`Cargo.toml` and available README files.
