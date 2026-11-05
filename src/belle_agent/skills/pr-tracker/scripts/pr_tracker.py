#!/usr/bin/env python3
"""Track pull requests and issues posted to Slack, then report changes.

Commands register or remove items, watch authors, refresh and report tracked items, advance the report watermark, query the store, cache translated titles, and audit Slack registration metadata. Registration and reporting share URL identity, store format, and locking. Registration never writes the runs sidecar; only reporting owns the watermark.

The script has no Slack credential and schedules no work. Tracker requests are read-only GETs. It never comments, labels, closes, approves, merges, or pushes items."""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import contextlib
import hashlib
import json
import os
import re
import shlex
import shutil
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

SKILL_DIR = Path(__file__).resolve().parent.parent
SKILL_NAME = "pr-tracker"
SCHEMA_VERSION = 1
USER_AGENT = "pr-tracker/1.0"
GITHUB_API_VERSION = "2022-11-28"

# Re-scan raw messages instead of trusting model-reconstructed links. The
# excluded characters also unwrap Slack's <url|label> links.
HTTP_URL_RE = re.compile(r"https?://[^\s<>()|]+")
TRAILING_PUNCTUATION = ".,;:!?'\"`*"

ATTENTION_STATUSES = {"ci_failed", "cla_pending"}
DEFAULT_RESURFACE_RUNS = 7

# Stored route keys are stable. Report labels explain them to readers.
REGISTRATION_ROUTES = {
    "auto_link": "link posted",
    "author_watch": "author watch",
    "operator": "asked for",
    "reconcile": "history scan",
}

# A host-visible message boundary used throughout the report.
THREAD_BOUNDARY = "<!-- belle:slack-thread-details -->"

# Bound author searches to avoid truncated results and search API rate limits.
DEFAULT_WATCH_WINDOW_DAYS = 7
DEFAULT_WATCH_MAX_PER_AUTHOR = 100
WATCH_PAGE_SIZE = 100
WATCH_KINDS = ("pull_request", "issue")
WATCH_STATES = ("all", "open")
# GitHub Search returns at most 1000 results per query.
GITHUB_SEARCH_RESULT_CAP = 1000
# Wait through a search rate limit instead of abandoning the report.
GITHUB_SEARCH_MAX_WAIT_SECONDS = 75

# Concurrent reads bound refresh latency while limiting requests per tracker.
DEFAULT_REFRESH_WORKERS = 12
# Stop before the caller's timeout so the report can name unfinished work.
# In-flight requests may finish after this budget. --max-seconds 0 removes it.
DEFAULT_REFRESH_BUDGET_SECONDS = 180

# Recheck missing merge-request mirrors: they may appear after the PR opens.
# --pairing-recheck-hours 0 searches on every run.
DEFAULT_PAIRING_RECHECK_HOURS = 6

REPORTED_FIELDS = (
    "status",
    "conflicted",
    "lgtm_count",
    "approved",
    "ci_verdict",
    "comment_count",
    "head_sha",
)


# --------------------------------------------------------------- small helpers


def _configure_utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="backslashreplace")


def now_utc() -> datetime:
    return datetime.now(UTC)


def format_utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def parse_utc(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def contains_foreign_script(text: str) -> bool:
    """Check for characters above U+2E80 to decide whether to render a title."""
    return any(ord(char) > 0x2E80 for char in text)


# ------------------------------------------------------------- path resolution


def reject_path_in_skill_dir(path: str | Path | None, flag: str) -> None:
    """Refuse a working file inside the skill's own directory.

    The skill directory is one path shared by every run, and on a host that
    installs skills from a source of record it is replaced wholesale on the next
    install, so a store written there is both raced and disposable. This check
    exists because that has cost a store before, on more than one skill.
    """
    if not path:
        return
    try:
        resolved = Path(path).resolve()
    except OSError:
        return
    if resolved != SKILL_DIR and SKILL_DIR not in resolved.parents:
        return
    raise SystemExit(
        f"{flag} {path} is inside the skill's own directory.\n"
        "Every run shares that one path, and a host that installs skills from a "
        "source of record replaces the directory wholesale on the next install, "
        "so a store there is overwritten by other runs and lost on reinstall.\n"
        "Name a path outside the skill directory instead -- a state or workspace "
        "directory belonging to the host, for example "
        f"$XDG_STATE_HOME/{SKILL_NAME}/<channel-id>.jsonl"
    )


def default_state_file(channel: str) -> Path:
    """XDG-compliant default for a host whose prompt names no path.

    `$XDG_STATE_HOME` when set, else `~/.local/state`, which means the same
    thing on every host this is meant to travel to. A prompt should override it
    with an absolute path under the host's own workspace, because `~/.local/state`
    is outside the sandbox's allowed roots on a sandboxed host and outside
    whatever backup covers that workspace.
    """
    configured = os.environ.get("XDG_STATE_HOME", "").strip()
    root = Path(configured).expanduser() if configured else Path.home() / ".local" / "state"
    return root / SKILL_NAME / f"{channel or 'default'}.jsonl"


ENVIRONMENT_REFERENCE = re.compile(r"\$(?:\{(\w+)\}|(\w+))")


def emptied_variables(value: str) -> list[str]:
    """Return names of path variables that are set to an empty string. Such a variable can turn ``$VAR/rest`` into ``/rest`` without leaving an unexpanded variable to detect. Unset variables remain visible to the separate unexpanded-variable check."""
    names = {
        match.group(1) or match.group(2) for match in ENVIRONMENT_REFERENCE.finditer(value)
    }
    return sorted(
        name for name in names if name in os.environ and not os.environ[name].strip()
    )


def resolve_state_file(value: str | None, channel: str) -> tuple[Path, list[str]]:
    """Resolve `--state-file` to an absolute path, and say so when it is guessed.

    Environment variables are expanded here as well as by the shell, so a prompt
    that single-quotes a path written in terms of an environment variable still
    names the intended file rather than creating a directory whose name starts
    with a dollar sign.
    """
    notes: list[str] = []
    if not value:
        path = default_state_file(channel)
        notes.append(
            f"no --state-file given; using the XDG default {path}. "
            "The prompt that runs this command is supposed to name the store "
            "path explicitly."
        )
        return path, notes
    # Checked before anything resolves the value, because an emptied variable is
    # the most accurate thing that can be said about the path and every later
    # check would describe it wrongly: the skill-directory check resolves a value
    # it cannot expand against the working directory, so `$VAR/...` can be
    # refused as living inside the skill directory when it does not.
    emptied = emptied_variables(str(value))
    if emptied:
        raise SystemExit(
            "--state-file is written in terms of "
            + ", ".join(emptied)
            + ", which is set in this environment but holds nothing, so the path "
            "expands with its root missing and names somewhere that does not "
            "exist.\nThat variable is normally set by whatever runs this "
            "command. Do not export it, assign it or otherwise re-establish it: "
            "use it as it already is, or write the path out in full."
        )
    expanded = os.path.expanduser(os.path.expandvars(str(value)))
    if "$" in expanded:
        raise SystemExit(
            f"--state-file {value} still contains an unexpanded variable after "
            f"expansion ({expanded}). Name the variable that is actually set in "
            "this environment, or write the path out in full."
        )
    # On the expanded value: an unexpanded `$VAR/...` is not a relative path, and
    # resolving it as one lands it under the working directory -- which reads as
    # a store inside the skill directory whenever that is where the run started.
    reject_path_in_skill_dir(expanded, "--state-file")
    path = Path(expanded)
    if not path.is_absolute():
        path = path.resolve()
        notes.append(
            f"--state-file was relative and resolved against the working "
            f"directory to {path}. State it absolutely: a relative path resolves "
            "differently for a scheduled run and a manual one, which silently "
            "splits the store in two and orphans half the history."
        )
    return path, notes


def runs_path(state_file: Path) -> Path:
    return state_file.with_suffix(".runs.json")


def lock_path(state_file: Path) -> Path:
    return Path(str(state_file) + ".lock")


def config_path(state_file: Path) -> Path:
    """Return the channel configuration path beside the store. Deriving it avoids another path for callers to supply and keeps editable configuration outside the replaceable skill directory. Naming it from the ledger gives each channel its own configuration."""
    return state_file.with_suffix(".config.json")


def names_held_open_in(directory: Path) -> list[str]:
    """Return entries in ``directory`` that a process still holds open. The check protects concurrent writes from directory cleanup; it uses open descriptors rather than timestamps. It checks ``/proc`` when available and this process's streams otherwise. Detection is best-effort."""
    prefix = f"{directory}{os.sep}"

    def entry_name(target: str) -> str | None:
        # A file whose name is already gone reads back as "<path> (deleted)".
        target = target.removesuffix(" (deleted)")
        if not target.startswith(prefix):
            return None
        return target[len(prefix) :].split(os.sep, 1)[0] or None

    try:
        processes = [item for item in Path("/proc").iterdir() if item.name.isdigit()]
    except OSError:
        processes = []
    held: set[str] = set()
    for process in processes:
        try:
            descriptors = list((process / "fd").iterdir())
        except OSError:
            # Exited between the two calls, or belongs to another user. Neither
            # is this run's business and neither is worth failing over.
            continue
        for descriptor in descriptors:
            try:
                target = os.readlink(descriptor)
            except OSError:
                continue
            name = entry_name(target)
            if name is not None:
                held.add(name)
    if not processes:
        # No /proc to read. This process's own streams are still checkable, and
        # a command whose own stdout was redirected into the directory it is
        # about to empty is the simplest form of the same mistake.
        for stream in (sys.stdout, sys.stderr):
            try:
                status = os.fstat(stream.fileno())
                children = list(directory.iterdir())
            except (AttributeError, OSError, ValueError):
                continue
            for child in children:
                try:
                    if os.path.samestat(status, child.stat()):
                        held.add(child.name)
                except OSError:
                    continue
    return sorted(held)


def run_dir_for(state_file: Path) -> Path:
    """Return this run's scratch directory beside the store. Its stable location lets separate commands find the same report artifacts. Keeping it beside the store avoids shared skill and working directories."""
    return state_file.with_suffix(".run")


def prepare_run_dir(state_file: Path) -> Path:
    """Clear and recreate this run's scratch directory. Clearing on entry prevents a failed run's artifacts from being mistaken for current output. Refuse to clear it while a process is writing there."""
    run_dir = run_dir_for(state_file)
    if run_dir.is_dir():
        held = names_held_open_in(run_dir)
        if held:
            raise SystemExit(
                f"the run directory is being written to right now: {run_dir}\n"
                f"Held open by a command that is still running: "
                f"{', '.join(held)}.\n"
                "\n"
                "`report` empties that directory as its first action, so "
                "anything a redirect on the same command line writes there is "
                "deleted while it is still being written. That is why the file "
                "cannot be found afterwards, and why issuing the same command "
                "line again cannot help.\n"
                "\n"
                "Run `report` on its own instead: no pipe into another program, "
                "and no `>` into the run directory. It already prints the "
                "finished report on stdout and writes the same text to "
                "report.md inside the run directory, so nothing has to be "
                "extracted from its output or saved out of it. Pass --output "
                "when the report belongs somewhere else."
            )
        shutil.rmtree(run_dir, ignore_errors=True)
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def describe_absent_store(state_file: Path) -> str:
    """Describe evidence that may explain why the store is absent. This is diagnostic only; it does not decide whether the store is allowed to be absent. A missing sidecar cannot distinguish a wrong path from a channel that has not registered items."""
    beside = sorted(
        suffix
        for suffix, path in (
            (".config.json", config_path(state_file)),
            (".runs.json", runs_path(state_file)),
        )
        if path.exists()
    )
    if not state_file.parent.exists():
        return (
            "The directory it would live in does not exist either, so this path "
            "never pointed anywhere. That is what a path written in terms of a "
            "variable looks like once the variable has expanded to nothing: it "
            "keeps its shape and loses its root."
        )
    if beside:
        return (
            "This channel's " + ", ".join(beside) + " is there beside it, so the "
            "channel is set up and its store is supposed to exist. Something "
            "renamed, moved or removed it -- the rows are more likely elsewhere "
            "than gone."
        )
    return (
        "The directory exists and holds nothing else belonging to this channel, "
        "so either the path is wrong or nothing has ever been registered here."
    )


def refuse_absent_store(state_file: Path, allow_missing: bool) -> None:
    """Stop reporting when the store is absent unless ``allow_missing`` is set. Otherwise, a wrong path could produce a plausible empty report. Sidecars cannot distinguish a wrong path from a new channel, so the caller must explicitly allow an empty report when that is intended."""
    if allow_missing or state_file.exists():
        return
    raise SystemExit(
        f"the store {store_label(state_file)} does not exist, so no report was "
        f"produced.\n{describe_absent_store(state_file)}\n"
        "A report against a missing store is not an empty report: it is a "
        "report about the wrong file, and it renders as a plausible '0 tracked' "
        "that no reader can distinguish from a quiet week. Check the path "
        "before anything else. If this channel really has registered nothing "
        "yet, register something -- `track` and `watch` each create the store "
        "-- or pass --allow-missing-store to say that an empty report is what "
        "you meant."
    )


# --------------------------------------------------------------- configuration


class ConfigError(SystemExit):
    """The configuration exists and cannot be read as configuration.

    A hard stop, deliberately unlike an absent file. A channel that configures
    nothing is a legitimate configuration and must be a clean no-op; a channel
    whose file the operator edited into something unparseable is a mistake, and
    the only way that mistake is ever noticed is if the run refuses. Both halves
    of this file fail silently otherwise: watching nobody and failing to read
    whom to watch look identical in the store afterwards, and a pairing that did
    not load looks exactly like a repository that has none.
    """


@dataclass(frozen=True)
class Repositories:
    """Repository configuration maps a GitHub repository to its GitCode project and preserves configured capitalization. Pairings are explicit because URLs do not establish that two repositories represent the same work. Repositories without a pairing remain tracked on GitHub alone. Slugs use case-insensitive ``owner/name`` identity."""

    by_slug: dict[str, dict[str, Any]] = field(default_factory=dict)
    by_gitcode: dict[str, str] = field(default_factory=dict)

    @property
    def slugs(self) -> list[str]:
        """Every configured repository, as the operator capitalised it."""
        return sorted(entry["display"] for entry in self.by_slug.values())

    def display(self, slug: str) -> str:
        """The configured capitalisation, or the slug unchanged when there is none."""
        entry = self.by_slug.get((slug or "").lower())
        return entry["display"] if entry else slug

    def gitcode_for(self, slug: str) -> str | None:
        entry = self.by_slug.get((slug or "").lower())
        return entry.get("gitcode") if entry else None

    def github_for_gitcode(self, project: str) -> str | None:
        """Lowercased, because the caller is building an identity key with it."""
        return self.by_gitcode.get((project or "").lower())

    @classmethod
    def from_config(
        cls, raw: Any, notes: list[str], *, where: str = "repositories"
    ) -> Repositories:
        if raw is None:
            return cls()
        if not isinstance(raw, dict):
            raise ConfigError(
                f"`{where}` must be an object keyed by repository, not "
                f"{type(raw).__name__}. The key is the repository and the value "
                "holds its properties, so that naming one twice is impossible "
                "and its capitalisation is stated exactly once."
            )
        by_slug: dict[str, dict[str, Any]] = {}
        by_gitcode: dict[str, str] = {}
        for slug, properties in raw.items():
            display = str(slug or "").strip().strip("/")
            if display.count("/") != 1 or not all(display.split("/")):
                raise ConfigError(
                    f"`{where}` has the key {slug!r}, which is not an owner/name "
                    "repository."
                )
            lowered = display.lower()
            if lowered in by_slug:
                raise ConfigError(
                    f"`{where}` names {display} twice in two capitalisations. "
                    "One repository, one entry: two would leave which "
                    "capitalisation a report shows down to dictionary order."
                )
            if properties is None:
                properties = {}
            if not isinstance(properties, dict):
                raise ConfigError(
                    f"`{where}.{display}` must be an object of properties, or "
                    f"an empty one for a repository tracked on the first tracker "
                    f"alone, not {type(properties).__name__}."
                )
            entry: dict[str, Any] = {"display": display}
            gitcode = properties.get("gitcode")
            if gitcode is not None:
                gitcode = str(gitcode).strip().strip("/")
                if gitcode.count("/") != 1 or not all(gitcode.split("/")):
                    raise ConfigError(
                        f"`{where}.{display}.gitcode` is "
                        f"{properties.get('gitcode')!r}, which is not a "
                        "namespace/project on the second tracker."
                    )
                owner = by_gitcode.get(gitcode.lower())
                if owner:
                    # The one misconfiguration that silently reports fiction:
                    # both repositories would then be told the other's merges.
                    raise ConfigError(
                        f"`{where}` pairs both {owner} and {display} with "
                        f"{gitcode}. A project belongs to one repository: the "
                        "pairing decides whose merge a landing is, and sharing "
                        "it reports each one's as the other's."
                    )
                by_gitcode[gitcode.lower()] = lowered
                entry["gitcode"] = gitcode
            unknown = sorted(
                key
                for key in properties
                if key not in {"gitcode"} and not str(key).startswith("_")
            )
            if unknown:
                notes.append(
                    f"`{where}.{display}` has key(s) {', '.join(unknown)}, which "
                    "this version does not read. They were ignored, not "
                    "rejected."
                )
            by_slug[lowered] = entry
        return cls(by_slug=by_slug, by_gitcode=by_gitcode)


NO_REPOSITORIES = Repositories()

CONFIG_SHAPE = """{
  "schema_version": 1,
  "repositories": {
    "owner/name": {"gitcode": "namespace/project"},
    "owner/other": {}
  },
  "watch": {
    "kinds": ["pull_request", "issue"],
    "state": "all",
    "window_days": 7,
    "authors": ["a-login", {"login": "another", "kinds": ["pull_request"]}]
  }
}"""


def config_help(path: Path) -> str:
    """What to write, and where, without naming anybody's directory layout."""
    return (
        f"The configuration is {path.name}, beside the store and named after it: "
        "take the store's file name, replace the .jsonl suffix with "
        ".config.json, and leave it in the same directory. It holds the "
        "repositories to track and the logins to watch, and nothing else:\n"
        f"{CONFIG_SHAPE}\n"
        "`repositories` is keyed by repository; the key is its own "
        "capitalisation, and an empty value is a repository tracked on the "
        "first tracker alone. Under `watch`, `authors` is the only required "
        "key, and an entry is a login or an object with a login and any of the "
        "sibling keys overridden for that one author; `repos` narrows the watch "
        "to some of the configured repositories and otherwise every author is "
        "watched in all of them. Keys beginning with an underscore are ignored, "
        "so the file can carry notes."
    )


def load_config(path: Path) -> tuple[dict[str, Any], list[str]]:
    """Read the channel configuration. Return an empty object when the file is absent and include notes. Invalid configuration stops the command because continuing could silently omit GitCode pairing data."""
    notes: list[str] = []
    if not path.exists():
        return {}, notes
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise ConfigError(f"the configuration {path.name} could not be read ({error}).")
    except json.JSONDecodeError as error:
        raise ConfigError(
            f"the configuration {path.name} is not valid JSON (line "
            f"{error.lineno}, column {error.colno}: {error.msg}). Nothing was "
            "searched and nothing was registered: a file that cannot be read is "
            "not the same as a list of nobody.\n" + config_help(path)
        )
    if isinstance(raw, list):
        raise ConfigError(
            f"the configuration {path.name} is a bare list. Wrap it so the "
            "file can gain a setting later without every existing one needing a "
            "rewrite:\n" + config_help(path)
        )
    if not isinstance(raw, dict):
        raise ConfigError(
            f"the configuration {path.name} must hold an object.\n"
            + config_help(path)
        )
    known = {"schema_version", "repositories", "watch"}
    unknown = sorted(
        key for key in raw if key not in known and not str(key).startswith("_")
    )
    if unknown:
        notes.append(
            f"the configuration has key(s) {', '.join(unknown)}, which this "
            "version does not read. They were ignored, not rejected: a newer "
            "file on an older script should still track and watch what it names."
        )
    watch = raw.get("watch")
    if watch is not None and not isinstance(watch, dict):
        raise ConfigError(
            f"the configuration's `watch` must be an object, not "
            f"{type(watch).__name__}.\n" + config_help(path)
        )
    known_watch = {"schema_version", "authors", "repos", "kinds", "state", "window_days"}
    unknown = sorted(
        key
        for key in watch or {}
        if key not in known_watch and not str(key).startswith("_")
    )
    if unknown:
        notes.append(
            f"the configuration's `watch` has key(s) {', '.join(unknown)}, which "
            "this version does not read. They were ignored, not rejected: a "
            "newer file on an older script should still watch the authors it "
            "names."
        )
    return raw, notes


# ----------------------------------------------------------------------- locks


@contextlib.contextmanager
def file_lock(path: Path) -> Iterator[None]:
    """Hold an exclusive lock on a sibling ``.lock`` file. Use ``portalocker`` when installed, otherwise use ``fcntl.flock`` on POSIX. If neither is available, stop rather than write without a lock. Announce use of the fallback on stderr."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        import portalocker  # type: ignore[import-not-found]
    except ImportError:
        portalocker = None
    if portalocker is not None:
        with portalocker.Lock(str(path), "a", timeout=30):
            yield
        return
    try:
        import fcntl
    except ImportError as error:  # pragma: no cover - non-POSIX host
        raise SystemExit(
            "neither portalocker nor fcntl is available, so the store cannot be "
            "locked. Install portalocker before running this skill here."
        ) from error
    print(
        "note: portalocker is not installed; locking the store with fcntl.flock",
        file=sys.stderr,
    )
    with open(path, "a", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


# ---------------------------------------------------------------- URL identity


@dataclass(frozen=True)
class ParsedLink:
    """One trackable link, reduced to the row it belongs to."""

    key: str
    url: str
    kind: str  # pull_request | issue | other
    tracker: str  # github | gitcode | none
    repo: str  # owner/name on GitHub, namespace/project on GitCode
    number: int | None
    raw: str


def strip_trailing_punctuation(url: str) -> str:
    while url and url[-1] in TRAILING_PUNCTUATION:
        url = url[:-1]
    return url


def canonicalise(raw_url: str) -> ParsedLink | None:
    """Reduce a posted URL to its identity key, or None when it is not a link.

    Normalisation, in order: lowercase the scheme and host, drop the query, the
    fragment and any trailing slash, and reduce
    `/pull/2724/files`, `/pull/2724/commits` and `#issuecomment-...` to the item
    root.
    """
    url = strip_trailing_punctuation((raw_url or "").strip())
    if not url:
        return None
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return None
    if parts.scheme.lower() not in {"http", "https"}:
        return None
    host = (parts.hostname or "").lower()
    host = host.removeprefix("www.")
    segments = [segment for segment in parts.path.split("/") if segment]
    canonical_host = f"https://{host}"

    if host == "github.com":
        link = _parse_github(canonical_host, segments)
    elif host in {"gitcode.com", "api.gitcode.com", "raw.gitcode.com"}:
        link = _parse_gitcode(canonical_host, segments)
    else:
        link = None
    if link is not None:
        return ParsedLink(**{**link.__dict__, "raw": url})
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]
    return ParsedLink(
        key=f"other/{digest}",
        url=url,
        kind="other",
        tracker="none",
        repo="",
        number=None,
        raw=url,
    )


def _parse_github(host: str, segments: list[str]) -> ParsedLink | None:
    if len(segments) < 4:
        return None
    owner, repo, noun, number = segments[0], segments[1], segments[2], segments[3]
    if not number.isdigit():
        return None
    repo_slug = f"{owner}/{repo}"
    if noun == "pull":
        return ParsedLink(
            key=f"github/{repo_slug.lower()}#pr{int(number)}",
            url=f"{host}/{repo_slug}/pull/{int(number)}",
            kind="pull_request",
            tracker="github",
            repo=repo_slug,
            number=int(number),
            raw="",
        )
    if noun == "issues":
        return ParsedLink(
            key=f"github/{repo_slug.lower()}#issue{int(number)}",
            url=f"{host}/{repo_slug}/issues/{int(number)}",
            kind="issue",
            tracker="github",
            repo=repo_slug,
            number=int(number),
            raw="",
        )
    return None


def _parse_gitcode(host: str, segments: list[str]) -> ParsedLink | None:
    cleaned = [segment for segment in segments if segment != "-"]
    # Tolerate the API spelling: /api/v5/repos/<ns>/<project>/pulls/<iid>
    if cleaned[:3] == ["api", "v5", "repos"]:
        cleaned = cleaned[3:]
    if len(cleaned) < 4:
        return None
    namespace, project, noun, number = cleaned[0], cleaned[1], cleaned[2], cleaned[3]
    if not number.isdigit():
        return None
    slug = f"{namespace}/{project}"
    if noun in {"merge_requests", "pulls"}:
        return ParsedLink(
            key=f"gitcode/{slug.lower()}#mr{int(number)}",
            url=f"https://gitcode.com/{slug}/merge_requests/{int(number)}",
            kind="pull_request",
            tracker="gitcode",
            repo=slug,
            number=int(number),
            raw="",
        )
    if noun == "issues":
        return ParsedLink(
            key=f"gitcode/{slug.lower()}#issue{int(number)}",
            url=f"https://gitcode.com/{slug}/issues/{int(number)}",
            kind="issue",
            tracker="gitcode",
            repo=slug,
            number=int(number),
            raw="",
        )
    return None


def links_in_text(text: str) -> list[ParsedLink]:
    """Every link in one message body, in order, de-duplicated by identity key.

    A link trigger typically fires once per message however many links it
    carries, so one trigger routinely means several items. Two links to the same
    item collapse
    here to one entry whose `raw` is the first spelling seen;
    the caller records both spellings in `sources[]`.
    """
    found: list[ParsedLink] = []
    seen: set[str] = set()
    for match in HTTP_URL_RE.finditer(text or ""):
        link = canonicalise(match.group(0))
        if link is None or link.key in seen:
            continue
        seen.add(link.key)
        found.append(link)
    return found


# ---------------------------------------------------------------------- ledger


@dataclass
class Ledger:
    """The JSONL store, plus whatever of it did not parse.

    A line that does not parse is skipped for reading, kept verbatim at its
    original position on write, and reported by line number. Dropping it would
    lose an item; refusing every write would stop registration
    on the whole channel because of one bad line.
    """

    path: Path
    rows: list[dict[str, Any]] = field(default_factory=list)
    corrupt: list[tuple[int, str]] = field(default_factory=list)
    existed: bool = True

    @classmethod
    def load(cls, path: Path) -> Ledger:
        if not path.exists():
            return cls(path=path, rows=[], corrupt=[], existed=False)
        rows: list[dict[str, Any]] = []
        corrupt: list[tuple[int, str]] = []
        with path.open("r", encoding="utf-8") as handle:
            for number, line in enumerate(handle, start=1):
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    parsed = json.loads(stripped)
                except json.JSONDecodeError:
                    corrupt.append((number, stripped))
                    continue
                if isinstance(parsed, dict) and parsed.get("key"):
                    rows.append(parsed)
                else:
                    corrupt.append((number, stripped))
        return cls(path=path, rows=rows, corrupt=corrupt, existed=True)

    def index(self) -> dict[str, dict[str, Any]]:
        """Key and alias -> row.

        A GitCode MR registered before its pairing was discovered keeps living in
        the surviving row's `aliases`, so re-posting the GitCode link later lands
        on the GitHub row without waiting for another refresh.
        """
        table: dict[str, dict[str, Any]] = {}
        for row in self.rows:
            table[row["key"]] = row
            for alias in row.get("aliases") or []:
                table.setdefault(alias, row)
        return table

    def serialise(self) -> str:
        lines = [json.dumps(row, ensure_ascii=False, sort_keys=True) for row in self.rows]
        for _, text in self.corrupt:
            lines.append(text)
        return "".join(f"{line}\n" for line in lines)

    def write(self) -> None:
        """Whole-file atomic rewrite: temp file in the same directory, replace."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=self.path.parent,
            delete=False,
        ) as handle:
            handle.write(self.serialise())
            temp_path = Path(handle.name)
        temp_path.replace(self.path)
        self.existed = True


def read_runs(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def write_runs(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        delete=False,
    ) as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        temp_path = Path(handle.name)
    temp_path.replace(path)


def check_owner(runs: dict[str, Any], channel: str) -> None:
    """Stop rather than interleave two channels into one store."""
    owner = str(runs.get("channel_id") or "")
    if owner and channel and owner != channel:
        raise SystemExit(
            f"this store belongs to channel {owner}, but the run is serving "
            f"{channel}. Nothing was written. Either the prompt names the wrong "
            "--state-file or the wrong --channel; fix the prompt rather than the "
            "store."
        )


def stream_key(repo_filter: str | None) -> str:
    return repo_filter.lower() if repo_filter else "all"


def known_repositories(repositories: Repositories, state_file: Path) -> list[str]:
    """Every repository this store can report on.

    Configured plus already tracked, because either alone is wrong: a
    repository can be configured before anything from it is registered, and a
    row can outlive the configuration entry it arrived under.
    """
    known = {slug for slug in repositories.slugs if slug}
    known.update(
        str(row.get("repo"))
        for row in Ledger.load(state_file).rows
        if row.get("repo")
    )
    return sorted(known, key=str.lower)


def resolve_repo_filter(
    repo_filter: str | None, repositories: Repositories, state_file: Path
) -> str | None:
    """Resolve ``--repo`` to the configured spelling or refuse it. An unknown repository could produce an empty, plausible report and create a separate watermark stream. Returning the configured spelling keeps report headers and watermark keys consistent."""
    if not repo_filter:
        return None
    known = known_repositories(repositories, state_file)
    for slug in known:
        if slug.lower() == repo_filter.lower():
            return slug
    if known:
        listing = "This store knows:\n" + "".join(f"  {slug}\n" for slug in known)
    else:
        listing = (
            "This store has no repository configured and no row naming one, so "
            "there is nothing --repo could narrow to.\n"
        )
    raise SystemExit(
        f"--repo {repo_filter} matches no repository this store tracks or is "
        "configured for. Nothing was refreshed, nothing was reported and no "
        "watermark moved.\n"
        + listing
        + "--repo takes a repository as owner/name, spelled as one of the lines "
        "above. It is not a channel name, not a project's nickname and not a "
        "deployment's name for the work: a value matching none of them filters "
        "every row out and renders a store full of open items as a quiet week. "
        "Drop the flag to report on every repository in the store."
    )


# ------------------------------------------------------------ registration


def new_row(link: ParsedLink, when: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "key": link.key,
        "url": link.url,
        "kind": link.kind,
        "tracker": link.tracker,
        "repo": link.repo,
        "number": link.number,
        "aliases": [],
        "sources": [],
        "first_seen_utc": when,
        "slack_permalink": None,
        "slack_author": None,
        "lifecycle": "ignored" if link.kind == "other" else "active",
        "reopen_count": 0,
        "observed_through_utc": None,
        "last_mentioned_run": None,
    }


# A Slack `ts` is the message's identity, not a rendered time: `<10-digit
# epoch>.<6 digits>`, exactly, for every message Slack has ever issued and until
# the epoch grows an eleventh digit in 2286.
SLACK_TS_RE = re.compile(r"\d{10}\.\d{6}")
# Slack opened to the public in 2013; nothing older can be a message id from a
# channel this store serves. The floor is deliberately earlier than the product,
# because the point is to reject a fabrication, not to date-check a real message.
SLACK_TS_FLOOR = 1356998400.0  # 2013-01-01T00:00:00Z
# A real `ts` is always in the past by construction -- the message exists before
# the turn that registers it -- so the only legitimate slack ahead of `now` is
# clock skew between this host and Slack, which is seconds. Five minutes absorbs
# that with room to spare while still rejecting the timezone-shaped fabrication
# that motivated this check, which landed eight hours in the future.
SLACK_TS_FUTURE_TOLERANCE_SECONDS = 300.0

SLACK_TS_SHAPE = (
    "A Slack ts is the message id as Slack issues it: ten digits, a dot, six "
    "digits, for example 1786613308.937139. It is not a human-readable date, not "
    "a local-time clock reading and not something to derive from the current "
    "time."
)
SLACK_PERMALINK_SHAPE = (
    "A Slack permalink is the URL Slack issues: https:// on slack.com or a "
    "workspace subdomain of it, for example "
    "https://example.slack.com/archives/C0123456789/p1786613308937139."
)
PROVENANCE_REMEDY = (
    "The value was refused and the item registered without it, exactly as if the "
    "flag had been omitted -- which is the documented thing to do when the value "
    "is not available. Stop passing the flag: a guessed value is worse than a "
    "missing one, because it is indistinguishable from a real one once written."
)


def check_slack_ts(value: str, *, now: datetime | None = None) -> str | None:
    """Return why ``value`` is not a Slack timestamp, or ``None`` when it is valid. Check the ``<10 digits>.<6 digits>`` shape, then reject values outside the accepted historical floor and current-time ceiling with clock-skew tolerance."""
    if not SLACK_TS_RE.fullmatch(value):
        return f"{value!r} is not the shape of a Slack message timestamp"
    seconds = float(value)
    stamped = format_utc(datetime.fromtimestamp(seconds, tz=UTC))
    if seconds < SLACK_TS_FLOOR:
        return f"{value!r} resolves to {stamped}, before Slack existed"
    ahead = seconds - (now or now_utc()).timestamp()
    if ahead > SLACK_TS_FUTURE_TOLERANCE_SECONDS:
        return (
            f"{value!r} resolves to {stamped}, {int(ahead // 60)} minutes in the "
            "future, so it cannot be a message this run is registering (either it "
            "was fabricated or this host's clock is badly skewed)"
        )
    return None


def check_slack_permalink(value: str) -> str | None:
    """Return why ``value`` is not a Slack permalink, or ``None`` when valid. Require HTTPS on ``slack.com`` or a workspace subdomain. Do not validate the path or compare its timestamp with ``--slack-ts``; a thread reply permalink can contain the reply timestamp while the registration carries the parent's."""
    host = ""
    with contextlib.suppress(ValueError):
        host = (urllib.parse.urlparse(value).hostname or "").lower()
    if not value.lower().startswith("https://"):
        return f"{value!r} is not an https:// URL"
    if not (host == "slack.com" or host.endswith(".slack.com")):
        return f"{value!r} is not on slack.com"
    return None


@dataclass
class Provenance:
    """Validate optional Slack registration metadata. Invalid timestamps and permalinks are rejected while registration continues, because the caller may not have access to message metadata. Report the rejection on stderr, in JSON as ``rejected_provenance``, and in the row as ``slack_ts_rejected`` so ``audit`` can find it later. Preserve the rejected value verbatim outside ``slack_ts``.

Do not validate ``--slack-author``. A display name or user ID has no reliable shape check, so validation could reject legitimate values without identifying fabricated ones."""

    slack_ts: str | None = None
    permalink: str | None = None
    author: str | None = None
    rejected: list[dict[str, str]] = field(default_factory=list)

    @classmethod
    def check(
        cls,
        *,
        slack_ts: str | None,
        permalink: str | None,
        author: str | None,
        now: datetime | None = None,
    ) -> Provenance:
        result = cls(author=author)
        if slack_ts is not None:
            complaint = check_slack_ts(str(slack_ts), now=now)
            if complaint is None:
                result.slack_ts = str(slack_ts)
            else:
                result.rejected.append(
                    {
                        "flag": "--slack-ts",
                        "value": str(slack_ts),
                        "reason": complaint,
                        "expected": SLACK_TS_SHAPE,
                        "effect": PROVENANCE_REMEDY,
                    }
                )
        if permalink is not None:
            complaint = check_slack_permalink(str(permalink))
            if complaint is None:
                result.permalink = str(permalink)
            else:
                result.rejected.append(
                    {
                        "flag": "--slack-permalink",
                        "value": str(permalink),
                        "reason": complaint,
                        "expected": SLACK_PERMALINK_SHAPE,
                        "effect": PROVENANCE_REMEDY,
                    }
                )
        return result

    @property
    def rejected_slack_ts(self) -> str | None:
        for entry in self.rejected:
            if entry["flag"] == "--slack-ts":
                return entry["value"]
        return None

    def warning(self) -> str:
        lines = ["provenance was refused and the registration continued without it:"]
        for entry in self.rejected:
            lines.append(f"  {entry['flag']} {entry['reason']}.")
            lines.append(f"  {entry['expected']}")
        lines.append(f"  {PROVENANCE_REMEDY}")
        return "\n".join(lines)


def audit_provenance(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Find two provenance problems: stored ``sources[]`` entries with invalid ``slack_ts`` values, and ``slack_ts_rejected`` traces from registrations where a timestamp was refused. Leave stored values unchanged because their original value cannot be recovered. Operators can unregister and register the item again."""
    stored: list[dict[str, Any]] = []
    refused: list[dict[str, Any]] = []
    for row in rows:
        for position, entry in enumerate(row.get("sources") or []):
            if not isinstance(entry, dict):
                continue
            common = {
                "key": row.get("key"),
                "url": row.get("url"),
                "source_index": position,
                "via": entry.get("via"),
                "registered_at": entry.get("at"),
            }
            raw = entry.get("slack_ts")
            if raw is not None and not SLACK_TS_RE.fullmatch(str(raw)):
                stored.append(
                    {**common, "slack_ts": raw, "first_seen_utc": row.get("first_seen_utc")}
                )
            refusal = entry.get("slack_ts_rejected")
            if refusal is not None:
                refused.append({**common, "slack_ts_rejected": refusal})
    return {"stored": stored, "refused": refused}


def upsert_registration(
    ledger: Ledger,
    link: ParsedLink,
    *,
    via: str,
    slack_ts: str | None,
    permalink: str | None,
    author: str | None,
    when: str,
    slack_ts_rejected: str | None = None,
    watch_author: str | None = None,
) -> tuple[dict[str, Any], str]:
    """Register one link. Returns the row and what happened to it.

    A pure upsert on the identity key: registering the same item twice is a
    no-op on everything but `sources[]`. This is what makes reposting a link the
    service missed a repair rather than a duplicate -- it lands on the existing
    row instead of creating a second one -- and what makes an operator-run
    reconciliation safe to repeat.
    """
    table = ledger.index()
    row = table.get(link.key)
    outcome = "updated"
    if row is None:
        row = new_row(link, when)
        ledger.rows.append(row)
        outcome = "registered"
    elif row.get("lifecycle") == "retired":
        # A retired item whose URL is posted again is revived, not re-created:
        # its history is the point of keeping the row.
        row["lifecycle"] = "active"
        row["retired_at_utc"] = None
        row["terminal_observed_utc"] = None
        row["reopen_count"] = int(row.get("reopen_count") or 0) + 1
        outcome = "reopened"

    source = {
        "via": via,
        "slack_ts": slack_ts,
        "url": link.raw or link.url,
        "at": when,
    }
    if watch_author:
        # Which watched login this registration came from. It is the only record
        # that this channel has ever seen work by that person, and the watch
        # reads it back to tell a login it has swept before from one it is
        # meeting for the first time.
        source["watch_author"] = watch_author
    if slack_ts_rejected is not None:
        # Recorded beside the source, never as the source's `slack_ts`, and left
        # out of the dedupe key: with the ts refused this entry is exactly the
        # `slack_ts: null` entry an omitted flag would have produced, and it has
        # to collapse with one. The trace is diagnosis, not provenance.
        source["slack_ts_rejected"] = slack_ts_rejected
    existing = row.setdefault("sources", [])
    # Deduplicated on (via, slack_ts, url). Never on slack_ts alone -- one
    # message carries several links and they are different registrations of
    # different items -- and never without the route either: an item the watch
    # found and someone also posted a link to arrived twice, by two routes, and
    # both are true. Without the route in the key those two collapse whenever
    # neither carries a message id, and the store then says the item came in by
    # whichever route happened to be first.
    if not any(
        entry.get("via") == source["via"]
        and entry.get("slack_ts") == source["slack_ts"]
        and entry.get("url") == source["url"]
        for entry in existing
    ):
        existing.append(source)
    if permalink and not row.get("slack_permalink"):
        row["slack_permalink"] = permalink
    if author and not row.get("slack_author"):
        row["slack_author"] = author
    if slack_ts and outcome == "registered":
        stamped = slack_ts_to_utc(slack_ts)
        if stamped:
            row["first_seen_utc"] = stamped
    return row, outcome


def slack_ts_to_utc(slack_ts: str | None) -> str | None:
    try:
        seconds = float(str(slack_ts))
    except (TypeError, ValueError):
        return None
    return format_utc(datetime.fromtimestamp(seconds, tz=UTC))


def pairing_from_rows(
    rows: list[dict[str, Any]], repositories: Repositories = NO_REPOSITORIES
) -> dict[str, str]:
    """Return provisional GitCode rows whose GitHub identity has evidence from a refresh: an existing GitHub row that discovered the merge-request IID, or a ``github-pr-<N>`` source branch. Registration cannot establish a pairing from URLs alone. The source-branch proof also requires configured repository mapping."""
    pairing: dict[str, str] = {}
    github_by_iid: dict[int, str] = {}
    for row in rows:
        if row.get("tracker") == "github" and row.get("mr_iid") is not None:
            with contextlib.suppress(TypeError, ValueError):
                github_by_iid[int(row["mr_iid"])] = row["key"]
    for row in rows:
        if row.get("tracker") != "gitcode" or row.get("kind") != "pull_request":
            continue
        number = row.get("number")
        target = github_by_iid.get(int(number)) if number is not None else None
        if target:
            pairing[row["key"]] = target
            continue
        match = re.fullmatch(r"github-pr-(\d+)", str(row.get("mr_source_branch") or ""))
        github_repo = repositories.github_for_gitcode(row.get("repo") or "")
        if match and github_repo:
            pairing[row["key"]] = f"github/{github_repo}#pr{int(match.group(1))}"
    return pairing


def parse_github_key(key: str) -> tuple[str, str, int] | None:
    match = re.fullmatch(r"github/([^#]+)#(pr|issue)(\d+)", key or "")
    if not match:
        return None
    return match.group(1), match.group(2), int(match.group(3))


def coalesce_pairs(
    ledger: Ledger,
    pairing: dict[str, str],
    repositories: Repositories = NO_REPOSITORIES,
) -> list[tuple[str, str]]:
    """Merge a provisional GitCode row into its GitHub pull-request row after refresh proves the pairing. Preserve sources and the earliest ``first_seen_utc``; retain the provisional key as an alias. If no GitHub row exists, re-key the GitCode row to the GitHub identity. This function does not access the network."""
    merged: list[tuple[str, str]] = []
    table = {row["key"]: row for row in ledger.rows}
    for gitcode_key, github_key in pairing.items():
        source = table.get(gitcode_key)
        target = table.get(github_key)
        if source is None or source is target:
            continue
        if target is None:
            parsed = parse_github_key(github_key)
            if parsed is None:
                continue
            repo_slug, noun, number = parsed
            aliases = source.setdefault("aliases", [])
            if gitcode_key not in aliases:
                aliases.append(gitcode_key)
            source["key"] = github_key
            source["tracker"] = "github"
            # The key is lowercased, so the owner's own capitalisation can only
            # come back from configuration. Absent, the slug stands as it is:
            # the item is still tracked, under a name spelled in lower case.
            source["repo"] = repositories.display(repo_slug)
            source["number"] = number
            source["kind"] = "pull_request" if noun == "pr" else "issue"
            source["url"] = (
                f"https://github.com/{source['repo']}/"
                f"{'pull' if noun == 'pr' else 'issues'}/{number}"
            )
            table.pop(gitcode_key, None)
            table[github_key] = source
            merged.append((gitcode_key, github_key))
            continue
        for entry in source.get("sources") or []:
            if not any(
                existing.get("slack_ts") == entry.get("slack_ts")
                and existing.get("url") == entry.get("url")
                for existing in target.setdefault("sources", [])
            ):
                target["sources"].append(entry)
        aliases = target.setdefault("aliases", [])
        for alias in [gitcode_key, *(source.get("aliases") or [])]:
            if alias not in aliases:
                aliases.append(alias)
        first_seen = [
            value
            for value in (source.get("first_seen_utc"), target.get("first_seen_utc"))
            if value
        ]
        if first_seen:
            target["first_seen_utc"] = min(first_seen)
        if not target.get("slack_permalink"):
            target["slack_permalink"] = source.get("slack_permalink")
        if not target.get("slack_author"):
            target["slack_author"] = source.get("slack_author")
        if source.get("lifecycle") == "ignored" and target.get("lifecycle") == "active":
            pass  # an explicit untrack of one facet does not untrack the item
        ledger.rows = [row for row in ledger.rows if row is not source]
        table.pop(gitcode_key, None)
        merged.append((gitcode_key, github_key))
    return merged


# ------------------------------------------------------------- status and CI


def derive_status(row: dict[str, Any]) -> str:
    """The status enum, recomputed from the facts each tick.

    Never stored: two sources of truth for one thing is how a store and a report
    start disagreeing.
    """
    if row.get("kind") == "other":
        return "ignored"
    if row.get("gone"):
        return "gone"
    if row.get("kind") == "issue":
        # The enum is pull-request shaped; an issue has two states and borrowing
        # `awaiting_review` for one of them would read as a claim about review
        # that nobody made.
        return "issue_closed" if row.get("gh_state") == "closed" else "issue_open"
    if row.get("mr_state") == "merged" or row.get("merged_at"):
        return "landed"
    if row.get("mr_state") == "closed" or row.get("gh_state") == "closed":
        return "closed"
    if row.get("approved"):
        return "approved"
    if int(row.get("lgtm_count") or 0) > 0:
        return "lgtm_partial"
    if row.get("cla_ok") is False:
        return "cla_pending"
    verdict = row.get("ci_verdict")
    if verdict == "fail":
        return "ci_failed"
    if verdict == "running":
        return "ci_running"
    if verdict == "pass":
        return "awaiting_review"
    return "registered"


def labels_to_facts(labels: Iterable[str]) -> dict[str, Any]:
    names = [str(name) for name in labels]
    lowered = {name.lower() for name in names}
    lgtm = sorted(name for name in names if name.lower().startswith("lgtm-"))
    verdict = None
    if "ci-failed" in lowered:
        verdict = "fail"
    elif "ci-running" in lowered:
        verdict = "running"
    elif "ci-successful" in lowered:
        verdict = "pass"
    known = {
        "github-mirror",
        "approved",
        "conflicted",
        "merged",
        "sync-managed",
        "ci-running",
        "ci-failed",
        "ci-successful",
        "check-successful",
    }
    unknown = sorted(
        name
        for name in names
        if name.lower() not in known
        and not name.lower().startswith("lgtm-")
        and not name.lower().startswith("openjiuwen-cla/")
        and not name.lower().startswith("sig/")
    )
    return {
        "cla_ok": "openjiuwen-cla/yes" in lowered,
        "approved": "approved" in lowered,
        "lgtm_count": len(lgtm),
        "lgtm_logins": lgtm,
        "ci_verdict": verdict,
        "ci_state": verdict,
        "conflicted_label": "conflicted" in lowered,
        "unknown_labels": unknown,
    }


def classify_ci_failure(
    failing_tests: list[dict[str, Any]],
    signatures: list[dict[str, Any]],
) -> tuple[str, list[str]]:
    """`matches_known_flake` | `unrelated_to_known_flake` | `unclassified`.

    Matched on the failure *signature*, never on the test name: the known flake's
    victim rotates, because three tests dial example.com and the resulting
    unraisable warning fails whichever unrelated test happened to be running. A
    name allowlist would be wrong on its first run.
    """
    if not failing_tests:
        return "unclassified", []
    matched: list[str] = []
    for test in failing_tests:
        blob = " ".join(
            str(test.get(field_name) or "")
            for field_name in ("testId", "message", "longrepr", "log", "text")
        )
        for signature in signatures:
            needles = [str(part) for part in signature.get("all_of") or []]
            if needles and all(needle in blob for needle in needles):
                matched.append(str(signature.get("name") or "unnamed"))
                break
        else:
            return "unrelated_to_known_flake", sorted(set(matched))
    return "matches_known_flake", sorted(set(matched))


def load_flake_signatures(path: Path | None) -> tuple[list[dict[str, Any]], list[str]]:
    """Load known-flake signatures and return any notes. A missing caller-specified file or malformed file is an error. A missing shipped default is allowed but reported, because CI failures then cannot be classified as known flakes."""
    named = path is not None
    target = path or (SKILL_DIR / "references" / "ci-flake-signatures.json")
    notes: list[str] = []
    if not target.exists():
        if named:
            raise SystemExit(
                f"--flake-signatures {target} does not exist. No signatures would "
                "be loaded, so no CI failure in this run could be classified and "
                "every red build would be reported as matching nothing recorded "
                "-- which is indistinguishable from having actually checked. "
                "Correct the path, or omit the flag to use the signatures that "
                "ship with the skill."
            )
        notes.append(
            "the signature file that ships with the skill is not there, so no "
            "known flake can be recognised and every failing build in this run "
            "reads as matching nothing recorded. That is only a true statement if "
            "this host really records no flakes."
        )
        return [], notes
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SystemExit(
            f"the known-flake signatures in {target.name} exist and cannot be "
            f"read ({error}). A file that is there and unparseable is never a "
            "choice, and carrying on would classify no CI failure in this run "
            "while reporting every red build as matching nothing recorded. Fix "
            "the file, or remove it to say that this host records no flakes."
        ) from error
    signatures = data.get("signatures") if isinstance(data, dict) else data
    if not isinstance(signatures, list):
        raise SystemExit(
            f"the known-flake signatures in {target.name} are not a list. Expected "
            'either a list of signatures, or an object with a "signatures" key '
            "holding one. Nothing would have been classified, and the report has "
            "no way to show that."
        )
    # An entry with no `all_of` strings is matched against nothing and can never
    # fire, so a file whose entries have lost that key parses perfectly and
    # classifies as little as an absent one. Same failure, one level down.
    usable = [item for item in signatures if isinstance(item, dict) and item.get("all_of")]
    if len(usable) != len(signatures):
        notes.append(
            f"{len(signatures) - len(usable)} of the {len(signatures)} entries in "
            f"{target.name} carry no all_of strings to match on, so they can never "
            "match anything and were skipped."
        )
    if not usable:
        notes.append(
            f"{target.name} yielded no usable signature, so every failing build in "
            "this run reads as matching nothing recorded."
        )
    return usable, notes


def parse_pytest_html(html: str) -> list[dict[str, Any]]:
    """Pull the failing results out of a pytest-html v4 report.

    Every result sits in a `data-jsonblob` attribute. v4 writes it base64-encoded;
    older layouts write the JSON inline. Both are tried, and anything else yields
    no tests, which the caller renders as `unclassified` -- calling an unfetchable
    report a flake is the one outcome worse than saying nothing.
    """
    match = re.search(r'data-jsonblob="([^"]*)"', html)
    if not match:
        return []
    raw = match.group(1)
    payload: Any = None
    for decode in (
        lambda value: json.loads(base64.b64decode(value).decode("utf-8")),
        lambda value: json.loads(value.replace("&quot;", '"').replace("&amp;", "&")),
    ):
        try:
            payload = decode(raw)
            break
        except Exception:  # noqa: BLE001, S112 - any decoding failure means unclassified
            continue
    if not isinstance(payload, dict):
        return []
    failing: list[dict[str, Any]] = []
    for test_id, entries in payload.items():
        for entry in entries if isinstance(entries, list) else [entries]:
            if not isinstance(entry, dict):
                continue
            result = str(entry.get("result") or "").lower()
            if result not in {"failed", "error"}:
                continue
            record = dict(entry)
            record.setdefault("testId", test_id)
            failing.append(record)
    return failing


# ------------------------------------------------------------- delta and rank


CHANGE_RANKS = (
    "landed",
    "conflicted",
    "ci_red",
    "approved",
    "lgtm",
    "cla",
    "stale_ci",
    "ci_green",
    "comments",
    "push",
)


def compute_delta(row: dict[str, Any]) -> list[tuple[str, str]]:
    """Every reportable change on one row, as (kind, human phrasing).

    The whole delta engine is `field != reported_field`. It needs no timestamps,
    and it is automatically correct across a missed run, because `reported_*` is whatever the reader was last told however long ago
    that was.
    """
    if row.get("lifecycle") in {"retired", "ignored"}:
        return []
    if row.get("refresh_ok") is False:
        return []
    changes: list[tuple[str, str]] = []
    status = derive_status(row)
    reported_status = row.get("reported_status")

    if status == "landed" and reported_status != "landed":
        changes.append(("landed", "landed on GitCode"))
    if row.get("conflicted") and not row.get("reported_conflicted"):
        changes.append(("conflicted", "now conflicted"))
    elif not row.get("conflicted") and row.get("reported_conflicted"):
        changes.append(("conflicted", "no longer conflicted"))

    verdict = row.get("ci_verdict")
    reported_verdict = row.get("reported_ci_verdict")
    if verdict != reported_verdict:
        if verdict == "fail":
            hint = row.get("ci_failure_hint") or "unclassified"
            changes.append(("ci_red", f"CI failed ({_hint_phrase(hint, row)})"))
        elif verdict == "pass":
            changes.append(("ci_green", "CI green"))
        elif verdict == "running":
            changes.append(("ci_green", "CI running"))
    elif verdict == "fail" and row.get("ci_failure_hint") != row.get(
        "reported_ci_failure_hint"
    ):
        hint = row.get("ci_failure_hint") or "unclassified"
        changes.append(("ci_red", f"CI still failing ({_hint_phrase(hint, row)})"))

    # Both directions, because the mirror is only written for a row that
    # appeared in a report: a one-directional check leaves `reported_approved`
    # stuck at True after a dismissal, and whether the next dismissal is ever
    # reported then depends on an unrelated delta happening to launder the
    # mirror in between. A dismissal is also news in its own right -- branch
    # protection drops a stale approval on every push, and a reader told
    # "approved" and never told otherwise goes on believing it.
    if bool(row.get("approved")) != bool(row.get("reported_approved")):
        state = "approved" if row.get("approved") else "approval dismissed"
        changes.append(("approved", state))
    lgtm = int(row.get("lgtm_count") or 0)
    reported_lgtm = int(row.get("reported_lgtm_count") or 0)
    if lgtm != reported_lgtm:
        changes.append(("lgtm", f"lgtm {reported_lgtm} -> {lgtm}"))
    if (
        row.get("cla_ok") is not None
        and row.get("reported_cla_ok") is not None
        and bool(row.get("cla_ok")) != bool(row.get("reported_cla_ok"))
    ):
        state = "satisfied" if row.get("cla_ok") else "no longer satisfied"
        changes.append(("cla", f"CLA {state}"))
    if bool(row.get("stale_ci")) != bool(row.get("reported_stale_ci")):
        # Also both directions, and for the mirror's sake more than the
        # recovery's: while `reported_stale_ci` stays True the warning cannot be
        # raised a second time, so a later push whose CI has not caught up goes
        # unmentioned unless something unrelated cleared the mirror first.
        state = (
            "CI result is older than the current head"
            if row.get("stale_ci")
            else "CI has caught up with the current head"
        )
        changes.append(("stale_ci", state))

    comments = int(row.get("comment_count") or 0)
    reported_comments = int(row.get("reported_comment_count") or 0)
    if comments > reported_comments and row.get("reported_comment_count") is not None:
        delta = comments - reported_comments
        changes.append(("comments", f"{delta} new comment{'s' if delta > 1 else ''}"))
    head = row.get("head_sha")
    if head and row.get("reported_head_sha") and head != row.get("reported_head_sha"):
        changes.append(("push", "author pushed"))

    if status == "closed" and reported_status != "closed":
        changes.append(("landed", "closed without landing"))
    if status == "gone" and reported_status != "gone":
        changes.append(("landed", "no longer visible on the tracker"))
    return changes


def _hint_phrase(hint: str, row: dict[str, Any]) -> str:
    count = int(row.get("ci_failing_test_count") or 0)
    if hint == "matches_known_flake":
        names = ", ".join(row.get("ci_flake_signatures") or []) or "known flake"
        return f"matches the known {names} signature -> retrigger or raise with maintainers"
    if hint == "unrelated_to_known_flake":
        tests = f"{count} failing test{'s' if count != 1 else ''}"
        return f"{tests}, no known-flake signature -> likely needs rework"
    return "could not classify (report unavailable)"


def rank_of(changes: list[tuple[str, str]]) -> int:
    ranks = [CHANGE_RANKS.index(kind) for kind, _ in changes if kind in CHANGE_RANKS]
    return min(ranks) if ranks else len(CHANGE_RANKS)


def is_terminal(row: dict[str, Any]) -> bool:
    """Terminal is decided GitCode-first.

    A GitHub PR here is routinely closed for reasons unrelated to whether the
    change landed, so a merged GitCode MR is the meaningful terminal event.
    """
    if row.get("mr_state") == "merged" or row.get("merged_at"):
        return True
    if row.get("mr_state") == "closed":
        return True
    if row.get("kind") == "issue":
        return row.get("gh_state") == "closed"
    if row.get("mr_iid"):
        return False
    return row.get("gh_state") == "closed"


def apply_rotation(row: dict[str, Any], when: str) -> str | None:
    """active -> terminal_pending -> retired, one report apiece."""
    lifecycle = row.get("lifecycle")
    if lifecycle == "active" and is_terminal(row) and row.get("refresh_ok") is not False:
        row["lifecycle"] = "terminal_pending"
        row["terminal_observed_utc"] = when
        return "terminal_pending"
    if lifecycle == "terminal_pending":
        if not is_terminal(row):
            # Reopened before it was ever retired.
            row["lifecycle"] = "active"
            row["terminal_observed_utc"] = None
            row["reopen_count"] = int(row.get("reopen_count") or 0) + 1
            return "reopened"
        row["lifecycle"] = "retired"
        row["retired_at_utc"] = when
        return "retired"
    if lifecycle == "retired" and not is_terminal(row) and row.get("refresh_ok"):
        row["lifecycle"] = "active"
        row["retired_at_utc"] = None
        row["terminal_observed_utc"] = None
        row["reopen_count"] = int(row.get("reopen_count") or 0) + 1
        return "reopened"
    return None


def should_resurface(row: dict[str, Any], run_index: int, every: int) -> bool:
    """Stop a long-red item going quiet forever.

    Counted in completed runs, not days: the no-timeframes rule applies here too.
    """
    status = derive_status(row)
    if status not in ATTENTION_STATUSES and not row.get("conflicted"):
        return False
    last = row.get("last_mentioned_run")
    if last is None:
        return False
    return (run_index - int(last)) >= every


def build_receipt(
    reported_keys: Iterable[str],
    rows_by_key: dict[str, dict[str, Any]],
    cutoff: str,
    run_index: int,
) -> dict[str, Any]:
    """What `commit` will copy forward once the report has actually been posted."""
    receipt: dict[str, Any] = {}
    for key in reported_keys:
        row = rows_by_key.get(key)
        if row is None or row.get("refresh_ok") is False:
            continue
        entry = {f"reported_{name}": row.get(name) for name in REPORTED_FIELDS}
        entry["reported_status"] = derive_status(row)
        entry["reported_cla_ok"] = row.get("cla_ok")
        entry["reported_stale_ci"] = row.get("stale_ci")
        entry["reported_ci_failure_hint"] = row.get("ci_failure_hint")
        entry["observed_through_utc"] = cutoff
        entry["last_mentioned_run"] = run_index
        receipt[key] = entry
    return receipt


def apply_receipt(ledger: Ledger, receipt: dict[str, Any], when: str) -> list[str]:
    """Copy field -> reported_field and rotate, after delivery."""
    table = ledger.index()
    applied: list[str] = []
    for key, entry in receipt.items():
        row = table.get(key)
        if row is None:
            continue
        row.update(entry)
        apply_rotation(row, when)
        applied.append(key)
    return applied


# -------------------------------------------------------------- author watch


@dataclass(frozen=True)
class WatchTarget:
    """One search to run: one author, in one repository."""

    login: str
    repo: str
    kinds: tuple[str, ...]
    state: str

    @property
    def label(self) -> str:
        return f"{self.login} in {self.repo}"


def _watch_str_list(value: Any, field: str, allowed: tuple[str, ...] | None = None) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError(
            f"the configuration's `watch.{field}` must be a list of strings, not "
            f"{type(value).__name__}."
        )
    items = [item.strip() for item in value if item.strip()]
    if allowed:
        unknown = [item for item in items if item not in allowed]
        if unknown:
            raise ConfigError(
                f"the configuration's `watch.{field}` has {', '.join(unknown)}, "
                f"which is not one of {', '.join(allowed)}."
            )
    return items


def plan_watch_targets(
    config: dict[str, Any] | None,
    *,
    cli_authors: list[str] | None,
    cli_repos: list[str] | None,
    cli_kinds: list[str] | None,
    cli_state: str | None,
    default_repos: Iterable[str],
) -> tuple[list[WatchTarget], list[str], list[str]]:
    """Expand the list into one search per author per repository.

    Returns the targets, the logins that were listed but switched off, and notes.

    One search per author is not an optimisation left on the table: the search
    endpoint has no author disjunction, and a single unsearchable login fails the
    *whole* query with a 422. Batching would therefore turn one bad login into
    silence about everybody, which is precisely the failure this command is meant
    to make impossible.
    """
    config = config or {}
    notes: list[str] = []
    disabled: list[str] = []

    base_repos = _watch_str_list(config.get("repos", []), "repos")
    base_kinds = _watch_str_list(config.get("kinds", []), "kinds", WATCH_KINDS)
    base_state = config.get("state") or ""
    if base_state and base_state not in WATCH_STATES:
        raise ConfigError(
            f"the configuration's `watch.state` is {base_state!r}, not one of "
            f"{', '.join(WATCH_STATES)}."
        )

    entries = config.get("authors", [])
    if entries and not isinstance(entries, list):
        raise ConfigError("the configuration's `watch.authors` must be a list.")
    if cli_authors:
        # --author replaces the file's authors rather than adding to them, so a
        # one-off or a test run never has to edit the operator's file and can
        # never accidentally sweep the whole list as well.
        entries = list(cli_authors)
        notes.append(
            "--author was given, so the configured authors were not used for "
            "this run."
        )

    targets: list[WatchTarget] = []
    seen: set[tuple[str, str]] = set()
    for entry in entries or []:
        if isinstance(entry, str):
            entry = {"login": entry}
        if not isinstance(entry, dict):
            raise ConfigError(
                "each entry in `watch.authors` must be a login or an object "
                f"with a login, not {type(entry).__name__}."
            )
        login = str(entry.get("login") or "").strip().lstrip("@")
        if not login:
            raise ConfigError("an entry in `watch.authors` has no login.")
        if entry.get("enabled") is False:
            disabled.append(login)
            continue
        repos = (
            cli_repos
            or _watch_str_list(entry.get("repos", []), "authors[].repos")
            or base_repos
            or list(default_repos)
        )
        kinds = (
            cli_kinds
            or _watch_str_list(entry.get("kinds", []), "authors[].kinds", WATCH_KINDS)
            or base_kinds
            or list(WATCH_KINDS)
        )
        state = cli_state or str(entry.get("state") or "") or base_state or "all"
        if state not in WATCH_STATES:
            raise ConfigError(
                f"the watch entry for {login} has state {state!r}, not one of "
                f"{', '.join(WATCH_STATES)}."
            )
        if not repos:
            raise ConfigError(
                f"there is no repository to search for {login}. The watch "
                "searches the configured `repositories` unless `watch.repos` or "
                "--repo narrows it, so configure a repository or name one. A "
                "watch with no repository is not a search of everything; it is a "
                "search of nothing."
            )
        for repo in repos:
            if (login.lower(), repo.lower()) in seen:
                continue
            seen.add((login.lower(), repo.lower()))
            targets.append(
                WatchTarget(
                    login=login,
                    repo=repo,
                    kinds=tuple(sorted(set(kinds), key=WATCH_KINDS.index)),
                    state=state,
                )
            )
    return targets, disabled, notes


def watch_closed_since(window_days: Any, override: str | None, *, now: datetime) -> str | None:
    """Return the lower date bound for the watch's closed-item search, or ``None`` for no bound. This is a registration window only; report inclusion remains controlled by the watermark. Accept a day count, an ISO date, or ``all``."""
    value = override if override is not None else window_days
    if value is None:
        value = DEFAULT_WATCH_WINDOW_DAYS
    text = str(value).strip().lower()
    if text in {"all", "0", ""}:
        return None
    match = re.fullmatch(r"(\d+)d?", text)
    if match:
        return (now - timedelta(days=int(match.group(1)))).date().isoformat()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return text
    raise ConfigError(
        f"{value!r} is not a window. Give a number of days (`14`, `14d`), an ISO "
        "date (`2026-08-01`), or `all` for no floor -- and know that `all` asks "
        "for the author's entire closed history, which stops at "
        f"{GITHUB_SEARCH_RESULT_CAP} results with nothing to say that it did."
    )


def build_search_queries(
    target: WatchTarget, closed_since: str | None
) -> list[tuple[str, str]]:
    """Build the open and closed searches for one watch target. The open search has no date bound so it includes all current work. The closed search uses ``closed:`` and the configured floor to catch items opened and closed between runs without backfilling older history. Omitting the kind qualifier searches issues and pull requests together."""
    base = [f"repo:{target.repo}", f"author:{target.login}"]
    if len(target.kinds) == 1:
        base.append("is:pr" if target.kinds[0] == "pull_request" else "is:issue")
    queries = [("open", " ".join([*base, "is:open"]))]
    if target.state != "open":
        closed = [*base, "is:closed"]
        if closed_since:
            closed.append(f"closed:>={closed_since}")
        queries.append(("closed", " ".join(closed)))
    return queries


class SearchRefused(RuntimeError):
    """The endpoint refused the query because it will not search that login.

    Kept apart from "no results" on purpose, and it is the whole reason this
    command reports per author rather than in total. An unknown login, a
    renamed one, a suspended one and a machine account addressed by the wrong
    spelling all come back as this -- and every one of them otherwise looks
    exactly like an author who happened not to open anything.
    """


class SearchRateLimited(RuntimeError):
    """The search budget for this window is spent."""

    def __init__(self, message: str, reset_epoch: float | None = None) -> None:
        super().__init__(message)
        self.reset_epoch = reset_epoch


# ------------------------------------------------------------------- network


def http_get_full(
    url: str, headers: dict[str, str], timeout: int = 30
) -> tuple[int, dict[str, str], bytes]:
    """A GET that also hands back the response headers.

    The search endpoint's rate-limit state is only in the headers, and a run that
    cannot read them can only discover the wall by hitting it.
    """
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **headers})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as error:
        return error.code, dict(error.headers or {}), error.read(4096)
    except urllib.error.URLError as error:
        raise RuntimeError(f"request failed: {error.reason}") from error


def http_get(url: str, headers: dict[str, str], timeout: int = 30) -> tuple[int, bytes]:
    status, _, body = http_get_full(url, headers, timeout)
    return status, body


def github_json(url: str, token: str) -> Any:
    status, body = http_get(
        url,
        {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
            "Authorization": f"Bearer {token}",
        },
    )
    if status == 404:
        raise NotFound(url)
    if status >= 400:
        raise RuntimeError(
            f"GitHub API returned HTTP {status} for "
            f"{url.split('://', 1)[-1].split('?', 1)[0]}"
        )
    return json.loads(body.decode("utf-8"))


def github_search(
    query: str,
    token: str,
    api_base: str,
    *,
    page: int = 1,
    per_page: int = WATCH_PAGE_SIZE,
) -> tuple[dict[str, Any], dict[str, str]]:
    """One page of the issue/pull-request search, with its rate-limit headers.

    Deliberately not `github_json`: this endpoint has its own budget, its own
    refusal for a login it will not search, and a result cap, none of which the
    per-item fetches have. Folding it into the general client would mean every
    caller of that client inheriting checks that are meaningless for it.
    """
    url = (
        f"{api_base.rstrip('/')}/search/issues"
        f"?per_page={int(per_page)}&page={int(page)}&sort=created&order=desc"
        f"&q={urllib.parse.quote(query)}"
    )
    status, headers, body = http_get_full(
        url,
        {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
            "Authorization": f"Bearer {token}",
        },
    )
    if status == 200:
        return json.loads(body.decode("utf-8")), headers

    try:
        payload = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        payload = {}
    detail = " ".join(
        str(entry.get("message") or "")
        for entry in payload.get("errors") or []
        if isinstance(entry, dict)
    ).strip() or str(payload.get("message") or "")

    remaining = headers.get("X-RateLimit-Remaining")
    reset = headers.get("X-RateLimit-Reset")
    if status in {403, 429} and (remaining == "0" or "rate limit" in detail.lower()):
        raise SearchRateLimited(
            detail or "the search rate limit is exhausted",
            float(reset) if reset and reset.isdigit() else None,
        )
    if status == 422:
        raise SearchRefused(detail or "the search query was rejected")
    raise RuntimeError(f"the search endpoint returned HTTP {status}: {detail}")


class RequestRate:
    """Coordinate a request ceiling across workers for one tracker. ``wait`` records each allowed request and waits until the oldest call leaves the window. It returns ``False`` if waiting would exceed the run deadline. A remote rate-limit refusal fills the local window so other workers wait instead of repeating the refusal."""

    def __init__(self, per_window: int, window_seconds: float) -> None:
        self.per_window = max(1, int(per_window))
        self.window_seconds = float(window_seconds)
        self._calls: deque[float] = deque()
        self._lock = threading.Lock()

    def wait(self, deadline: float | None = None) -> bool:
        while True:
            with self._lock:
                now = time.monotonic()
                while self._calls and now - self._calls[0] >= self.window_seconds:
                    self._calls.popleft()
                if len(self._calls) < self.per_window:
                    self._calls.append(now)
                    return True
                sleep_for = self.window_seconds - (now - self._calls[0])
            if deadline is not None and time.monotonic() + sleep_for >= deadline:
                return False
            time.sleep(min(sleep_for, self.window_seconds) + 0.05)

    def penalise(self) -> None:
        """Treat the window as spent, after the remote said it was.

        A refusal means the count on this side is wrong -- another run sharing
        the credential, or a ceiling narrower than the one configured -- so the
        window is filled rather than incremented. The next caller waits it out
        instead of walking into the same wall one request at a time.
        """
        with self._lock:
            now = time.monotonic()
            self._calls = deque([now] * self.per_window)


# GitCode publishes its anonymous ceiling nowhere except in the body of the
# refusal -- "Threshold: 50 times per user per Minute" -- and the refusal carries
# no Retry-After and no rate-limit header of any kind, so there is nothing to
# read on the way to it and nothing to read once there. Staying inside it means
# counting on this side, and counting under it rather than at it: the window is
# the remote's, so ours cannot be aligned with it, and a client pacing itself at
# exactly the ceiling crosses it whenever the two windows overlap.
GITCODE_REQUESTS_PER_MINUTE = 40
GITCODE_RATE_WINDOW_SECONDS = 60.0
# One retry, not a series. The window is a minute, so a second refusal means
# waiting most of a minute again, and a run has a budget it is spending.
GITCODE_RETRY_ATTEMPTS = 2
gitcode_rate = RequestRate(GITCODE_REQUESTS_PER_MINUTE, GITCODE_RATE_WINDOW_SECONDS)


def gitcode_json(url: str, deadline: float | None = None) -> Any:
    """Anonymous read, paced to the ceiling the tracker enforces.

    GitCode needs no credential, and no credential raises the limit: the
    threshold it names is per user and an anonymous caller is one user, so
    every run of every deployment sharing an address shares one budget.
    """
    for attempt in range(GITCODE_RETRY_ATTEMPTS):
        if not gitcode_rate.wait(deadline):
            raise RuntimeError(
                "GitCode's rate limit would be exceeded and waiting for it to "
                "clear would run past this run's refresh budget"
            )
        status, body = http_get(url, {"Accept": "application/json"})
        if status == 429:
            # The count on this side was wrong. Spend the window and retry once;
            # a second refusal is reported as the item's refresh error, which is
            # the truth about it and what the next run retries from.
            gitcode_rate.penalise()
            if attempt + 1 < GITCODE_RETRY_ATTEMPTS:
                continue
            raise RuntimeError(
                "GitCode refused the request as too frequent (HTTP 429). Its "
                "ceiling is per user and per minute, so it is shared by "
                "everything reading it from here, and it is not raised by any "
                "credential"
            )
        if status == 404:
            raise NotFound(url)
        if status >= 400:
            raise RuntimeError(f"GitCode API returned HTTP {status}")
        return json.loads(body.decode("utf-8"))
    raise RuntimeError("GitCode refused the request as too frequent (HTTP 429)")


class NotFound(RuntimeError):
    pass


def resolve_token(token_env: str) -> str:
    """Read the token from the environment. Never print it, never store it.

    The variable name stays configurable; `GITHUB_TOKEN` is the default.
    Deliberately not `gh`: this token is more restricted than the account's
    `gh` credentials, and shelling out would silently widen the grant to whatever
    the CLI is authenticated as.

    Missing is a hard stop, never a downgrade to anonymous: 60 requests an hour
    would exhaust partway through a run and produce a half-refreshed report that
    looks complete.
    """
    token = os.environ.get(token_env, "").strip()
    if not token:
        raise SystemExit(
            f"{token_env} is not set in this environment. Refusing to fall back to "
            "anonymous GitHub access: the 60/hour limit would run out partway "
            "through a refresh and the report would look complete while being "
            "half stale. Set the variable, or name another with --token-env."
        )
    return token


def gitcode_project_for(
    repo: str, override: str | None, repositories: Repositories = NO_REPOSITORIES
) -> str | None:
    """The second tracker's project for this repository, or None for neither.

    None is a supported answer and not a failure: the item is refreshed from the
    first tracker alone. `--gitcode-project` overrides the configuration for one
    run, which is how a pairing is checked before it is written down.
    """
    if override:
        return override
    return repositories.gitcode_for(repo)


def encode_repo(repo: str) -> str:
    return "/".join(urllib.parse.quote(part, safe="") for part in repo.split("/"))


def fetch_github_facts(row: dict[str, Any], token: str, api_base: str) -> dict[str, Any]:
    repo = row.get("repo") or ""
    number = row.get("number")
    base = f"{api_base.rstrip('/')}/repos/{encode_repo(repo)}"
    facts: dict[str, Any] = {}
    issue = github_json(f"{base}/issues/{number}", token)
    labels = [
        label.get("name") if isinstance(label, dict) else str(label)
        for label in issue.get("labels") or []
    ]
    facts.update(
        {
            "title_source": issue.get("title") or "",
            "author_login": (issue.get("user") or {}).get("login"),
            "created_at": issue.get("created_at"),
            "gh_state": issue.get("state"),
            "comment_count": int(issue.get("comments") or 0),
            "github_labels": labels,
            "conflicted": any(name.lower() == "conflicted" for name in labels),
            "last_activity_utc": issue.get("updated_at"),
            "closes": declared_closes(str(issue.get("body") or ""), repo),
        }
    )
    if row.get("kind") == "pull_request":
        pull = github_json(f"{base}/pulls/{number}", token)
        facts.update(
            {
                "head_sha": ((pull.get("head") or {}).get("sha") or "")[:40] or None,
                "draft": bool(pull.get("draft")),
                "gh_merged_at": pull.get("merged_at"),
            }
        )
        facts.update(_ci_writeback(base, number, facts["comment_count"], token))
    return facts


def _ci_writeback(base: str, number: Any, comment_count: int, token: str) -> dict[str, Any]:
    """Find the newest `<!-- bot2-ci-writeback -->` comment.

    It carries `head_sha:` and the artifact links, which is where `stale_ci` and
    the classifier's input come from. Only the last page of comments is fetched,
    so this is one request however long the discussion is.
    """
    if comment_count <= 0:
        return {}
    last_page = max((comment_count + 99) // 100, 1)
    try:
        comments = github_json(
            f"{base}/issues/{number}/comments?per_page=100&page={last_page}", token
        )
    except (RuntimeError, NotFound):
        return {}
    for comment in reversed(comments if isinstance(comments, list) else []):
        body = str(comment.get("body") or "")
        if "bot2-ci-writeback" not in body:
            continue
        sha = re.search(r"head_sha:\s*([0-9a-f]{7,40})", body)
        artifact = re.search(r"(https?://\S*?unit_test_report\.html)", body)
        return {
            "ci_head_sha": sha.group(1) if sha else None,
            "ci_artifact_url": artifact.group(1) if artifact else None,
        }
    return {}


class ProjectListing:
    """Hold one run's merge-request listing and indexes by IID, source branch, and head SHA. ``exhausted`` is true only when the listing reaches its end. Only an exhausted listing can establish that an item is absent; otherwise, unanswered rows must use the per-item lookup."""

    def __init__(self, project: str, base: str, max_pages: int) -> None:
        self.project = project
        self.base = base
        self.max_pages = max_pages
        self.pages_read = 0
        self.exhausted = False
        self.error: str | None = None
        self.by_iid: dict[int, dict[str, Any]] = {}
        self.by_branch: dict[str, int] = {}
        self.by_sha: dict[str, int] = {}

    def ingest(self, entries: Iterable[Any]) -> None:
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            raw = entry.get("iid") or entry.get("number")
            try:
                iid = int(raw)
            except (TypeError, ValueError):
                continue
            self.by_iid.setdefault(iid, entry)
            branch = str(entry.get("source_branch") or "")
            if branch:
                self.by_branch.setdefault(branch, iid)
            sha = str((entry.get("head") or {}).get("sha") or "")
            if sha:
                self.by_sha.setdefault(sha, iid)

    def entry_for(self, iid: Any) -> dict[str, Any] | None:
        try:
            return self.by_iid.get(int(iid))
        except (TypeError, ValueError):
            return None

    def iid_for(self, row: dict[str, Any]) -> int | None:
        """The same match the per-row search makes: branch first, head sha second."""
        found = self.by_branch.get(f"github-pr-{row.get('number')}")
        if found:
            return found
        sha = str(row.get("head_sha") or "")
        return self.by_sha.get(sha) if sha else None

    def pending_reads(self, rows: list[dict[str, Any]]) -> int:
        """How many of `rows` name a merge request this listing has not reached.

        Each is one single-merge-request read if the listing never reaches it,
        which is the unit a page is priced against.
        """
        return sum(
            1
            for row in rows
            if row.get("mr_iid") and self.entry_for(row.get("mr_iid")) is None
        )

    def unresolved_pairings(self, rows: list[dict[str, Any]], recheck_hours: int) -> int:
        """How many of `rows` are still looking for a merge request at all.

        Priced apart from the reads above and never traded off against them: a
        row whose pairing is unknown pages this listing from end to end by
        itself if this phase stops early, so stopping early to spare it a page
        hands it every page back, privately, and once per such row.
        """
        return sum(
            1
            for row in rows
            if not row.get("mr_iid")
            and recorded_absence(row, self.project, recheck_hours) is None
            and self.iid_for(row) is None
        )


# A page of the listing carries a hundred merge requests and costs about what
# two single merge requests cost to read -- measured, and the ratio the stopping
# rule below is priced against.
LISTING_PAGE_COST = 2
LISTING_PAGE_SIZE = 100


def read_project_listing(
    project: str,
    api_base: str,
    max_pages: int,
    rows: list[dict[str, Any]],
    *,
    recheck_hours: int = 0,
    deadline: float | None = None,
) -> ProjectListing:
    """Read project listing pages while their expected cost does not exceed the per-item reads they can replace. Continue to the end while any row still needs pairing discovery. Stop at the run deadline without treating unread pages as evidence of absence. Never raise for listing failures; unanswered rows fall back to per-item reads."""
    base = f"{api_base.rstrip('/')}/repos/{project}"
    listing = ProjectListing(project, base, max_pages)
    for _ in range(max(0, int(max_pages))):
        pending = listing.pending_reads(rows)
        if not listing.unresolved_pairings(rows, recheck_hours):
            pages_left = max(0, int(max_pages)) - listing.pages_read
            if pending < LISTING_PAGE_COST or pages_left * LISTING_PAGE_COST > pending:
                return listing
        if deadline is not None and time.monotonic() >= deadline:
            # Same rule as the per-row search: a listing cut short by the budget
            # is not evidence that anything is missing from it.
            return listing
        try:
            payload = gitcode_json(
                f"{base}/pulls?state=all&sort=created&direction=desc"
                f"&per_page={LISTING_PAGE_SIZE}&page={listing.pages_read + 1}",
                deadline,
            )
        except (RuntimeError, NotFound) as error:
            listing.error = str(error)
            return listing
        listing.pages_read += 1
        entries = payload if isinstance(payload, list) else payload.get("items") or []
        if not entries:
            listing.exhausted = True
            return listing
        listing.ingest(entries)
    listing.exhausted = True
    return listing


def read_project_listings(
    rows: list[dict[str, Any]],
    *,
    gitcode_api: str,
    repositories: Repositories,
    project_override: str | None,
    max_pages: int,
    recheck_hours: int,
    workers: int,
    deadline: float | None = None,
) -> dict[str, ProjectListing]:
    """One listing per project the run's rows actually need, read in parallel."""
    wanted: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        if row.get("kind") != "pull_request" or row.get("gone"):
            continue
        if row.get("tracker") == "gitcode":
            continue
        project = gitcode_project_for(
            row.get("repo") or "", project_override, repositories
        )
        if project:
            wanted.setdefault(project, []).append(row)
    if not wanted:
        return {}

    def read(project: str) -> tuple[str, ProjectListing]:
        return project, read_project_listing(
            project,
            gitcode_api,
            max_pages,
            wanted[project],
            recheck_hours=recheck_hours,
            deadline=deadline,
        )

    if len(wanted) == 1 or workers <= 1:
        return dict(read(project) for project in wanted)
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=min(workers, len(wanted)), thread_name_prefix="pr-tracker-listing"
    ) as pool:
        return dict(pool.map(read, list(wanted)))


def recorded_absence(
    row: dict[str, Any],
    project: str,
    recheck_hours: int,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Return a recorded ``pairing: none`` only while it names the current project and is younger than ``recheck_hours``. Pairing configuration can change, and a GitCode mirror can appear later, so old or undated findings must be established again."""
    if row.get("pairing") != "none":
        return None
    if (row.get("pairing_project") or project) != project:
        return None
    checked = parse_utc(row.get("pairing_checked_utc"))
    if checked is None:
        return None
    if (now or now_utc()) - checked >= timedelta(hours=max(0, int(recheck_hours))):
        return None
    return {
        "pairing": "none",
        "pairing_project": project,
        "pairing_checked_utc": row.get("pairing_checked_utc"),
    }


def absence_facts(project: str, now: datetime | None = None) -> dict[str, Any]:
    """What is written down when a search establishes there is no merge request."""
    return {
        "pairing": "none",
        "pairing_project": project,
        "pairing_checked_utc": format_utc(now or now_utc()),
    }


def fetch_gitcode_facts(
    row: dict[str, Any],
    project: str,
    api_base: str,
    max_pages: int,
    deadline: float | None = None,
    listing: ProjectListing | None = None,
    recheck_hours: int = DEFAULT_PAIRING_RECHECK_HOURS,
) -> dict[str, Any]:
    base = f"{api_base.rstrip('/')}/repos/{project}"
    iid = row.get("mr_iid")
    if not iid:
        held = recorded_absence(row, project, recheck_hours)
        if held is not None:
            return held
        iid = None
        if listing is not None:
            iid = listing.iid_for(row)
            if iid is None and not listing.exhausted:
                # The listing stopped before the end, so it is not evidence of
                # absence -- only the pages it did read say anything at all.
                iid = discover_pairing(row, base, max_pages, deadline)
        else:
            iid = discover_pairing(row, base, max_pages, deadline)
        if not iid:
            return absence_facts(project)
    entry = listing.entry_for(iid) if listing is not None else None
    payload = entry if entry is not None else gitcode_json(f"{base}/pulls/{iid}", deadline)
    return {
        "pairing": "github-pr-branch",
        "mr_iid": iid,
        "pairing_project": project,
        **_gitcode_fields(payload, iid),
    }


def _gitcode_fields(payload: dict[str, Any], iid: Any) -> dict[str, Any]:
    labels = [
        label.get("name") if isinstance(label, dict) else str(label)
        for label in payload.get("labels") or []
    ]
    facts = {
        "mr_state": "merged" if payload.get("merged_at") else payload.get("state"),
        "merged_at": payload.get("merged_at"),
        "mr_head_sha": ((payload.get("head") or {}).get("sha") or "") or None,
        "mr_source_branch": payload.get("source_branch"),
        "mr_url": payload.get("html_url")
        or f"https://gitcode.com/merge_requests/{iid}",
        "gitcode_labels": labels,
    }
    facts.update(labels_to_facts(labels))
    if payload.get("mergeable") is False:
        facts["conflicted_gitcode"] = True
    return facts


def discover_pairing(
    row: dict[str, Any], base: str, max_pages: int, deadline: float | None = None
) -> int | None:
    """Find the merge request created for a pull request by paging the project listing and matching ``github-pr-<N>`` first, then the head SHA. Record a found IID for later direct reads. Record absence only after the search completes; a deadline raises because unread pages may contain the match."""
    wanted_branch = f"github-pr-{row.get('number')}"
    wanted_sha = row.get("head_sha")
    for page in range(1, max_pages + 1):
        if deadline is not None and time.monotonic() >= deadline:
            # The one loop inside a single row's refresh that can run for
            # minutes on its own, and so the one place a run's time bound has to
            # reach if it is to bound anything. Raised rather than returned as
            # "no pairing": the pages left unread might hold the merge request,
            # and recording an absence nobody established would be written into
            # the store and believed by every run after this one.
            raise RuntimeError(
                "pairing discovery stopped at the run's refresh budget after "
                f"{page - 1} of {max_pages} page(s); the pairing is unknown, "
                "not absent"
            )
        payload = gitcode_json(
            f"{base}/pulls?state=all&sort=created&direction=desc"
            f"&per_page=100&page={page}",
            deadline,
        )
        entries = payload if isinstance(payload, list) else payload.get("items") or []
        if not entries:
            return None
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            branch = str(entry.get("source_branch") or "")
            sha = str((entry.get("head") or {}).get("sha") or "")
            if branch == wanted_branch or (wanted_sha and sha and sha == wanted_sha):
                iid = entry.get("iid") or entry.get("number")
                return int(iid) if iid else None
    return None


def refresh_row(
    row: dict[str, Any],
    *,
    token: str,
    github_api: str,
    gitcode_api: str,
    gitcode_project_override: str | None,
    max_pages: int,
    signatures: list[dict[str, Any]],
    fetch_artifact: bool,
    repositories: Repositories = NO_REPOSITORIES,
    deadline: float | None = None,
    listings: dict[str, ProjectListing] | None = None,
    recheck_hours: int = DEFAULT_PAIRING_RECHECK_HOURS,
) -> None:
    """Refresh one row in place. Never touches `reported_*`.

    `deadline` bounds the paging inside this row rather than the row itself. A
    refresh already begun is always finished -- half an item's facts written
    into the store would be worse than a slow run -- but the pairing search is
    up to `max_pages` sequential requests, so without a bound reaching into it
    the overshoot past a run's budget is a whole row's worth of paging rather
    than a single request's.
    """
    row.pop("refresh_error", None)
    row["refresh_ok"] = True
    row["last_refresh_utc"] = format_utc(now_utc())
    facts_error: list[str] = []

    if row.get("tracker") == "github":
        try:
            row.update(fetch_github_facts(row, token, github_api))
            row["gone"] = False
        except NotFound:
            # 404 is deletion and "invisible to this token" at the same time, and
            # the token may well be narrower than the account that posted the
            # link, so this is reported as inaccessible rather than as deleted.
            row["gone"] = True
            row["gone_reason"] = "404 - deleted, or not visible to this token"
        except RuntimeError as error:
            facts_error.append(f"github: {error}")

    if row.get("tracker") == "gitcode":
        # A GitCode-first registration whose pairing is not yet known: read the
        # MR directly, which also yields the `github-pr-<N>` source branch that
        # rule 3 resolves the identity with.
        project = gitcode_project_override or row.get("repo") or ""
        try:
            payload = gitcode_json(
                f"{gitcode_api.rstrip('/')}/repos/{project}/pulls/{row.get('number')}",
                deadline,
            )
            row.update(_gitcode_fields(payload, row.get("number")))
            row["mr_iid"] = row.get("number")
            row.setdefault("title_source", payload.get("title") or "")
            row["comment_count"] = int(payload.get("comments") or row.get("comment_count") or 0)
            row["gone"] = False
        except NotFound:
            row["gone"] = True
            row["gone_reason"] = "404 on GitCode - deleted or renamed"
        except RuntimeError as error:
            facts_error.append(f"gitcode: {error}")
    else:
        project = gitcode_project_for(
            row.get("repo") or "", gitcode_project_override, repositories
        )
        if row.get("kind") == "pull_request" and project and not row.get("gone"):
            try:
                row.update(
                    fetch_gitcode_facts(
                        row,
                        project,
                        gitcode_api,
                        max_pages,
                        deadline,
                        listing=(listings or {}).get(project),
                        recheck_hours=recheck_hours,
                    )
                )
            except NotFound:
                # The merge request this row named is not there. That is an
                # established absence like any other, and dated like any other
                # so it is looked for again rather than believed forever.
                row.update(absence_facts(project))
            except RuntimeError as error:
                facts_error.append(f"gitcode: {error}")
        elif row.get("kind") == "pull_request" and not project:
            # No GitCode project is known for this repository, so the decision
            # state is simply unavailable -- said plainly rather than invented.
            # Re-derived from the configuration on every refresh and never read
            # back out of the row: `unmapped` is a fact about the configuration,
            # and the run that gains the mapping must stop saying it. The dated
            # absence above is read back; this is not, and the two are kept
            # apart deliberately.
            row["pairing"] = "unmapped"
            row.pop("pairing_checked_utc", None)
            row.pop("pairing_project", None)

    if row.get("conflicted_gitcode"):
        row["conflicted"] = True
    head = row.get("head_sha")
    ci_head = row.get("ci_head_sha")
    row["stale_ci"] = bool(head and ci_head and not head.startswith(ci_head))

    if row.get("ci_verdict") == "fail" and fetch_artifact:
        _classify_row_ci(row, signatures)
    elif row.get("ci_verdict") != "fail":
        row.pop("ci_failure_hint", None)
        row.pop("ci_flake_signatures", None)
        row.pop("ci_failing_test_count", None)

    if facts_error:
        row["refresh_ok"] = False
        row["refresh_error"] = "; ".join(facts_error)
    if (
        row.get("title_source")
        and not row.get("title_rendered")
        and not contains_foreign_script(str(row["title_source"]))
    ):
        # A title already in the output language needs no rendering step; one
        # that is not is listed in the report's Detail for a human to render.
        row["title_rendered"] = row["title_source"]


def _classify_row_ci(row: dict[str, Any], signatures: list[dict[str, Any]]) -> None:
    """Fetch and classify the failure report, at most once per failing push."""
    url = row.get("ci_artifact_url")
    sha = row.get("ci_head_sha")
    if row.get("ci_classified_sha") and row.get("ci_classified_sha") == sha:
        return
    if not url:
        row["ci_failure_hint"] = "unclassified"
        row["ci_failing_test_count"] = 0
        return
    try:
        status, body = http_get(url, {"Accept": "text/html"}, timeout=120)
    except RuntimeError:
        row["ci_failure_hint"] = "unclassified"
        return
    if status >= 400:
        row["ci_failure_hint"] = "unclassified"
        return
    failing = parse_pytest_html(body.decode("utf-8", errors="replace"))
    hint, matched = classify_ci_failure(failing, signatures)
    row["ci_failure_hint"] = hint
    row["ci_flake_signatures"] = matched
    row["ci_failing_test_count"] = len(failing)
    row["ci_classified_sha"] = sha


def refresh_within_budget(
    rows: list[dict[str, Any]],
    refresh: Callable[[dict[str, Any]], None],
    *,
    workers: int,
    deadline: float | None,
) -> list[dict[str, Any]]:
    """Refresh rows and return rows whose refresh never started. The worker limit bounds concurrency and the deadline bounds total work. In-flight refreshes finish so their facts are not partially written. The deadline also reaches pairing pagination. Callers must exclude unstarted rows from reports because their stored facts are stale."""
    if not rows:
        return []

    def attempt(row: dict[str, Any]) -> dict[str, Any] | None:
        if deadline is not None and time.monotonic() >= deadline:
            return row
        refresh(row)
        return None

    if workers <= 1:
        results = [attempt(row) for row in rows]
    else:
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(workers, len(rows)),
            thread_name_prefix="pr-tracker-refresh",
        ) as pool:
            results = list(pool.map(attempt, rows))
    return [row for row in results if row is not None]


# ------------------------------------------------------------------ rendering


def store_label(state_file: Path) -> str:
    """Return the store basename for output. Do not expose the absolute path, which is configuration input and can reveal a host's directory layout."""
    return state_file.name


def row_routes(row: dict[str, Any]) -> list[str]:
    """How this item got into the store, in the order the routes are named."""
    present = {str((source or {}).get("via") or "") for source in row.get("sources") or []}
    return [route for route in REGISTRATION_ROUTES if route in present]


def store_routes(rows: Iterable[dict[str, Any]]) -> list[str]:
    """Every route any active row arrived by.

    Read off the store rather than off the configuration, so the line the report
    prints is a description of what happened and not a claim about what was set
    up. A watch that is configured and never ran does not get to say it did.
    """
    present: set[str] = set()
    for row in rows:
        present.update(row_routes(row))
    return [route for route in REGISTRATION_ROUTES if route in present]


def describe_routes(routes: Iterable[str]) -> str:
    names = [REGISTRATION_ROUTES[route] for route in routes if route in REGISTRATION_ROUTES]
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + " and " + names[-1]


def item_label(row: dict[str, Any]) -> str:
    number = row.get("number")
    repo = row.get("repo") or ""
    short = repo.split("/")[-1] if repo else ""
    prefix = "#" if row.get("tracker") == "github" else "!"
    return f"{short} {prefix}{number}" if short else f"{prefix}{number}"


def item_title(row: dict[str, Any]) -> str:
    title = row.get("title_rendered") or row.get("title_source") or ""
    return str(title).strip() or "(no title)"


# GitHub's own closing keywords. Parsing these is not a guess about what a body
# "seems to mean": they are the exact words GitHub itself acts on to close an
# issue when a pull request merges, so a match is a link the author declared,
# not one inferred. Every other cross-reference -- "see #12", "related to #34",
# a bare "#56" -- is deliberately not matched, because a mention is not a claim
# and the false positives live entirely there.
_CLOSING_KEYWORDS = (
    "close", "closes", "closed",
    "fix", "fixes", "fixed",
    "resolve", "resolves", "resolved",
)
_CLOSES_RE = re.compile(
    r"\b(?:" + "|".join(_CLOSING_KEYWORDS) + r")\b\s*:?\s*"
    r"(?:https?://[^\s/]+/(?P<slug>[\w.-]+/[\w.-]+)/(?:issues|pull)/(?P<url_num>\d+)"
    r"|(?P<xrepo>[\w.-]+/[\w.-]+)?#(?P<num>\d+))",
    re.IGNORECASE,
)


def declared_closes(body: str, repo: str) -> list[str]:
    """Issue references a body declares this item closes, as ``owner/repo#N``.

    Order is preserved and duplicates removed. A reference with no explicit
    repository resolves against *repo*, which is how GitHub reads it.
    """
    seen: list[str] = []
    for match in _CLOSES_RE.finditer(body or ""):
        slug = match.group("slug") or match.group("xrepo") or repo
        number = match.group("url_num") or match.group("num")
        if not number:
            continue
        ref = f"{slug}#{int(number)}"
        if ref not in seen:
            seen.append(ref)
    return seen


# The columns, in order. Named once because three things have to agree about
# them: the header the table prints, the cells each row prints, and the width
# the pagination charges a row -- and a disagreement between the last two is
# invisible until a page silently breaches its budget.
TABLE_COLUMNS = ("Item", "Author", "What changed", "Status", "Title", "See also")

# Slack's Block Kit limits, and the host's rather than this skill's. The
# character budget is counted per message across every table in it, over the
# text each cell *shows*: a cell holding a link shows its label only, so a long
# URL is free and a long title is not. The row cap is per table and counts the
# header row, so 99 items is a full table. A message breaching either is not
# truncated -- the host declines to render the whole of it and every table in it
# arrives as raw pipe characters.
SLACK_TABLE_ROWS_PER_TABLE = 100

# What the roster is actually paged against, held under the host's own numbers
# on purpose. The count here is a model of the host's, and a model that is
# exact today is one release away from being wrong by a little; the failure it
# would cause is the whole message losing its formatting, while stopping a page
# early costs one row. The margin buys "one more page" instead of "a wall of
# pipe characters".
ROSTER_PAGE_CHARACTERS = 9000
ROSTER_PAGE_ROWS = SLACK_TABLE_ROWS_PER_TABLE - 1

# Past this the roster stops being rendered at all -- see `render_report`. It is
# not a limit of the mechanism, which would page indefinitely, but of what a
# reader can use.
ROSTER_MAX_PAGES = 4

# Block Kit's carousel and card limits, not this skill's. A carousel
# holds 1-10 cards; `title` and `subtitle` cap at 150 characters, `body` and
# `subtext` at 200. `title`, `body` and `subtext` are the three used here, so
# those are the ones worth naming; `subtitle` still isn't.
CAROUSEL_MAX_CARDS = 10
CARD_BODY_CHARACTERS = 200
CARD_SUBTEXT_CHARACTERS = 200


def table_cells(
    row: dict[str, Any], changes: list[tuple[str, str]]
) -> tuple[str, ...]:
    """Return the visible cells for one row in column order. Pagination measures these same cells so its character budget matches the rendered table. The Item cell contains only the label because the host counts displayed text, not the linked URL."""
    title = item_title(row)
    if len(title) > 60:
        title = title[:57].rstrip() + "…"
    closes = row.get("closes") or []
    # Only what the item declares it closes. A row shows nothing rather than
    # guessing, so an empty cell means "declared none", not "none exist".
    see_also = ", ".join(closes[:3]) if closes else "—"
    if len(closes) > 3:
        see_also += f" +{len(closes) - 3}"
    return (
        item_label(row),
        row.get("author_login") or "—",
        "; ".join(text for _, text in changes) or "—",
        derive_status(row),
        title,
        see_also,
    )


def paginate_roster(rows: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Split the roster into pages that fit message character and row limits. Measure the cells the renderer will print. Keep a row that exceeds the character budget on its own page rather than dropping it."""
    pages: list[list[dict[str, Any]]] = []
    page: list[dict[str, Any]] = []
    used = sum(len(name) for name in TABLE_COLUMNS)
    for row in rows:
        width = sum(len(cell) for cell in table_cells(row, []))
        if page and (
            used + width > ROSTER_PAGE_CHARACTERS or len(page) >= ROSTER_PAGE_ROWS
        ):
            pages.append(page)
            page = []
            used = sum(len(name) for name in TABLE_COLUMNS)
        page.append(row)
        used += width
    if page:
        pages.append(page)
    return pages


def render_change_table(entries: list[tuple[dict[str, Any], list[tuple[str, str]]]]) -> str:
    """Render item changes in a table with change columns before the title. Use Slack ``<url|label>`` links. Show the author's tracker login unchanged, including ``[bot]``. Use an em dash for an absent author. The Item cell links to the primary tracker only; paired identifiers remain in the store and audit output."""
    header = (
        "| " + " | ".join(TABLE_COLUMNS) + " |\n"
        "| " + " | ".join("---" for _ in TABLE_COLUMNS) + " |"
    )
    lines = [header]
    for row, changes in entries:
        cells = table_cells(row, changes)
        lines.append(
            f"| <{row.get('url')}|{cells[0]}> | " + " | ".join(cells[1:]) + " |"
        )
    return "\n".join(lines)


@dataclass
class ReportContext:
    state_file: Path
    row_count: int
    tracked: int
    since: datetime | None
    generated: datetime
    run_id: str
    channel: str
    repo_filter: str | None
    full: bool
    warnings: list[str]
    coverage: list[str]
    store_created: bool
    # Which registration routes the store actually shows, so *Source and
    # registration* describes this store rather than reciting a fixed sentence.
    routes: tuple[str, ...] = ()
    # A run that ran out of refresh budget. Both numbers are needed to say
    # anything useful -- "9 unrefreshed" is unreadable without the size of what
    # was attempted -- and they are in the context rather than in `coverage`
    # because this one goes in the brief, where a reader who opens nothing sees
    # it. A report that is short because the refresh stopped early looks exactly
    # like a quiet week, and nothing else in the report distinguishes them.
    unrefreshed: int = 0
    refresh_attempted: int = 0
    refresh_budget: int = 0
    # One entry per repository this run can speak to, for the brief's carousel.
    # Precomputed by `compute_repository_summaries` rather than derived in
    # `render_report` itself, for the same reason `routes` is: everything
    # `render_report` needs to write the brief is already a plain value on the
    # context by the time it runs, and nothing in it re-reads the ledger.
    repository_summaries: list[dict[str, Any]] = field(default_factory=list)


def compute_repository_summaries(
    all_rows: list[dict[str, Any]],
    newly_tracked: list[dict[str, Any]],
    changed: list[tuple[dict[str, Any], list[tuple[str, str]]]],
    terminal: list[dict[str, Any]],
    repositories: Repositories,
    repo_filter: str | None,
) -> list[dict[str, Any]]:
    """Count per-repository changes from this run's report sections. Combine configured repositories with repositories present in rows, using case-insensitive identity and configured capitalization when available. ``tracked`` counts active rows. Apply ``--repo`` to the summaries as well as the rest of the run."""

    def norm(repo: str | None) -> str:
        return (repo or "").lower()

    labels: dict[str, str] = {}
    for slug in repositories.slugs:
        labels[norm(slug)] = slug
    for row in all_rows:
        repo = row.get("repo")
        if repo:
            labels.setdefault(norm(repo), repositories.display(repo))

    if repo_filter:
        key = norm(repo_filter)
        labels = {key: labels[key]} if key in labels else {}

    tracked: dict[str, int] = {}
    new: dict[str, int] = {}
    changed_count: dict[str, int] = {}
    merged: dict[str, int] = {}
    closed: dict[str, int] = {}

    def bump(counter: dict[str, int], repo: str | None) -> None:
        key = norm(repo)
        if key in labels:
            counter[key] = counter.get(key, 0) + 1

    for row in all_rows:
        if row.get("lifecycle") == "active":
            bump(tracked, row.get("repo"))
    for row in newly_tracked:
        bump(new, row.get("repo"))
    for row, _ in changed:
        bump(changed_count, row.get("repo"))
    for row in terminal:
        bump(merged if derive_status(row) == "landed" else closed, row.get("repo"))

    return [
        {
            "slug": labels[key],
            "tracked": tracked.get(key, 0),
            "new": new.get(key, 0),
            "changed": changed_count.get(key, 0),
            "merged": merged.get(key, 0),
            "closed": closed.get(key, 0),
        }
        for key in sorted(labels, key=lambda key: labels[key].lower())
    ]


def _fit_card_text(text: str, limit: int) -> str:
    """*text*, or its first *limit* characters with a trailing ellipsis.

    The blunt fallback both card renderers reach for once a field cannot be
    shortened any other way: drop nothing structural, just stop before the
    cap and say something was cut.
    """
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _fit_card_body(parts: list[str], limit: int = CARD_BODY_CHARACTERS) -> str:
    """Join *parts* with `" · "`, dropping from the end until it fits.

    Not reached at any realistic count -- see `render_repository_carousel` --
    but the card is display-only and would rather lose its least important
    figure than exceed a field Slack has documented as 200 characters.
    `parts[0]`, the tracked count, is dropped last of all: a card naming no
    count at all is a card naming nothing.
    """
    while len(parts) > 1 and len(" · ".join(parts)) > limit:
        parts = parts[:-1]
    return _fit_card_text(" · ".join(parts), limit)


def render_repository_carousel(summaries: list[dict[str, Any]]) -> str | None:
    """Render one linked Block Kit card per repository, or return ``None`` when there is nothing to show. Cards show active tracked items and this run's new, changed, merged, and closed counts. Omit zero counts and show ``no changes`` when all are zero. Limit the carousel to ten cards, prioritizing repositories with activity, then display retained cards alphabetically. Cards are display-only and use the same repository icon."""
    if not summaries:
        return None
    kept = summaries
    if len(kept) > CAROUSEL_MAX_CARDS:
        busiest = sorted(
            summaries,
            key=lambda summary: (
                -(summary["new"] + summary["changed"] + summary["merged"] + summary["closed"]),
                summary["slug"].lower(),
            ),
        )
        keep = {summary["slug"] for summary in busiest[:CAROUSEL_MAX_CARDS]}
        kept = [summary for summary in summaries if summary["slug"] in keep]

    cards = []
    for summary in kept:
        slug = summary["slug"]
        parts = [f"{summary['tracked']} tracked"]
        for count, label in (
            (summary["new"], "new"),
            (summary["changed"], "changed"),
            (summary["merged"], "merged"),
            (summary["closed"], "closed"),
        ):
            if count:
                parts.append(f"{count} {label}")
        if len(parts) == 1:
            parts.append("no changes")
        cards.append(
            {
                "type": "card",
                "title": {
                    "type": "mrkdwn",
                    "text": f"<https://github.com/{slug}|{slug}>",
                    "verbatim": False,
                },
                "body": {
                    "type": "mrkdwn",
                    "text": _fit_card_body(parts),
                    "verbatim": False,
                },
                "slack_icon": {"type": "icon", "name": "code"},
            }
        )
    payload = {"blocks": [{"type": "carousel", "elements": cards}]}
    return "```blockkit\n" + json.dumps(payload, ensure_ascii=False, indent=2) + "\n```"


def render_report_header(
    context: ReportContext,
    newly_tracked: list[dict[str, Any]],
    changed: list[tuple[dict[str, Any], list[tuple[str, str]]]],
    still_waiting: list[dict[str, Any]],
    terminal: list[dict[str, Any]],
    roster: list[dict[str, Any]] | None,
) -> str:
    """Render the report opening as two Block Kit ``context`` blocks: run details, then the change tally. Include the run date and time, tracked scope, look-back period, and repository filter when set. Keep the tally's existing section order and omit zero-count sections. Include the roster count on ``--full`` runs. Show an explicit quiet-run message when no sections have counts."""
    when = [context.generated.strftime("%Y-%m-%d %H:%M"), f"{context.tracked} tracked"]
    if context.repo_filter:
        when.append(f"repo: {context.repo_filter}")
    since = f"since {context.since.date().isoformat()}" if context.since else "since the first run"
    when_text = f":clock3: {' · '.join(when)} ({since})"

    tally = [
        (len(newly_tracked), "newly tracked"),
        (len(changed), "changed"),
        (len(still_waiting), "still waiting"),
        (len(terminal), "closed and landed"),
    ]
    if context.full and roster:
        tally.append((len(roster), "on the roster"))
    counted = [f"{count} {label}" for count, label in tally if count]
    what_text = ":arrows_counterclockwise: " + (" · ".join(counted) if counted else "No changes")

    payload = {
        "blocks": [
            {"type": "context", "elements": [{"type": "mrkdwn", "text": when_text}]},
            {"type": "context", "elements": [{"type": "mrkdwn", "text": what_text}]},
        ]
    }
    return "```blockkit\n" + json.dumps(payload, ensure_ascii=False, indent=2) + "\n```"


def render_report(
    context: ReportContext,
    newly_tracked: list[dict[str, Any]],
    changed: list[tuple[dict[str, Any], list[tuple[str, str]]]],
    still_waiting: list[dict[str, Any]],
    terminal: list[dict[str, Any]],
    roster: list[dict[str, Any]] | None,
    unrendered_titles: list[dict[str, Any]],
) -> str:
    # The header states when the report ran and what it found.
    lines = [render_report_header(context, newly_tracked, changed, still_waiting, terminal, roster)]

    # In the brief, not only in the thread. A run that stopped refreshing early
    # renders as a short report, and a short report is what a quiet week renders
    # as too: the reader cannot tell them apart from the tally, and the one
    # reading they must not come away with is "nothing happened". So it sits
    # above the fold, in the channel message, where it is seen without opening
    # anything. *Coverage and gaps* repeats it for the reader who does.
    if context.unrefreshed:
        lines += [
            "",
            (
                f"*Partial run:* {context.unrefreshed} of {context.refresh_attempted} "
                f"tracked items were not refreshed before this run's {context.refresh_budget}s "
                "refresh budget ran out, and are left out of everything below. "
                "Whatever changed on them is not here and was not consumed: the next "
                "run reports it."
            ),
        ]

    # The per-repository carousel, an addition to the brief rather than a
    # replacement for anything in it. It sits below the two context blocks
    # rather than above them: those two blocks are what report-shape.md
    # promises a skimmer can read without expanding or scrolling anything, and
    # a carousel is a horizontally-scrolling element that can push whatever
    # follows it out of view on a narrow client. Above it, that is the headline
    # and the tally; below it, it is only the carousel -- so those stay
    # readable first and the per-repository detail follows them, which also
    # reads better in isolation: here is what happened, here is the
    # breakdown. None with nothing to show -- see `render_repository_carousel`
    # -- so a store tracking nothing configured and holding no rows adds
    # nothing here.
    carousel = render_repository_carousel(context.repository_summaries)
    if carousel:
        lines += ["", carousel]

    # Each marker is a message boundary on hosts that recognise it, and an HTML
    # comment everywhere else. The report writes three: the brief above the
    # first, the delta tables next, the roster after them, and the prose that
    # explains them all last. Tables come before prose because they are what the
    # reader opened the thread for; the prose is reference, read once and then
    # skipped. The roster follows the deltas rather than leading them because it
    # is the standing list and they are the news.
    #
    # The splits are worth having beyond ordering. Slack's Block Kit limits --
    # 100 table rows, 50 blocks, 10 000 table characters -- are counted per
    # message, so one message is one shared budget and a breach degrades every
    # table in it to raw pipe characters at once. Each boundary buys another
    # budget: the prose that could never render as a table spends none of the
    # tables', and the roster -- the longest table by construction, and the one
    # that reaches a limit first -- no longer spends the deltas'.
    #
    # A host that splits on the first marker only still delivers a correct
    # report: it strips the rest and the thread is one reply again, in this same
    # order. That is the degradation to design for, not a case to detect.
    #
    # Three is where the boundaries go, not how many are written. A boundary
    # whose piece renders to nothing is dropped on the way out -- see
    # `join_message_pieces` -- because a boundary with nothing under it is a
    # request to post an empty message.
    lines += ["", THREAD_BOUNDARY]

    if newly_tracked:
        lines += ["", "*Newly tracked*"]
        # The route belongs on these rows and on no others. "Twelve newly
        # tracked" reads completely differently depending on whether twelve
        # people asked about something or one sweep swept -- they are different
        # intents and a reader deciding whether to look is entitled to the
        # difference. It is only ever news once: by the time an item is in
        # *Changed*, how it arrived is settled history and would be a column of
        # noise repeated every run.
        entries = []
        for row in newly_tracked:
            marks: list[tuple[str, str]] = []
            if int(row.get("reopen_count") or 0) > 0:
                marks.append(("reopened", "reopened"))
            route = describe_routes(row_routes(row))
            if route:
                marks.append(("registered", route))
            entries.append((row, marks))
        lines.append(render_change_table(entries))

    if changed:
        lines += ["", "*Changed*"]
        lines.append(render_change_table(changed))

    if still_waiting:
        lines += ["", "*Still waiting*"]
        lines.append(
            render_change_table(
                [(row, [("waiting", "unchanged since last mentioned")]) for row in still_waiting]
            )
        )

    if terminal:
        lines += ["", "*Closed and landed*"]
        lines.append(render_change_table([(row, []) for row in terminal]))

    # The second boundary, and the roster below it. The roster is unlike every
    # table above it: those name what changed and are as long as the news, this
    # one names every tracked item and is as long as the roster itself, so it is
    # the section that reaches a Block Kit limit first and the only one whose
    # length nobody chose. Sharing a message with the deltas made the two
    # ceilings one -- measured on real rows, each was fine at a size that
    # together ran out of table characters at under half of it, and the breach
    # took the deltas' formatting down alongside the roster's. A boundary here
    # gives each its own budget and costs nothing when there is no roster: the
    # piece between this marker and the next is then empty, and an empty piece
    # is dropped rather than delivered as a blank message.
    lines += ["", THREAD_BOUNDARY]

    if context.full and roster:
        # Paged, because one message's budget is a ceiling the roster grows past
        # rather than a size it happens to be. Each page is its own message and
        # so its own budget, using the same boundary mechanism as everything
        # else here -- there is nothing to add to the host for this.
        pages = paginate_roster(roster)
        if len(pages) > ROSTER_MAX_PAGES:
            # Paging further is mechanically fine and useless. A roster this
            # long is no longer something a reader scans in a thread, and the
            # messages it would take push everything after it out of reach. The
            # count is still stated, so nothing is hidden -- what is withheld is
            # a list nobody could read, and the way to get one back is named.
            lines += ["", "*Roster*"]
            lines.append(
                f"- {len(roster)} items on the roster, which is more than this "
                f"report will list: it would take {len(pages)} messages, and a "
                "list that long is no longer one a reader can scan. Narrow the "
                "run with `--repo owner/name` to get a roster back."
            )
        else:
            for index, page in enumerate(pages, 1):
                # Between pages, never before the first: the boundary above
                # already opened this message.
                if index > 1:
                    lines += ["", THREAD_BOUNDARY]
                # A single page is titled the way it always was. Numbering a
                # list of one says a second message exists and invites a reader
                # to go looking for it.
                heading = (
                    "*Roster*"
                    if len(pages) == 1
                    else f"*Roster ({index}/{len(pages)})*"
                )
                lines += ["", heading]
                lines.append(render_change_table([(row, []) for row in page]))

    # The last boundary. Below it is prose about the tables above it, in the
    # order a reader wants it: what the rows mean, then what is missing from
    # them, then where they came from.
    lines += ["", THREAD_BOUNDARY]

    lines += ["", "*Detail*"]
    lines.append(
        "- `mergeStateStatus` is not read as a signal: where a repository's merge "
        "gate is not GitHub's own, GitHub reports every open PR as `BLOCKED` "
        "whatever its state, so the field is a constant rather than news."
    )
    for row in unrendered_titles:
        lines.append(
            f"- <{row.get('url')}|{item_label(row)}> source title: {row.get('title_source')}"
        )

    lines += ["", "*Coverage and gaps*"]
    # Same condition the registration line below uses to decide whether it
    # mentions the link trigger: the default (no routes observed yet) reads as
    # link-trigger-only there, and author-watch-only is the one case where the
    # link trigger is not in the picture at all. Printing this unconditionally
    # described a route that was never configured on an author-watch-only
    # store, and contradicted the registration line sitting twenty lines below
    # it in the same report.
    if "author_watch" not in context.routes or "auto_link" in context.routes:
        lines.append(
            "- Only links posted while the link trigger was running are tracked. A "
            "link posted while it was down, posted by an app or another bot, or added "
            "by editing an existing message may never have registered and is missing "
            "here until it is posted again. A quiet report is not evidence of a quiet "
            "week."
        )
    if "author_watch" in context.routes:
        lines.append(
            "- The author watch covers the people on its list and nobody else, "
            "in the repositories it is pointed at. It registers what they have "
            "open and what of theirs closed recently, so work by anyone not on "
            "the list arrives only if its link is posted, and an item that "
            "closed before the watch first saw its author was never registered "
            "at all."
        )
    if context.store_created:
        lines.append(
            f"- The store {store_label(context.state_file)} did not exist and was "
            "created by this run. Every link already in the channel is "
            "unregistered."
        )
    for warning in context.coverage:
        lines.append(f"- {warning}")

    # Where the rows came from and how one gets in. Named for the two questions
    # it answers rather than for the fields it prints, so that the store line and
    # the registration line sit under one heading instead of two. It is last
    # because it is the section a correct report never needs -- it earns its
    # place on the run where the report is wrong, and then it is the first thing
    # to read.
    lines += ["", "*Source and registration*"]
    lines.append(
        f"- Store: {store_label(context.state_file)} ({context.row_count} rows). A "
        "store name this channel does not recognise, or `(0 rows)` under a channel "
        "that has been collecting links, is a misconfigured store rather than a "
        "quiet week."
    )
    if "author_watch" in context.routes:
        others = [route for route in context.routes if route != "author_watch"]
        lines.append(
            "- Registration: author watch"
            + (f" and {describe_routes(others)}" if others else " only")
            + ". An item enters when the watch finds one its authors opened"
            + (
                ", when its link is posted in the channel"
                if "auto_link" in context.routes
                else ""
            )
            + ", and by no other route. A store showing only one of these when "
            "both are configured is a route that has stopped running rather "
            "than a quiet week."
        )
    else:
        lines.append(
            "- Registration: link trigger only. An item enters when its link is posted "
            "in the channel and by no other route; nothing replays a link that was "
            "missed."
        )
    lines.append(f"- Run id `{context.run_id}` · channel `{context.channel}`.")
    return join_message_pieces(lines)


def join_message_pieces(lines: list[str]) -> str:
    """Join report sections and remove empty message boundaries. Keep the first piece because it contains the channel message that provides the thread root."""
    pieces: list[list[str]] = [[]]
    for line in lines:
        if line == THREAD_BOUNDARY:
            pieces.append([])
        else:
            pieces[-1].append(line)

    def trimmed(piece: list[str]) -> list[str]:
        # The blank line before a boundary belongs to the boundary, and lands at
        # the end of the piece before it when the list is split. Dropping it here
        # and re-adding it with the boundary keeps the spacing identical to
        # writing the boundary inline, whether or not the piece survives.
        while piece and not piece[-1].strip():
            piece.pop()
        return piece

    out = trimmed(pieces[0])
    for piece in pieces[1:]:
        if not any(line.strip() for line in piece):
            continue
        out = out + ["", THREAD_BOUNDARY] + trimmed(piece)
    return "\n".join(out) + "\n"


# ----------------------------------------------- answering about the store
#
# Everything in this section reads the store and writes nothing at all. It
# exists because the alternative is a hand-written `grep` per question, and a
# `grep` written against a schema the caller is guessing at answers confidently
# and wrongly. The pattern that made this necessary was `"status": "merged"`,
# run over a store that has no `status` field and records a merge in
# `gh_merged_at`. It matched nothing, printed `0`, and `0` is a completely
# plausible answer to "how many have been merged". Nothing in the output said
# the field did not exist, so the next attempt varied the path rather than the
# pattern, and the path is what got corrupted.
#
# Three rules follow from that, and every function below keeps all three:
#
#   * **Name the store and the number of rows read in every output**, however
#     small that output is. That one line is what separates "nothing matched"
#     from "nothing was read", and a caller cannot recover the difference from
#     an exit code -- some hosts report every command as having succeeded.
#   * **Refuse a store that is not there** rather than answering over it, the
#     way `audit` does and for the same reason: a clean count over a file that
#     was never opened is the failure itself, not a milder version of it.
#   * **Take the vocabulary from `references/store-schema.md`**, so the flag a
#     caller reaches for is the field name the document explains. A CLI whose
#     words differ from the schema's is one more thing to guess at.

# The columns of the registration table, in order, and the field each one reads.
# Every one of them is already in the store, which is the entire point: filling
# this table needs no call to any tracker, and a caller that goes to a tracker
# for them is fetching what it has already been handed. The mapping is data
# rather than a formatting function so that a test can check that claim.
REGISTRATION_COLUMNS: tuple[tuple[str, str], ...] = (
    ("URL", "url"),
    ("Tracker", "tracker"),
    ("Repository", "repo"),
    ("Number", "number"),
    ("Title", "title_rendered"),
    ("Author", "author_login"),
    ("State", "gh_state"),
    ("Created", "created_at"),
    ("Last activity", "last_activity_utc"),
)

# Two ways a cell can carry no value, kept apart on purpose. "Never looked" and
# "looked, and the tracker did not say" are different facts about the world, and
# a table rendering both as a dash invites a reader to read the first as the
# second -- which is how an item nobody has refreshed yet gets reported as
# having no author. It is the same distinction that makes an absent store a
# refusal rather than a count of zero.
CELL_NEVER_REFRESHED = "not refreshed yet"
CELL_UNKNOWN = "unknown"

# The tracker's own name for itself, which is neither the key the store uses nor
# anything a host configures. A value not in the map renders as stored.
TRACKER_NAMES = {"github": "GitHub", "gitcode": "GitCode"}

# Past this a title is cut. A cell is read at a glance and the table has nine
# columns; without a bound one long title pushes every other column off the
# side of the message.
TABLE_TITLE_LIMIT = 80


def is_refreshed(row: dict[str, Any]) -> bool:
    """Has any run ever filled this row's tracker facts?

    `track` and `watch` write identity and registration only -- deliberately, so
    registering never waits on a tracker and can never corrupt status -- so a row
    that has been registered and never reported has no state, no title and no
    merge marker. Absent is not `open`, and it is not `unmerged`.
    """
    return bool(row.get("last_refresh_utc"))


def is_merged(row: dict[str, Any]) -> bool:
    """Return whether either tracker marks the item as merged. Check ``gh_merged_at`` on GitHub and ``merged_at`` or ``mr_state`` on GitCode. GitHub ``gh_state`` reports merged pull requests as ``closed`` and cannot identify a merge by itself."""
    return bool(
        row.get("gh_merged_at")
        or row.get("merged_at")
        or row.get("mr_state") == "merged"
    )


def tracker_state(row: dict[str, Any]) -> str:
    """Return the item's tracker state for display. Prefer the merge marker, then the draft flag, before using ``gh_state``. Do not show store lifecycle values such as ``registered``, ``active``, or ``ignored`` as tracker state."""
    if not is_refreshed(row):
        return CELL_NEVER_REFRESHED
    if row.get("gone"):
        return "gone"
    if is_merged(row):
        return "merged"
    state = row.get("gh_state")
    if state == "open" and row.get("draft"):
        return "draft"
    return str(state) if state else CELL_UNKNOWN


def cell_text(value: Any) -> str:
    """One cell's text: never more than one line, and never an unescaped pipe.

    Titles are arbitrary text from a tracker. A pipe in one silently invents a
    tenth column in every renderer that reads this table, and a newline in one
    ends the table.
    """
    text = "" if value is None else str(value)
    text = " ".join(text.split())
    return text.replace("|", "\\|")


def registration_cells(row: dict[str, Any]) -> tuple[str, ...]:
    """The nine cells of one row, in column order, read from the store alone."""
    absent = CELL_UNKNOWN if is_refreshed(row) else CELL_NEVER_REFRESHED
    cells: list[str] = []
    for name, key in REGISTRATION_COLUMNS:
        if name == "State":
            cells.append(cell_text(tracker_state(row)))
            continue
        if name == "Tracker":
            tracker = str(row.get("tracker") or "")
            cells.append(cell_text(TRACKER_NAMES.get(tracker, tracker)) or CELL_UNKNOWN)
            continue
        if name == "Title":
            text = cell_text(row.get("title_rendered") or row.get("title_source"))
            if len(text) > TABLE_TITLE_LIMIT:
                text = text[: TABLE_TITLE_LIMIT - 1].rstrip() + "…"
            cells.append(text or absent)
            continue
        text = cell_text(row.get(key))
        if text:
            cells.append(text)
        elif key in {"url", "repo", "number"}:
            # Registration writes identity. Missing values are unknown, not pending refresh.
            cells.append(CELL_UNKNOWN)
        else:
            cells.append(absent)
    return tuple(cells)


def render_registration_table(rows: list[dict[str, Any]]) -> str:
    """The nine-column table, or an empty string when there is nothing to show.

    Markdown rather than the report's `<url|label>` links: this table is a reply
    to a message rather than part of a rendered report, and it has to survive
    being read somewhere that has never heard of one host's link syntax.
    """
    if not rows:
        return ""
    header = [name for name, _ in REGISTRATION_COLUMNS]
    lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join("---" for _ in header) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(registration_cells(row)) + " |")
    return "\n".join(lines) + "\n"


def query_matches(row: dict[str, Any], args: argparse.Namespace) -> bool:
    """Does this row pass every filter the caller stated?

    Each filter is named after the field it reads, so the flags and
    `references/store-schema.md` share one vocabulary. The set is small on
    purpose: this replaces `grep | wc -l`, and a filter language large enough to
    need learning is a filter language a caller improvises around.
    """
    if args.lifecycle and str(row.get("lifecycle") or "") not in args.lifecycle:
        return False
    if args.gh_state and str(row.get("gh_state") or "") not in args.gh_state:
        return False
    if args.kind and str(row.get("kind") or "") not in args.kind:
        return False
    if args.status and derive_status(row) not in args.status:
        return False
    if args.repo and str(row.get("repo") or "").lower() != args.repo.lower():
        return False
    if args.merged and not is_merged(row):
        return False
    return not (args.unmerged and is_merged(row))


def query_filters(args: argparse.Namespace) -> list[str]:
    """The filters in force, written as the flags that would reproduce them.

    Echoed in every output. A count is only as trustworthy as the question it
    answers, and a caller who mistyped a filter value has no other way to see
    that the number in front of them answers a narrower question than the one
    asked.
    """
    stated: list[str] = []
    for flag, values in (
        ("--lifecycle", args.lifecycle),
        ("--gh-state", args.gh_state),
        ("--kind", args.kind),
        ("--status", args.status),
    ):
        for value in values or []:
            stated.append(f"{flag} {value}")
    if args.repo:
        stated.append(f"--repo {args.repo}")
    if args.merged:
        stated.append("--merged")
    if args.unmerged:
        stated.append("--unmerged")
    return stated


def tally(values: Iterable[Any]) -> str:
    """`a 12, b 3`, commonest first, with an unset value named as unset."""
    counts: dict[str, int] = {}
    for value in values:
        name = str(value) if value not in (None, "") else "(unset)"
        counts[name] = counts.get(name, 0) + 1
    ordered = sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))
    return ", ".join(f"{name} {count}" for name, count in ordered) or "(none)"


def query_scope(store: str, read: int, matched: int, filters: list[str]) -> str:
    """The line every output starts with, whatever its format.

    It carries the two facts that make a number checkable: which file was read,
    and how many rows were in it. A `0` under this line is visibly an answer; a
    bare `0` is indistinguishable from a `grep` that matched a field name the
    store does not have.
    """
    scope = f"{store} -- {read} row(s) read, {matched} matched"
    return scope + (f" ({' '.join(filters)})." if filters else ".")


def render_query_summary(
    store: str, read: int, rows: list[dict[str, Any]], filters: list[str]
) -> str:
    """The census: one screen answering the questions people actually ask.

    Printed when no format is named, because the question that made this
    necessary -- "how many have been merged?" -- should cost the caller no flag
    to guess at and no field name to remember.
    """
    merged = sum(1 for row in rows if is_merged(row))
    unrefreshed = [row for row in rows if not is_refreshed(row)]
    lines = [
        query_scope(store, read, len(rows), filters),
        "",
        (
            f"merged      {merged}"
            "   gh_merged_at set, or the second tracker's merged_at"
        ),
        (
            f"gh_state    {tally(row.get('gh_state') for row in rows)}"
            "   the tracker's own state; never 'merged'"
        ),
        (
            f"lifecycle   {tally(row.get('lifecycle') for row in rows)}"
            "   this skill's tracking state, not the tracker's"
        ),
        f"kind        {tally(row.get('kind') for row in rows)}",
        (
            f"status      {tally(derive_status(row) for row in rows)}"
            "   derived; 'landed' is the second tracker's merge alone"
        ),
    ]
    if unrefreshed:
        lines += [
            "",
            (
                f"{len(unrefreshed)} matched row(s) have never been refreshed, so they "
                "carry no tracker facts at all: their state, merge marker and title "
                "are absent rather than false. Registration writes identity only; the "
                "facts arrive with the next report."
            ),
        ]
    return "\n".join(lines) + "\n"


def cmd_query(args: argparse.Namespace) -> int:
    """Read the store and answer one question about it. Writes nothing.

    No lock is taken and none is needed: every writer replaces the file whole,
    so a reader sees one version or the next and never half of either. Taking
    the lock would instead park an interactive question behind a refresh that
    runs for minutes, which is the one thing this verb cannot afford to be.
    """
    state_file, notes = resolve_state_file(args.state_file, args.channel)
    if not state_file.exists():
        # Exit 2, `audit`'s code and `audit`'s reason: "there was nothing to
        # read" must never be printed in the same shape as "the answer is zero".
        # A wrong path is the overwhelmingly likely cause, so what is around the
        # path is described rather than a bare refusal given.
        print(
            f"the store {store_label(state_file)} does not exist, so nothing was "
            f"read and no count was produced.\n{describe_absent_store(state_file)}\n"
            "A count over a store that was never opened is not an answer of zero; "
            "it is the absence of an answer, and the two are indistinguishable "
            "once printed. Check the path before anything else -- the prompt that "
            "runs this skill states it, so use the one it states rather than "
            "searching for the file or retyping it from memory.",
            file=sys.stderr,
        )
        return 2
    for note in notes:
        print(f"note: {note}", file=sys.stderr)
    ledger = Ledger.load(state_file)
    check_owner(read_runs(runs_path(state_file)), args.channel)
    if ledger.corrupt:
        print(
            "note: unparseable ledger line(s) at "
            + ", ".join(str(number) for number, _ in ledger.corrupt)
            + "; they were skipped for reading and are not counted below.",
            file=sys.stderr,
        )
    read = len(ledger.rows)
    rows = [row for row in ledger.rows if query_matches(row, args)]
    shown = rows[: args.limit] if args.limit else rows
    store = store_label(state_file)
    filters = query_filters(args)

    if args.format == "count":
        # The number alone on the first line, so a caller reading only the first
        # line reads only the number -- and the provenance under it, so a caller
        # reading the whole thing cannot mistake a count over the wrong file for
        # a small one.
        print(len(rows))
        print(query_scope(store, read, len(rows), filters))
        return 0
    if args.format == "json":
        payload = {
            "store": store,
            "rows_read": read,
            "matched": len(rows),
            "shown": len(shown),
            "filters": filters,
            "merged": sum(1 for row in rows if is_merged(row)),
            "items": [
                {
                    "key": row.get("key"),
                    "lifecycle": row.get("lifecycle"),
                    "status": derive_status(row),
                    "merged": is_merged(row),
                    "refreshed": is_refreshed(row),
                    **{field: row.get(field) for _, field in REGISTRATION_COLUMNS},
                }
                for row in shown
            ],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    if args.format == "table":
        print(query_scope(store, read, len(rows), filters))
        if not rows:
            print(
                f"No row matched. The store was read and holds {read} row(s), so "
                "this is an answer about the store and not a missing file."
            )
            return 0
        print()
        print(render_registration_table(shown), end="")
        if len(shown) < len(rows):
            print(
                f"\n{len(rows) - len(shown)} further matched row(s) are not shown; "
                f"raise --limit (currently {args.limit}) to see them."
            )
        return 0
    print(render_query_summary(store, read, rows, filters), end="")
    return 0


# ---------------------------------------------------------------- subcommands


def cmd_track(args: argparse.Namespace) -> int:
    # Provenance is checked before the store path is even resolved: these flags
    # are the one part of this command the caller fills in from its own reading
    # of the turn, and a bad value is indistinguishable from a good one once
    # written. A refused value never reaches the store; the registration itself
    # is unaffected.
    provenance = Provenance.check(
        slack_ts=args.slack_ts,
        permalink=args.slack_permalink,
        author=args.slack_author,
    )
    if provenance.rejected:
        print(f"warning: {provenance.warning()}", file=sys.stderr)
    state_file, notes = resolve_state_file(args.state_file, args.channel)
    when = format_utc(now_utc())
    text = args.message_text or ""
    if args.message_file:
        source = sys.stdin if args.message_file == "-" else None
        text = (
            source.read()
            if source is not None
            else Path(args.message_file).read_text(encoding="utf-8")
        )
    explicit = [canonicalise(url) for url in args.url or []]
    found = links_in_text(text)
    for link in explicit:
        if link is not None and all(link.key != seen.key for seen in found):
            found.append(link)

    result: dict[str, Any] = {
        "store": store_label(state_file),
        "store_created": not state_file.exists(),
        "notes": notes,
        "registered": [],
        "updated": [],
        "reopened": [],
        "unregistered": [],
        "skipped_incidental": [],
        "not_trackable": [],
        "rejected_provenance": provenance.rejected,
    }

    trackable = [link for link in found if link.kind != "other"]
    untrackable = [link for link in found if link.kind == "other"]
    # The rows this run registered or re-registered, in the order the links
    # arrived, kept as rows rather than as keys: `--render-table` renders them
    # from the store, and re-reading the store for keys it is already holding is
    # how a table and a JSON summary of the same run start disagreeing.
    touched: list[dict[str, Any]] = []

    with file_lock(lock_path(state_file)):
        ledger = Ledger.load(state_file)
        runs = read_runs(runs_path(state_file))
        check_owner(runs, args.channel)
        if not ledger.existed:
            print(
                f"note: the store {store_label(state_file)} does not exist and is "
                "being created. An empty store means every link already in the "
                "channel is unregistered.",
                file=sys.stderr,
            )
        if ledger.corrupt:
            result["corrupt_lines"] = [number for number, _ in ledger.corrupt]

        table = ledger.index()
        for target in args.unregister or []:
            link = canonicalise(target)
            row = table.get(target) or (table.get(link.key) if link else None)
            if row is None:
                result["unregistered"].append({"target": target, "outcome": "unknown"})
                continue
            row["lifecycle"] = "ignored"
            row["ignored_at_utc"] = when
            result["unregistered"].append({"target": row["key"], "outcome": "ignored"})

        for link in [*trackable, *untrackable]:
            if link.kind == "other" and trackable:
                # Incidental context alongside a real link is skipped silently.
                result["skipped_incidental"].append(link.url)
                continue
            row, outcome = upsert_registration(
                ledger,
                link,
                via=args.via,
                slack_ts=provenance.slack_ts,
                permalink=provenance.permalink,
                author=provenance.author,
                when=when,
                slack_ts_rejected=provenance.rejected_slack_ts,
            )
            if link.kind == "other":
                row["lifecycle"] = "ignored"
                result["not_trackable"].append(link.url)
                continue
            entry = {"key": row["key"], "url": row["url"], "kind": row["kind"]}
            touched.append(row)
            if row.get("lifecycle") == "ignored":
                entry["note"] = "row is untracked; re-registering did not revive it"
            result[
                {"registered": "registered", "reopened": "reopened"}.get(
                    outcome, "updated"
                )
            ].append(entry)

        if args.channel and not runs.get("channel_id"):
            runs.setdefault("schema_version", SCHEMA_VERSION)
            runs["channel_id"] = args.channel
            write_runs(runs_path(state_file), runs)
        ledger.write()
        result["rows"] = len(ledger.rows)

    print(json.dumps(result, ensure_ascii=False, indent=2))
    if getattr(args, "render_table", False):
        # After the JSON and separated from it, so that a caller told to reply
        # with the table has one block to copy and no prose of its own to write.
        # The rule about which links get a table is enforced here rather than
        # asked for in a prompt: a table means these items are now tracked, so a
        # table of untrackable links is a wrong answer rather than a formatting
        # choice, and a rule a prompt merely states is one a reader can talk
        # itself out of.
        if touched:
            print()
            print(render_registration_table(touched), end="")
            if any(not is_refreshed(row) for row in touched):
                print(
                    f"\n'{CELL_NEVER_REFRESHED}' is not a gap in the tracker: "
                    "registering writes identity only, on purpose, so that it "
                    "never waits on a tracker and can never corrupt status. "
                    "Those cells fill on the next report run. Do not look them "
                    "up and do not guess at them."
                )
        else:
            for url in result["not_trackable"]:
                print(f"not tracked: no pull request or issue link ({url})")
    return 0


def _run_one_sweep(
    query: str,
    *,
    token: str,
    api_base: str,
    kinds: tuple[str, ...],
    max_results: int,
    seen: set[str],
    found: list[ParsedLink],
    sleeper: Callable[[float], None],
) -> dict[str, Any]:
    """Page through one sweep, appending what it finds. Returns what it cost."""
    stats: dict[str, Any] = {"requests": 0, "matched": None, "truncated": False}
    page = 1
    waited = False
    per_page = max(1, min(WATCH_PAGE_SIZE, max_results))
    while len(found) < max_results:
        try:
            payload, _ = github_search(
                query, token, api_base, page=page, per_page=per_page
            )
        except SearchRateLimited as limited:
            wait = 0.0
            if limited.reset_epoch:
                wait = limited.reset_epoch - now_utc().timestamp() + 1
            if waited or wait <= 0 or wait > GITHUB_SEARCH_MAX_WAIT_SECONDS:
                raise
            # The search budget refills on a short window, so waiting it out once
            # is cheaper and more honest than abandoning the rest of the authors.
            # Only once: a second wall in the same pass is a budget this run
            # does not fit inside, and sleeping through that would strand the
            # caller instead of telling it.
            print(
                f"note: the search rate limit is spent; waiting {int(wait)}s for "
                "it to refill before continuing.",
                file=sys.stderr,
            )
            sleeper(wait)
            waited = True
            continue
        stats["requests"] += 1
        total = payload.get("total_count")
        if stats["matched"] is None and isinstance(total, int):
            stats["matched"] = total
            if total > GITHUB_SEARCH_RESULT_CAP:
                stats["truncated"] = True
        if payload.get("incomplete_results"):
            stats["truncated"] = True
        items = [item for item in payload.get("items") or [] if isinstance(item, dict)]
        for item in items:
            wanted = "pull_request" if item.get("pull_request") else "issue"
            if wanted not in kinds:
                continue
            link = canonicalise(str(item.get("html_url") or ""))
            if link is None or link.kind == "other" or link.key in seen:
                continue
            seen.add(link.key)
            found.append(link)
            if len(found) >= max_results:
                stats["truncated"] = True
                break
        if len(items) < per_page:
            break
        page += 1
    return stats


def search_watch_target(
    target: WatchTarget,
    closed_since: str | None,
    *,
    token: str,
    api_base: str,
    max_results: int,
    sleeper: Callable[[float], None] = time.sleep,
) -> tuple[list[ParsedLink], dict[str, Any]]:
    """Everything this author has open, plus what of theirs closed recently.

    Paging stops at `max_results` and says so rather than presenting the prefix
    it got as the whole answer. A truncated result is reported as truncated for
    the same reason a failed refresh is left out of a delta: a partial answer
    that looks complete is the one kind of wrong this store cannot detect later.
    """
    found: list[ParsedLink] = []
    seen: set[str] = set()
    outcome: dict[str, Any] = {
        "author": target.login,
        "repo": target.repo,
        "kinds": list(target.kinds),
        "requests": 0,
        "truncated": False,
        "sweeps": {},
    }
    for sweep, query in build_search_queries(target, closed_since):
        stats = _run_one_sweep(
            query,
            token=token,
            api_base=api_base,
            kinds=target.kinds,
            max_results=max_results,
            seen=seen,
            found=found,
            sleeper=sleeper,
        )
        outcome["sweeps"][sweep] = {
            "matched": stats["matched"],
            "requests": stats["requests"],
        }
        outcome["requests"] += stats["requests"]
        outcome["truncated"] = outcome["truncated"] or bool(stats["truncated"])
    outcome["found"] = len(found)
    return found, outcome


def logins_already_watched(rows: Iterable[dict[str, Any]]) -> set[str]:
    """Watched logins this store has already seen work by, lowercased.

    Two proofs, both facts already in the store rather than a fourth file to
    keep in step with it: a registration this watch made and stamped with the
    login, or a row whose refreshed `author_login` is that person. The second
    matters because an item the channel tracked by link is still evidence that
    the channel has seen this author's work.
    """
    known: set[str] = set()
    for row in rows:
        login = str(row.get("author_login") or "").strip().lower()
        if login:
            known.add(login)
        for source in row.get("sources") or []:
            watched = str((source or {}).get("watch_author") or "").strip().lower()
            if watched:
                known.add(watched)
    return known


def render_watch_card(
    registered_count: int, tracked_count: int, repo_count: int, author_count: int
) -> str:
    """Render the author watch reply as one Block Kit ``card``. The card title and icon are fixed; its body shows the new-item count or ``no new items``. Put standing tracked, repository, and author counts in subtext. The function builds the payload so unattended runs do not depend on a model to reproduce its structure."""
    body_text = f"{registered_count} new items" if registered_count else "no new items"
    repo_word = "repo" if repo_count == 1 else "repos"
    author_word = "author" if author_count == 1 else "authors"
    subtext_text = _fit_card_text(
        f"{tracked_count} tracked · {repo_count} {repo_word} · "
        f"{author_count} {author_word}",
        CARD_SUBTEXT_CHARACTERS,
    )
    payload = {
        "blocks": [
            {
                "type": "card",
                "title": {"type": "mrkdwn", "text": "Author watch", "verbatim": False},
                "body": {"type": "mrkdwn", "text": body_text, "verbatim": False},
                "subtext": {
                    "type": "mrkdwn",
                    "text": subtext_text,
                    "verbatim": False,
                },
                "slack_icon": {"type": "icon", "name": "eye-open"},
            }
        ]
    }
    return "```blockkit\n" + json.dumps(payload, ensure_ascii=False, indent=2) + "\n```"


def cmd_watch(args: argparse.Namespace) -> int:
    """Register open items for watched authors and recently closed items within the configured window. This command never writes the runs sidecar or advances the watermark; reporting alone owns it. Skip the closed sweep for authors with no prior work in this store, because older terminal items are not new activity."""
    state_file, notes = resolve_state_file(args.state_file, args.channel)
    config_file = config_path(state_file)
    config, config_notes = load_config(config_file)
    notes += config_notes
    repositories = Repositories.from_config(config.get("repositories"), notes)
    watch_config = config.get("watch") or {}
    when = format_utc(now_utc())

    result: dict[str, Any] = {
        "store": store_label(state_file),
        "config_file": config_file.name,
        "configured": bool(config),
        "repositories_configured": repositories.slugs,
        "notes": notes,
        "authors_searched": [],
        "authors_unsearchable": [],
        "authors_disabled": [],
        "authors_not_reached": [],
        "authors_first_seen": [],
        "registered": [],
        "already_tracked": [],
        "declined_over_cap": [],
        "dry_run": bool(args.dry_run),
    }

    targets, disabled, plan_notes = plan_watch_targets(
        watch_config,
        cli_authors=args.author,
        cli_repos=args.repo,
        cli_kinds=args.kind,
        cli_state=args.state,
        default_repos=repositories.slugs,
    )
    result["notes"] += plan_notes
    result["authors_disabled"] = disabled

    if not targets:
        # A channel that watches nobody is a configuration, not a fault. It says
        # so and stops, having touched neither the store nor the sidecars.
        result["notes"].append(
            "no authors to watch, so nothing was searched and nothing was "
            "registered."
        )
        if not config:
            print(
                "note: this store has no configuration, so the watch did "
                "nothing. " + config_help(config_file),
                file=sys.stderr,
            )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    closed_since = watch_closed_since(
        watch_config.get("window_days"), args.since, now=now_utc()
    )
    result["closed_since"] = closed_since or "no floor"
    if closed_since is None:
        result["notes"].append(
            "the closed sweep has no floor, so it asks for each author's whole "
            f"closed history and stops at {GITHUB_SEARCH_RESULT_CAP} results "
            "with nothing to say that it did."
        )

    # Read before any search so that a login's first sweep is decided by what the
    # store held when the run started, not by what this same run has just put in
    # it. Read-only, and outside the lock: nothing here writes.
    known_logins = logins_already_watched(Ledger.load(state_file).rows)
    token = resolve_token(args.token_env)
    # (link, the watched login it was found under), so the registration can
    # stamp the source with who it came from.
    links: list[tuple[ParsedLink, str]] = []
    rate_limited: str | None = None
    for target in targets:
        first_sight = target.login.lower() not in known_logins
        if first_sight:
            if target.label not in result["authors_first_seen"]:
                result["authors_first_seen"].append(target.label)
            # Open work only. Everything they have open registers however old it
            # is -- a pull request open for months is the one most worth
            # tracking, and a recency bound would be exactly wrong here.
            target = WatchTarget(
                login=target.login,
                repo=target.repo,
                kinds=target.kinds,
                state="open",
            )
        if rate_limited:
            result["authors_not_reached"].append(target.label)
            continue
        try:
            found, outcome = search_watch_target(
                target,
                closed_since,
                token=token,
                api_base=args.github_api,
                max_results=args.max_per_author,
            )
        except SearchRefused as refused:
            # Never folded into "no results". A login the endpoint will not
            # search is a list that needs fixing; an author who happened to open
            # nothing is a quiet week, and the two are indistinguishable in a
            # total.
            result["authors_unsearchable"].append(
                {"author": target.login, "repo": target.repo, "reason": str(refused)}
            )
            print(
                f"warning: {target.label} could not be searched: {refused} "
                "This login registered nothing, which is not the same as having "
                "opened nothing. Check the spelling, and note that a machine "
                "account is searched under its application spelling rather than "
                "its bare name.",
                file=sys.stderr,
            )
            continue
        except SearchRateLimited as limited:
            rate_limited = str(limited)
            result["authors_not_reached"].append(target.label)
            print(
                f"warning: the search rate limit stopped this pass at "
                f"{target.label}: {limited}. Everything already found is "
                "registered; the authors not reached are named in the output, "
                "and the next run covers them because registering is an upsert "
                "and the open sweep has no floor to slide past them.",
                file=sys.stderr,
            )
            continue
        outcome["first_sight"] = first_sight
        result["authors_searched"].append(outcome)
        links.extend((link, target.login) for link in found)
    if rate_limited:
        result["rate_limited"] = rate_limited

    with file_lock(lock_path(state_file)):
        ledger = Ledger.load(state_file)
        # Read-only on the runs sidecar: the owner check is the one thing this
        # command needs from it, and taking nothing else is what keeps the
        # watermark out of reach.
        check_owner(read_runs(runs_path(state_file)), args.channel)
        if not ledger.existed and not args.dry_run:
            print(
                f"note: the store {store_label(state_file)} does not exist and is "
                "being created.",
                file=sys.stderr,
            )
        if ledger.corrupt:
            result["corrupt_lines"] = [number for number, _ in ledger.corrupt]
        rows_before = len(ledger.rows)
        result["rows_before"] = rows_before
        known_keys = set(ledger.index())
        # New rows are counted before anything is written, so a cap can decline
        # them rather than a store discovering afterwards that it grew too far
        # in one step. What is declined is listed by URL: a cap nobody can see
        # the effect of is worse than no cap, and these are exactly the items a
        # later run, or a raised cap, has to pick up.
        seen_new: set[str] = set()
        deduped_new: list[ParsedLink] = []
        for link, _ in links:
            if link.key in known_keys or link.key in seen_new:
                continue
            seen_new.add(link.key)
            deduped_new.append(link)
        cap = args.max_new
        if cap is not None and len(deduped_new) > cap:
            over = deduped_new[cap:]
            result["declined_over_cap"] = [link.url for link in over]
            declined = {link.key for link in over}
            links = [pair for pair in links if pair[0].key not in declined]
            print(
                f"warning: this run would have added {len(deduped_new)} new "
                f"items and --max-new is {cap}, so {len(over)} were declined and "
                "are listed in the output. They are not lost: registering is an "
                "upsert, so a later run with a higher cap picks them up "
                "unchanged.",
                file=sys.stderr,
            )
        if args.dry_run:
            result["would_register"] = [link.url for link in deduped_new]
            result["would_register_count"] = len(deduped_new)
            result["rows_after"] = rows_before + len(deduped_new)
            result["notes"].append(
                "--dry-run: the searches ran, the store was read to work out "
                "which items are new, and nothing was written."
            )
        else:
            for link, found_under in links:
                row, outcome = upsert_registration(
                    ledger,
                    link,
                    via="author_watch",
                    slack_ts=None,
                    permalink=None,
                    author=None,
                    when=when,
                    watch_author=found_under,
                )
                entry = {"key": row["key"], "url": row["url"], "kind": row["kind"]}
                if row.get("lifecycle") == "ignored":
                    entry["note"] = "row is untracked; the watch did not revive it"
                result[
                    "registered" if outcome == "registered" else "already_tracked"
                ].append(entry)
            ledger.write()
            result["rows_after"] = len(ledger.rows)

        # The scheduled reply, built once the store reflects this run rather
        # than off `rows_before` -- a registration this run made is part of
        # what "tracked" means by the time anyone reads the card. Not built
        # for --dry-run: that path is the operator's own pre-flight read of
        # `would_register_count`, described in `references/prompts.md`, not
        # a run whose card is ever sent anywhere.
        if not args.dry_run:
            result["card"] = render_watch_card(
                registered_count=len(result["registered"]),
                tracked_count=sum(
                    1 for row in ledger.rows if row.get("lifecycle") == "active"
                ),
                repo_count=len({target.repo for target in targets}),
                author_count=len({target.login for target in targets}),
            )

    print(json.dumps(result, ensure_ascii=False, indent=2))
    # A login that cannot be searched, a pass cut short, or an item declined by
    # the cap is reported as a failure even though everything found was written:
    # each one means the store is missing items that nobody would otherwise
    # notice are missing.
    if result["authors_unsearchable"] or rate_limited or result["declined_over_cap"]:
        return 1
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    state_file, notes = resolve_state_file(args.state_file, args.channel)
    # Refuse an absent store before creating sidecars.
    refuse_absent_store(state_file, args.allow_missing_store)
    # Clear stale output even if refresh fails.
    run_dir = prepare_run_dir(state_file)
    config, config_notes = load_config(config_path(state_file))
    notes += config_notes
    repositories = Repositories.from_config(config.get("repositories"), notes)
    # Validate the filter and use the configured repository spelling throughout.
    args.repo = resolve_repo_filter(args.repo, repositories, state_file)
    token = resolve_token(args.token_env)
    # Validate the signature file before creating store sidecars.
    signatures, signature_notes = load_flake_signatures(
        Path(args.flake_signatures) if args.flake_signatures else None
    )
    notes += signature_notes
    cutoff = now_utc()
    # Start the time budget before loading the ledger.
    started = time.monotonic()
    run_id = args.run_id or f"manual-{int(cutoff.timestamp())}"
    stream = stream_key(args.repo)
    coverage: list[str] = []

    with file_lock(lock_path(state_file)):
        ledger = Ledger.load(state_file)
        runs = read_runs(runs_path(state_file))
        check_owner(runs, args.channel)
        store_created = not ledger.existed
        streams = runs.setdefault("streams", {})
        bookkeeping = streams.setdefault(stream, {})
        if bookkeeping.get("last_completed_run_id") == run_id:
            print(
                f"run {run_id} already delivered a report for stream {stream}; "
                "nothing posted.",
                file=sys.stderr,
            )
            return 0
        if bookkeeping.get("pending"):
            coverage.append(
                "The previous run left an uncommitted report: it either died "
                "before delivery or was never committed. Its watermark was not "
                "advanced, so anything it would have said is repeated below."
            )
        since = parse_utc(bookkeeping.get("last_completed_run_utc"))
        run_index = int(runs.get("completed_runs") or 0)
        runs.setdefault("schema_version", SCHEMA_VERSION)
        if args.channel:
            runs.setdefault("channel_id", args.channel)
        if args.cron_job_id:
            runs["cron_job_id"] = args.cron_job_id
        bookkeeping["last_attempt_utc"] = format_utc(cutoff)
        write_runs(runs_path(state_file), runs)

    if store_created:
        print(
            f"note: the store {store_label(state_file)} did not exist and "
            "--allow-missing-store was given, so this reports against an empty "
            "store: nothing is tracked and nothing can have changed. Say so "
            "when delivering it, or the report reads as a quiet week.",
            file=sys.stderr,
        )
    for note in notes:
        print(f"note: {note}", file=sys.stderr)

    active = [
        row
        for row in ledger.rows
        if row.get("lifecycle") in {"active", "terminal_pending"}
        and (not args.repo or (row.get("repo") or "").lower() == args.repo.lower())
    ]
    budget = max(0, int(args.max_seconds))
    workers = max(1, int(args.refresh_workers))
    deadline = (started + budget) if budget else None

    recheck_hours = max(0, int(args.pairing_recheck_hours))
    # Replaced rather than reconfigured, so a run that names a different ceiling
    # starts counting from nothing instead of inheriting a window it never used.
    globals()["gitcode_rate"] = RequestRate(
        args.gitcode_requests_per_minute, GITCODE_RATE_WINDOW_SECONDS
    )
    listings: dict[str, ProjectListing] = {}

    def refresh_one(row: dict[str, Any]) -> None:
        try:
            refresh_row(
                row,
                token=token,
                github_api=args.github_api,
                gitcode_api=args.gitcode_api,
                gitcode_project_override=args.gitcode_project,
                max_pages=args.max_pages,
                signatures=signatures,
                fetch_artifact=not args.no_ci_artifact,
                repositories=repositories,
                deadline=deadline,
                listings=listings,
                recheck_hours=recheck_hours,
            )
        except Exception as error:  # noqa: BLE001 - one bad row must not lose the run
            row["refresh_ok"] = False
            row["refresh_error"] = str(error)

    if active:
        print(
            f"refreshing {len(active)} item(s), {workers} at a time"
            + (
                f", stopping after {budget}s and reporting what it has"
                if budget
                else ", with no time bound (--max-seconds 0)"
            )
            + ".",
            file=sys.stderr,
        )
    # Before the rows, not during them. The second tracker's listing answers
    # every row of a project at once, and reading it once per project is what
    # keeps that tracker out of the per-row cost -- the refresh below then goes
    # to the network only for what the listing did not answer. A row it cannot
    # answer takes the per-row path unchanged, so this phase can make a run
    # cheaper and cannot make it less complete.
    listings.update(
        read_project_listings(
            active,
            gitcode_api=args.gitcode_api,
            repositories=repositories,
            project_override=args.gitcode_project,
            max_pages=args.max_pages,
            recheck_hours=recheck_hours,
            workers=workers,
            deadline=deadline,
        )
    )
    for listing in listings.values():
        if listing.error:
            print(
                f"note: the second tracker's listing for {listing.project} could "
                f"not be read ({listing.error}); its items are refreshed one at a "
                "time instead, which is slower and not less complete.",
                file=sys.stderr,
            )
    unrefreshed = refresh_within_budget(
        active, refresh_one, workers=workers, deadline=deadline
    )
    unrefreshed_ids = {id(row) for row in unrefreshed}
    for row in unrefreshed:
        # Written into the row, not only counted. It is the truth about the row
        # -- this run did not refresh it -- and it is what keeps the stamping
        # below, which trusts `refresh_ok`, from promoting an item to terminal
        # on facts that predate the run claiming it.
        row["refresh_ok"] = False
        row["refresh_error"] = (
            "not refreshed: the run's refresh budget ran out before reaching this item"
        )
    github_down = sum(
        1
        for row in active
        if id(row) not in unrefreshed_ids and row.get("refresh_ok") is False
    )

    merged = coalesce_pairs(
        ledger, pairing_from_rows(ledger.rows, repositories), repositories
    )
    # Over the rows this run refreshed, never over the whole store. `refresh_ok`
    # is a fact the *last* run to touch a row left behind, so a row outside this
    # run's reach -- another repository under `--repo`, most of all -- carries a
    # true `refresh_ok` from a run that is over. Stamping it terminal here
    # promotes it on stored facts, and the next commit retires it having never
    # reported it: the item is gone from the store's attention and was never in
    # anybody's report. Budget-skipped rows are already safe, because this run
    # wrote `refresh_ok = False` into them above; the filtered-out rows are not,
    # because this run never wrote anything into them at all.
    surviving = {id(row) for row in ledger.rows}
    for row in active:
        if id(row) not in surviving:
            continue  # folded into its pair by the collapse just above
        if row.get("lifecycle") == "active" and row.get("refresh_ok") and is_terminal(row):
            # Stamped during the refresh so the item appears in *Closed and
            # landed* exactly once, on the run that observed the terminal state;
            # that run's commit then retires it.
            row["lifecycle"] = "terminal_pending"
            row["terminal_observed_utc"] = format_utc(cutoff)
    if merged:
        coverage.append(
            "Merged "
            + ", ".join(f"`{source}` into `{target}`" for source, target in merged)
            + " once the sync pairing was proved."
        )

    with file_lock(lock_path(state_file)):
        ledger.write()

    rows_by_key = {row["key"]: row for row in ledger.rows}
    newly_tracked: list[dict[str, Any]] = []
    changed: list[tuple[dict[str, Any], list[tuple[str, str]]]] = []
    still_waiting: list[dict[str, Any]] = []
    terminal: list[dict[str, Any]] = []
    unrendered: list[dict[str, Any]] = []

    def reached(row: dict[str, Any]) -> bool:
        """Return whether this run established the row's current state. A row that was not refreshed or whose tracker refused the request still has stale stored facts, so exclude it from the report. Also exclude it from the receipt; otherwise it could be announced repeatedly without being stamped."""
        return id(row) not in unrefreshed_ids and row.get("refresh_ok") is not False

    reported_rows = [row for row in active if reached(row)]

    for row in reported_rows:
        if row.get("key") not in rows_by_key:
            continue
        if row.get("observed_through_utc") is None:
            newly_tracked.append(row)
            continue
        deltas = compute_delta(row)
        if row.get("lifecycle") == "terminal_pending":
            terminal.append(row)
            continue
        if deltas:
            changed.append((row, deltas))
        elif should_resurface(row, run_index, args.resurface_after):
            still_waiting.append(row)

    changed.sort(key=lambda entry: (rank_of(entry[1]), entry[0].get("key") or ""))
    for row in reported_rows:
        title = str(row.get("title_source") or "")
        if title and contains_foreign_script(title) and not row.get("title_rendered"):
            unrendered.append(row)

    if unrefreshed:
        coverage.append(
            f"{len(unrefreshed)} tracked item(s) were not refreshed: the run's "
            f"refresh budget of {budget}s ran out before it reached them. They are "
            "left out of every section above, the roster included, rather than "
            "reported from stored facts, and their watermark was not advanced, so "
            "the next run reports whatever changed on them. A report this run is "
            "short is a report that stopped early, not a quiet week."
        )
    if github_down:
        coverage.append(
            f"{github_down} item(s) failed to refresh and are omitted from every "
            "section above, the roster included; their stored state is not "
            "presented as current. Their watermark was not advanced either, so "
            "the next run reports whatever changed on them once its refresh "
            "succeeds, and they are retried through the ordinary refresh path."
        )
    if ledger.corrupt:
        coverage.append(
            "Unparseable ledger line(s) at "
            + ", ".join(str(number) for number, _ in ledger.corrupt)
            + "; they were skipped for reading and left in place, not rewritten."
        )
    # Grouped by label, not one bullet per row: a label applied across a whole
    # backlog is one fact about the tracker's label set, not one fact per item,
    # and a report naming every item individually crowds out everything else in
    # it. Read off `reported_rows`, the same set every other section above
    # reports from -- a row the budget never reached or whose refresh failed
    # has no current state to carry a label from, and naming it here would
    # announce a row the report says it omitted.
    unknown_labels: dict[str, list[dict[str, Any]]] = {}
    for row in reported_rows:
        for label in row.get("unknown_labels") or []:
            unknown_labels.setdefault(label, []).append(row)
    for label, rows in unknown_labels.items():
        items = ", ".join(item_label(row) for row in rows)
        coverage.append(f"Unknown label `{label}` on {items}, carried as-is.")

    context = ReportContext(
        state_file=state_file,
        row_count=len(ledger.rows),
        tracked=len([row for row in ledger.rows if row.get("lifecycle") == "active"]),
        since=since,
        generated=cutoff,
        run_id=run_id,
        channel=args.channel or "",
        repo_filter=args.repo,
        full=bool(args.full),
        warnings=notes,
        coverage=coverage,
        store_created=store_created,
        routes=tuple(store_routes(ledger.rows)),
        unrefreshed=len(unrefreshed),
        refresh_attempted=len(active),
        refresh_budget=budget,
        repository_summaries=compute_repository_summaries(
            ledger.rows, newly_tracked, changed, terminal, repositories, args.repo
        ),
    )
    # The roster is the same claim as every section above, made about every row
    # at once, so it obeys the same rule. A row this run did not reach renders
    # there as a status and a blank change column, which reads as an item in a
    # known state and nothing to say about it -- directly under the line saying
    # those items are left out because their stored state is not current.
    roster = reported_rows if args.full else None
    text = render_report(
        context, newly_tracked, changed, still_waiting, terminal, roster, unrendered
    )

    reported_keys = [
        *(row["key"] for row in newly_tracked),
        *(row["key"] for row, _ in changed),
        *(row["key"] for row in still_waiting),
        *(row["key"] for row in terminal),
    ]
    receipt = build_receipt(reported_keys, rows_by_key, format_utc(cutoff), run_index + 1)

    with file_lock(lock_path(state_file)):
        runs = read_runs(runs_path(state_file))
        streams = runs.setdefault("streams", {})
        bookkeeping = streams.setdefault(stream, {})
        bookkeeping["pending"] = {
            "run_id": run_id,
            "run_cutoff_utc": format_utc(cutoff),
            "receipt": receipt,
        }
        write_runs(runs_path(state_file), runs)

    # The receipt is pending from here on. Everything below decides whether this
    # run gets to keep it: the report has to reach the caller first.
    # Written every time, and to a path the caller did not have to invent. The
    # next two steps both need the file -- the language check takes it as an
    # argument, the reply is its contents -- and neither can be handed a path by
    # the command that produced it, because each of them is a fresh shell.
    output = Path(args.output) if args.output else run_dir / "report.md"
    try:
        output.write_text(text, encoding="utf-8")
    except OSError as error:
        raise SystemExit(
            f"the report rendered but could not be written to {output} "
            f"({error}). Nothing was committed, so the next run repeats "
            "this delta rather than losing it."
        ) from error
    if unrefreshed:
        print(
            f"\nPARTIAL: {len(unrefreshed)} of {len(active)} item(s) were not "
            f"refreshed within --max-seconds {budget}. The report above says so, "
            "in the brief and in Coverage and gaps, and it is still the report to "
            "deliver. Reissuing this command unchanged spends the same budget on "
            "the same store and produces the same partial run; raise "
            "--refresh-workers, or --max-seconds if the caller's own timeout is "
            "comfortably above it.",
            file=sys.stderr,
        )
    sys.stdout.write(text)
    sys.stdout.flush()
    # On stderr, because stdout is the report itself. Printed so the later steps
    # use this path exactly as given rather than rebuilding it: a rebuilt path is
    # a mistyped path, and a command that names a file which is not there fails,
    # gets retried unchanged, and takes the run with it.
    print(f"\nreport written to {output}", file=sys.stderr)

    if not args.commit_after:
        print(
            "\n---\n"
            "Not yet committed. Deliver the report above to Slack first, then run "
            "this -- the store path below is an argument for the command, never "
            "something to paste into the channel:\n"
            f"  {shlex.quote(sys.executable)} {Path(__file__).resolve()} commit "
            f"--state-file {state_file} --channel {args.channel} --run-id {run_id}"
            + (f" --repo {args.repo}" if args.repo else "")
            + "\nIf delivery fails, do not commit: the same delta is then repeated by "
            "the next run rather than lost.",
            file=sys.stderr,
        )
        return 0

    applied = commit_pending(state_file, args.channel, stream, run_id)
    print(
        "\n---\n"
        "Committed by this run: the watermark has already moved, so there is no "
        "command to run afterwards and nothing here to copy. The delta above is "
        "reported once and will not be repeated.",
        file=sys.stderr,
    )
    print(commit_receipt_json(state_file, run_id, applied or []), file=sys.stderr)
    return 0


def commit_pending(
    state_file: Path, channel: str, stream: str, run_id: str
) -> list[str] | None:
    """Apply the pending receipt for `run_id` and advance the watermark.

    Shared by the `commit` subcommand and by `report --commit-after`, so the two
    paths cannot drift into two ideas of what committing means. Returns the keys
    it advanced, or None when this run id was already committed -- an idempotent
    re-run, not a failure.
    """
    when = format_utc(now_utc())
    with file_lock(lock_path(state_file)):
        ledger = Ledger.load(state_file)
        runs = read_runs(runs_path(state_file))
        check_owner(runs, channel)
        bookkeeping = runs.setdefault("streams", {}).setdefault(stream, {})
        pending = bookkeeping.get("pending")
        if not pending:
            if bookkeeping.get("last_completed_run_id") == run_id:
                return None
            raise SystemExit(
                f"no pending report for stream {stream}. Run `report` first, "
                "deliver it, and commit the run id it printed."
            )
        if pending.get("run_id") != run_id:
            raise SystemExit(
                f"pending report is for run {pending.get('run_id')}, not "
                f"{run_id}. Nothing was committed."
            )
        applied = apply_receipt(ledger, pending.get("receipt") or {}, when)
        ledger.write()
        bookkeeping["last_completed_run_utc"] = pending.get("run_cutoff_utc") or when
        bookkeeping["last_completed_run_id"] = run_id
        bookkeeping["pending"] = None
        bookkeeping["consecutive_failures"] = 0
        runs["completed_runs"] = int(runs.get("completed_runs") or 0) + 1
        write_runs(runs_path(state_file), runs)
    return applied


def commit_receipt_json(state_file: Path, run_id: str, applied: list[str]) -> str:
    return json.dumps(
        {
            "committed": run_id,
            "items": applied,
            "store": store_label(state_file),
        },
        ensure_ascii=False,
        indent=2,
    )


def cmd_commit(args: argparse.Namespace) -> int:
    state_file, _ = resolve_state_file(args.state_file, args.channel)
    applied = commit_pending(state_file, args.channel, stream_key(args.repo), args.run_id)
    if applied is None:
        print(f"run {args.run_id} was already committed; nothing to do.")
        return 0
    print(commit_receipt_json(state_file, args.run_id, applied))
    return 0


def cmd_audit(args: argparse.Namespace) -> int:
    """Report stored ``sources[]`` entries whose ``slack_ts`` is invalid. This command is read-only: it takes no lock and creates no store or sidecar files. Atomic whole-file replacement lets it read either the old or new store during a concurrent write. Exit with 0 for a clean audit, 1 for findings, and 2 when no store exists."""
    state_file, notes = resolve_state_file(args.state_file, args.channel)
    ledger = Ledger.load(state_file)
    if not ledger.existed:
        # Decided on what the load actually saw rather than on a prior `exists()`,
        # so there is no window between the two in which the answer changes.
        #
        # Deliberately not `refuse_absent_store`. Not because a report and an
        # audit should differ over an absent store -- both must stop -- but
        # because that helper raises, and a raise leaves through exit 1, which
        # this command has already spent on "something was found". Reusing it
        # would make "malformed provenance exists" and "nothing was read"
        # indistinguishable to precisely the gate this is for. Its *diagnosis* is
        # reused, which is the part that must not drift between the two.
        #
        # There is no `--allow-missing-store` here either, and that asymmetry is
        # the point rather than an omission. That flag exists on `report` because
        # a report over an empty store is a legitimate artefact someone may
        # actually want delivered. A clean audit verdict over a file that was
        # never opened is not an artefact anyone wants; it is the failure itself,
        # so there is nothing for a flag to authorise.
        absent: dict[str, Any] = {
            "store": store_label(state_file),
            "store_exists": False,
            "audited": False,
            "notes": notes,
            "store_absent_why": describe_absent_store(state_file),
            "store_absent_remedy": (
                "Nothing was read, so nothing could be found, and this is not a "
                "clean audit -- it is the absence of one. Check the path before "
                "anything else. If this channel has genuinely registered nothing "
                "yet then there is nothing to audit and never was: register "
                "something -- `track` and `watch` each create the store -- and "
                "the audit then has rows to answer over."
            ),
        }
        # The findings keys are *left out* rather than emitted empty, which is the
        # half of this that a caller cannot ignore. `malformed_slack_ts: []`
        # beside `store_exists: false` is a clean verdict in every respect that
        # anything reads, human or scripted; an absent key cannot be mistaken for
        # a finding of nothing.
        print(json.dumps(absent, ensure_ascii=False, indent=2))
        print(
            f"the store {store_label(state_file)} does not exist, so nothing was "
            "audited and this is not a clean result. See the JSON on stdout for "
            "what is around the path.",
            file=sys.stderr,
        )
        return 2
    findings = audit_provenance(ledger.rows)
    result: dict[str, Any] = {
        "store": store_label(state_file),
        "store_exists": True,
        "audited": True,
        "notes": notes,
        "rows": len(ledger.rows),
        "expected": SLACK_TS_SHAPE,
        "malformed_slack_ts": findings["stored"],
        "refused_slack_ts": findings["refused"],
    }
    if findings["stored"]:
        result["malformed_remedy"] = (
            "These entries carry a slack_ts that is not one. They were written "
            "before the value was checked -- nothing can write one now -- and "
            "nothing here rewrites them, because a value invented once cannot be "
            "recovered into the real one and overwriting it would swap one "
            "unverifiable claim for another. Either leave them and treat their "
            "slack_ts as unknown, or, for an item whose provenance matters, "
            "`track --unregister <key>` and have the link reposted so it "
            "registers again with real metadata."
        )
    if findings["refused"]:
        result["refused_remedy"] = (
            "These registrations arrived with a fabricated slack_ts; the value "
            "was refused and the item registered without it, so the rows are "
            "honest. The fault is upstream: something is still passing "
            "--slack-ts a value it cannot actually obtain. Stop it asking."
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if (findings["stored"] or findings["refused"]) else 0


def cmd_title(args: argparse.Namespace) -> int:
    state_file, _ = resolve_state_file(args.state_file, args.channel)
    with file_lock(lock_path(state_file)):
        ledger = Ledger.load(state_file)
        row = ledger.index().get(args.key)
        if row is None:
            raise SystemExit(f"no row with key {args.key}")
        row["title_rendered"] = args.rendered
        ledger.write()
    print(json.dumps({"key": args.key, "title_rendered": args.rendered}, indent=2))
    return 0


# ------------------------------------------------------------------------ CLI


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    def common(target: argparse.ArgumentParser) -> None:
        target.add_argument(
            "--state-file",
            help="the ledger; sidecars .runs.json and .lock derive from it. "
            "State it absolutely.",
        )
        target.add_argument("--channel", default="", help="Slack channel id this store serves")

    track = sub.add_parser("track", help="register or unregister items (no network)")
    common(track)
    track.add_argument("--message-text", help="raw Slack message body")
    track.add_argument("--message-file", help="file holding the raw message body, or -")
    track.add_argument("--url", action="append", help="register this URL explicitly")
    track.add_argument("--unregister", action="append", help="URL or key to stop tracking")
    track.add_argument(
        "--slack-ts",
        help="the message id Slack issued, <10 digits>.<6 digits>. Omit it "
        "rather than deriving one: anything else is refused, reported and not "
        "stored, and the item registers without provenance.",
    )
    track.add_argument(
        "--slack-permalink",
        help="the permalink Slack issued; anything not an https:// slack.com "
        "URL is refused the same way",
    )
    track.add_argument("--slack-author")
    track.add_argument(
        "--via",
        default="auto_link",
        choices=("auto_link", "operator", "reconcile"),
    )
    track.add_argument(
        "--render-table",
        action="store_true",
        help=(
            "after the JSON, print the registered items as a Markdown table of "
            "URL, Tracker, Repository, Number, Title, Author, State, Created and "
            "Last activity -- every one of them read from the store, so this "
            "costs no tracker call and needs no second command. A run that "
            "registered nothing trackable prints no table, and says per URL why "
            "not."
        ),
    )
    track.set_defaults(func=cmd_track)

    query = sub.add_parser(
        "query",
        help="read-only: count or list tracked items. Exits 2 when there was no "
        "store to read, which is never reported as a count of zero.",
    )
    common(query)
    query.add_argument(
        "--lifecycle",
        action="append",
        help="this skill's own tracking state: active, terminal_pending, "
        "retired or ignored. Repeatable. Not the tracker's state.",
    )
    query.add_argument(
        "--gh-state",
        action="append",
        help="the first tracker's own state: open or closed. Repeatable. It "
        "never holds 'merged' -- a merged pull request is closed there too, so "
        "use --merged for that.",
    )
    query.add_argument(
        "--kind", action="append", help="pull_request, issue or other. Repeatable."
    )
    query.add_argument(
        "--status",
        action="append",
        help="the derived status the report shows, such as awaiting_review or "
        "ci_failed. Repeatable. 'landed' means the second tracker's merge alone; "
        "--merged is the wider question.",
    )
    query.add_argument("--repo", help="only rows in this owner/name repository")
    query.add_argument(
        "--merged",
        action="store_true",
        help="only items that merged, on either tracker: gh_merged_at set, or "
        "the second tracker's merged_at",
    )
    query.add_argument(
        "--unmerged", action="store_true", help="only items that did not merge"
    )
    query.add_argument(
        "--format",
        default="summary",
        choices=("summary", "count", "table", "json"),
        help="summary (the default) is a census of the whole store: how many "
        "merged, and the spread of every state field. count prints the number "
        "and where it came from. table is the nine-column table. json is the "
        "same rows for a caller that will process them.",
    )
    query.add_argument(
        "--limit",
        type=int,
        default=50,
        help="most rows to show under --format table or json; 0 shows every "
        "one. The count and the summary always cover every matched row.",
    )
    query.set_defaults(func=cmd_query)

    watch = sub.add_parser(
        "watch",
        help="register what the watched authors have open (registration only; "
        "never writes a receipt and never advances the watermark)",
    )
    common(watch)
    watch.add_argument(
        "--author",
        action="append",
        help="watch this login for this run instead of the ones in the watch "
        "list; repeat it. The list itself is read from the configuration's "
        "`watch.authors`, derived from the store's own name with .jsonl "
        "replaced by .config.json.",
    )
    watch.add_argument(
        "--repo",
        action="append",
        help="search this owner/name; repeat it. Defaults to the repositories "
        "this script knows a tracker pairing for.",
    )
    watch.add_argument(
        "--kind",
        action="append",
        choices=WATCH_KINDS,
        help="register only this kind; repeat it. Both by default.",
    )
    watch.add_argument(
        "--state",
        choices=WATCH_STATES,
        help="`open` skips the closed sweep entirely for every author",
    )
    watch.add_argument(
        "--since",
        help="how far back the closed sweep reaches: a number of days, an ISO "
        "date, or `all`. It bounds which items are registered and has no "
        "bearing on which are reported.",
    )
    watch.add_argument(
        "--max-per-author",
        type=int,
        default=DEFAULT_WATCH_MAX_PER_AUTHOR,
        help="stop after this many items per author and say the result was cut",
    )
    watch.add_argument(
        "--max-new",
        type=int,
        help="refuse to add more than this many new items in one run, listing "
        "what was declined. Nothing is lost: a later run picks them up.",
    )
    watch.add_argument(
        "--dry-run",
        action="store_true",
        help="search and say what would register, writing nothing",
    )
    watch.add_argument("--token-env", default="GITHUB_TOKEN")
    watch.add_argument("--github-api", default="https://api.github.com")
    watch.set_defaults(func=cmd_watch)

    report = sub.add_parser("report", help="refresh, diff and render the delta")
    common(report)
    report.add_argument("--token-env", default="GITHUB_TOKEN")
    report.add_argument("--github-api", default="https://api.github.com")
    report.add_argument("--gitcode-api", default="https://api.gitcode.com/api/v5")
    report.add_argument("--gitcode-project", help="override the GitCode project slug")
    report.add_argument("--repo", help="only report items from this owner/name")
    report.add_argument("--full", action="store_true", help="also render the roster")
    report.add_argument(
        "--run-id",
        help="the scheduler's id for this tick; suppresses an exact double-fire",
    )
    # `--cron-job-id` is the older spelling and stays accepted: it is written into
    # stores in the field, and renaming a flag a running deployment may pass is a
    # breakage with no upside. `--schedule-id` is what new callers should use.
    report.add_argument(
        "--schedule-id",
        "--cron-job-id",
        dest="cron_job_id",
        help="the scheduler's id for the schedule itself, recorded for diagnosis",
    )
    report.add_argument(
        "--output",
        help="write the report here instead of into the run directory beside the "
        "store. Rarely needed: the default path is derived from --state-file, so "
        "every step of the run can work it out without being told it.",
    )
    report.add_argument("--max-pages", type=int, default=6)
    report.add_argument(
        "--gitcode-requests-per-minute",
        type=int,
        default=GITCODE_REQUESTS_PER_MINUTE,
        help=(
            "how often the second tracker may be called. Its ceiling is per "
            "user and per minute, it is stated only in the text of a refusal, "
            "and no credential raises it, so this is a number about the remote "
            "rather than about this deployment: change it when the remote's "
            "changes. Set it below what the remote allows, not at it -- the "
            "window is the remote's and this one cannot be aligned with it."
        ),
    )
    report.add_argument(
        "--pairing-recheck-hours",
        type=int,
        default=DEFAULT_PAIRING_RECHECK_HOURS,
        help=(
            "how long an established 'this item has no merge request' is read "
            "back out of the store before it is established again. Searching "
            "for a merge request that is not there costs the whole listing, so "
            "an unread finding is that cost on every run forever; a finding kept "
            "forever instead would miss the mirror being created later. 0 "
            "searches every run."
        ),
    )
    report.add_argument(
        "--max-seconds",
        type=int,
        default=DEFAULT_REFRESH_BUDGET_SECONDS,
        help=(
            "stop refreshing after this many seconds and report what was "
            "refreshed, saying in the report how many items were not. The point "
            "is to finish inside whatever timeout the caller has: a run killed "
            "by that timeout produces no report, no receipt and no diagnosis. "
            "0 removes the bound, for an operator watching the command run."
        ),
    )
    report.add_argument(
        "--refresh-workers",
        type=int,
        default=DEFAULT_REFRESH_WORKERS,
        help=(
            "how many items to refresh at a time. A refresh is network latency "
            "and nothing else, so this is what makes a run of any size finish; "
            "raise it before raising --max-seconds."
        ),
    )
    report.add_argument("--resurface-after", type=int, default=DEFAULT_RESURFACE_RUNS)
    report.add_argument(
        "--flake-signatures",
        help="signatures to classify CI failures against, instead of the ones "
        "shipped with the skill. A path given here and not found stops the run: "
        "carrying on would leave every red build reported as matching nothing "
        "recorded, which reads the same as having checked.",
    )
    report.add_argument("--no-ci-artifact", action="store_true")
    report.add_argument(
        "--commit-after",
        action="store_true",
        help=(
            "commit this run in the same process, once the report has been "
            "rendered and written; suppresses the commit epilogue"
        ),
    )
    report.add_argument(
        "--allow-missing-store",
        action="store_true",
        help=(
            "report against a store that does not exist yet, which otherwise "
            "stops the run. Without it a wrong path cannot render as a "
            "plausible empty report. Say it only when an empty report is the "
            "intended output."
        ),
    )
    report.set_defaults(func=cmd_report)

    commit = sub.add_parser("commit", help="advance the watermark after delivery")
    common(commit)
    commit.add_argument("--run-id", required=True)
    commit.add_argument("--repo")
    commit.set_defaults(func=cmd_commit)

    title = sub.add_parser("title", help="cache a rendered title for one item")
    common(title)
    title.add_argument("--key", required=True)
    title.add_argument("--rendered", required=True)
    title.set_defaults(func=cmd_title)

    audit = sub.add_parser(
        "audit",
        help="read-only: list sources[] entries whose slack_ts is malformed. "
        "Exits 0 clean, 1 on a finding, 2 when there was no store to read -- "
        "which is never reported as clean.",
    )
    common(audit)
    audit.set_defaults(func=cmd_audit)
    return parser


def main(argv: list[str] | None = None) -> int:
    _configure_utf8_stdio()
    args = build_parser().parse_args(argv)
    handler: Callable[[argparse.Namespace], int] = args.func
    return handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
