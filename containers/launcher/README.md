# Belle container

Build the Belle image from the repository root:

```sh
podman build -f containers/launcher/Containerfile -t localhost/belle:launcher-local .
```

The image includes the shared [example config](../../examples/config/belle-config.yaml)
and no credentials. One image runs beside either harness. It has no default
backend: the pairing file of the harness sets `BELLE_BACKEND`, its address and
its secrets. Belle stops at start when `BELLE_BACKEND` is not set.

| Harness | `BELLE_BACKEND` | Quick start |
| --- | --- | --- |
| OpenCode | `opencode` | [Belle + OpenCode](../opencode/README.md) |
| Docker Agent | `docker_agent` | [Belle + Docker Agent](../docker-agent/README.md) |

To change harness, change the pairing file and the harness container. For
configuration and persistent paths, see the [config guide](../../docs/config.md).

## The Matrix helper

The image holds `belle-matrix-helper` in `/usr/local/bin`, the process that
holds Belle's Matrix encryption. The build compiles it in a first stage,
from the crate in `src/belle_agent/platforms/matrix/helper`, with the
toolchain image that the Containerfile pins by digest and the crate
versions of `Cargo.lock`. The stage runs `cargo test`, then the release
build. The stripped binary adds about 54 MB to the image.

Two build caches keep the crate registry (`belle-cargo-registry`) and the
target directory (`belle-cargo-target`) between builds. The first build
downloads the crates and compiles the helper in about 4 minutes. A build
after a change of the crate takes about 1 minute. A change elsewhere
reuses the helper's layer. Builds from the same sources give the same
binary, byte for byte. See [Matrix](../../docs/platforms/matrix.md).
