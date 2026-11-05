# AGENTS.md

Belle Agent provides reusable agent behavior, platform support, and optional
host integrations. Keep those parts separate so Belle can run with another
messaging platform or agent harness.

## Where code belongs

- `src/belle_agent/` contains shared configuration, policy, and services that
  need no platform or host SDK.
- `src/belle_agent/platforms/<platform>/` handles platform APIs, payloads,
  and rendering. Keep reusable behavior in shared code.
- A platform may hold a native helper crate in
  `src/belle_agent/platforms/<platform>/helper/`. It ships in the launcher
  image only. The platform refuses to start without its helper, and the error
  names the setting of the helper path.
- `src/belle_agent/integrations/<host>/` registers Belle with a host and
  translates host objects and lifecycle events.
- `src/belle_agent/skills/` is the packaged skill source. Root `skills/` links
  to it. `contrib/` holds disposable probes and examples.
- `tests/`, `docs/`, `examples/`, and `scripts/` hold checks, operator guides,
  sample configuration, and generated-asset tooling.

## Implementation boundaries

- Keep `integrations/` and `platforms/` as thin as their external APIs allow.
  Convert external inputs to plain values, call shared code, then convert the
  result back. Platform-specific protocol handling stays in `platforms/`.
- Shared code must not import a platform or host integration. Platform code
  must not import a host integration. Put any required host-specific bridge
  under `integrations/<host>/`.
- Keep optional host dependencies inside their integrations. Shared modules
  must import without an optional host installed.
- Use a host's public API or extension contract where possible. Keep any
  unavoidable private host import in that host's integration.
- Keep Belle-owned settings in Belle's YAML config. Host-owned settings stay
  with the host. Update `examples/config/belle-config.yaml` and `docs/config.md`
  when Belle configuration changes. Do not maintain duplicate keys.

## Contracts and inputs

- Describe each interface truthfully, and do what it says. A schema, a
  docstring, a default and an error code match what the code does. A
  parameter that the code treats as optional is optional in its schema. Code
  that cannot do what it advertises refuses. It does not truncate in secret or
  return a partial result as complete.
- Check each input where it enters, in one shared place, before any other
  check.
- Refuse an input that does not fit with a precise error. The error names the
  field and the form that it expects, so that the caller can correct the
  input. Do not guess, coerce, rewrite or silently drop a value.
- When a caller misuses an interface, first check whether the interface
  misleads it. If it does, correct the interface.
- Write code for the general case. Do not add a special case for one model,
  harness, platform version or deployment. When an external system is wrong,
  record the fault and report it upstream. Do not patch around it in Belle.
- Expose each capability fully. An option that nobody uses yet stays
  available.
- Give each restriction a hard reason, and write the reason beside the
  restriction. These are the hard reasons:
  - It protects a privilege, or the security of a user or a deployment.
  - It prevents harm that cannot be undone.
  - The input has more than one meaning.
  - An external system sets the limit: a platform, an API, or the identity
    data that Belle has.
  - The operator sets the limit as a resource limit in the configuration.

  Do not add a maximum, omit an option or refuse a call without a hard
  reason. Do not set a maximum only to keep a result small. Refuse a
  result that is too large, and name its size and the argument to change.
- Do not run a possibly destructive action outside the turn that asked for
  it. An approval that arrives after its turn or its timeout runs nothing.

## Making changes

- Target the Python version declared in `pyproject.toml`. Reuse existing
  helpers, keep comments and docstrings brief, and test behavior rather than
  restating the implementation.
- Add focused tests in `tests/` at the boundary you changed.
- Regenerate committed assets with their owning script. Check the output
  before committing. Do not edit generated files by hand.
- After changing the package version in `pyproject.toml`, run
  `python scripts/sync_version.py` to update the extension and Matrix helper
  version files.
- Update the relevant guide under `docs/` when behavior or operator setup
  changes. Keep deployment-specific paths, credentials, and internal findings
  out of this public repository.

## Testing in containers

- Build `containers/test/Containerfile` and run tests in rootless Podman
  containers. Start separate containers when checks can run independently.
- Check the test image's build time before use. Rebuild it after relevant
  source, dependency, or test changes.
- Use `--network none` when a test needs no network. For integration tests,
  create a dedicated network and use unique container names and ports.
- Do not mount host configuration, state, cache, or other standard application
  paths into test containers. Use temporary files or dedicated test volumes.
- Give each test container its own writable volumes. Share fixtures only as
  read-only mounts.
- Record the containers, networks, volumes, processes, and worktrees that you
  create. Remove only your own resources after the task ends.
