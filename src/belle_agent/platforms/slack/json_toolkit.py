"""Shared Slack call handling, refusal wording and card vocabulary."""

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

from belle_agent.tools.inputs import string_ids
from belle_agent.tools.workflow import run_targets

from .file_transfer import (
    slack_file_client,
    stream_slack_file,
)
from .retry import (
    DEFAULT_RETRY_AFTER_SECONDS as _DEFAULT_RETRY_AFTER_SECONDS,
)
from .retry import retry_after_seconds as _retry_after_seconds
from .tool_common import (
    _as_mapping,
    _safe_error_code,
    _SlackCallFailure,
    redact_credentials,
)
from .tool_context import (
    CONVERSATION_ID_RE,
    FILE_ID_RE,
    USER_ID_RE,
    SlackToolBinding,
    slack_tool_error,
)

Failure = TypeVar("Failure", bound=_SlackCallFailure)
_MAX_RATE_LIMIT_RETRIES = 2
_MAX_DETAIL_CHARS = 1000


async def slack_text_download(
    url: str,
    *,
    bot_token: str,
    allowed_hosts: frozenset[str],
    byte_limit: int,
    char_limit: int,
    http_client_factory: Callable[[], Any] | None = None,
) -> tuple[int, str, int, bool]:
    """Fetch bounded Slack-hosted text and return status, text, size, truncation."""
    factory = http_client_factory or (lambda: slack_file_client(allowed_hosts))
    chunks: list[bytes] = []
    size = 0
    truncated = False
    async with (
        factory() as client,
        stream_slack_file(
            client,
            url,
            headers={"Authorization": f"Bearer {bot_token}"},
            allowed_hosts=allowed_hosts,
        ) as response,
    ):
        if response.status_code != 200:
            return response.status_code, "", 0, False
        async for chunk in response.aiter_bytes():
            size += len(chunk)
            if size > byte_limit:
                truncated = True
                break
            chunks.append(chunk)
    content = b"".join(chunks).decode("utf-8", "replace")
    total = len(content)
    return 200, content[:char_limit], total, truncated or total > char_limit


#: Maximum untrusted text quoted back in a refusal.
_MAX_ECHOED_CHARS = 60


def slack_echo(value: Any) -> str:
    """One model-supplied value, safe and short enough to quote in a refusal."""
    text, _redacted, _truncated = redact_credentials(value, cap=_MAX_ECHOED_CHARS)
    return text.strip()


def slack_refusal_json(
    logger: logging.Logger,
    resource: str,
    code: str,
    detail: str = "",
    **fields: Any,
) -> str:
    logger.warning("slack %s refused: %s%s", resource, code, f" -- {detail}" if detail else "")
    return slack_tool_error(code, detail, **{key: value for key, value in fields.items() if value})


def slack_failure_detail(
    exc: _SlackCallFailure,
    details: Mapping[str, str],
    *,
    scope: str = "",
) -> str:
    """The sentence a caller acts on, for one refusal from Slack.

    *details* answers the codes an operator or a model can do something about.
    Anything else names the call Slack refused, plus the *scope* that call
    needs where the refusal was about a grant.
    """
    known = details.get(exc.code)
    if known:
        return known
    detail = f"Slack refused {exc.where}"
    if exc.code == "missing_scope" and scope:
        detail += (
            f"; it needs {scope}, which this installation does not hold."
            f" Adding a scope means reinstalling the Slack app"
        )
    return detail


#: Codes that mean an access call named several targets and some subset of them
#: failed. Reaching one is what starts the per-target pass.
PARTIAL_ACCESS_FAILURE_CODES = frozenset({"failed_to_update_user_ids", "user_not_found", "channel_not_found"})


class SlackRetryingBinding(SlackToolBinding):
    """Retry Slack calls within one deadline."""

    #: Reported when the call's budget runs out.
    TIMEOUT_CODE = "slack_call_timed_out"

    #: Whole-tool deadline, including retries and rate-limit waits.
    DEFAULT_TIMEOUT_SECONDS = 30.0

    def __init__(
        self,
        *,
        timeout_seconds: float | None = None,
        sleep: Callable[..., Any] = asyncio.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._timeout_seconds = max(
            1.0,
            float(self.DEFAULT_TIMEOUT_SECONDS if timeout_seconds is None else timeout_seconds),
        )
        self._sleep = sleep
        self._monotonic = monotonic

    def _deadline(self) -> float:
        """A fresh budget for one tool call."""
        return self._monotonic() + self._timeout_seconds

    def _refusal(self, data: Mapping[str, Any], text: str) -> Exception:
        """One Slack refusal as this toolkit's own exception.

        *data* is the refusal body, which is empty when the SDK raised rather
        than answered; *text* is then what it raised, for a toolkit that reads
        a code out of it.
        """
        raise NotImplementedError

    def _may_wait(self, delay: float | None, retries: int, deadline: float) -> bool:
        """Whether a rate-limit wait fits both the retry count and the deadline."""
        if delay is None or retries >= _MAX_RATE_LIMIT_RETRIES:
            return False
        return self._monotonic() + delay < deadline

    async def _retrying_call(self, make_call: Callable[[], Any], deadline: float) -> dict[str, Any]:
        """One Slack call, waiting out a rate limit while the deadline allows."""
        retries = 0
        while True:
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                raise self._refusal({"error": self.TIMEOUT_CODE}, "")
            try:
                response = await asyncio.wait_for(make_call(), timeout=remaining)
            except TimeoutError:
                raise self._refusal({"error": self.TIMEOUT_CODE}, "") from None
            except Exception as exc:  # noqa: BLE001 - SDK error types vary.
                delay = _retry_after_seconds(exc)
                if self._may_wait(delay, retries, deadline):
                    retries += 1
                    await self._sleep(delay)
                    continue
                raise self._refusal(_as_mapping(getattr(exc, "response", None)), str(exc)) from None

            data = _as_mapping(response)
            if data.get("ok", True) is False:
                refusal = self._refusal(data, "")
                # The same refusal arrives as a raised error or as an ok:false
                # body depending on the SDK version, so both paths retry. This
                # one holds no Retry-After to read.
                if getattr(refusal, "code", "") == "ratelimited" and self._may_wait(
                    _DEFAULT_RETRY_AFTER_SECONDS, retries, deadline
                ):
                    retries += 1
                    await self._sleep(_DEFAULT_RETRY_AFTER_SECONDS)
                    continue
                raise refusal
            return data


class SlackJsonCallFailure(_SlackCallFailure):
    """Keep Slack's detail and dotted method name on JSON API failures."""

    def __init__(
        self,
        code: str,
        method: str = "",
        *,
        subject: str = "",
        detail: str = "",
    ) -> None:
        super().__init__(code, method, subject=subject)
        self.detail = detail

    @property
    def where(self) -> str:
        return super().where if self.method else "a Slack call"


@dataclass(frozen=True)
class SlackAccessRequest:
    method: str
    resource_key: str
    resource_id: str
    kind: str
    wire_kind: str
    ids: tuple[str, ...]
    level: str
    removes: bool

    @property
    def reported_level(self) -> str:
        return "none" if self.removes else self.level

    def payload(self, ids: Sequence[str]) -> dict[str, Any]:
        result = {self.resource_key: self.resource_id, self.wire_kind: list(ids)}
        if self.level:
            result["access_level"] = self.level
        return result


def slack_file_id_error(file_id: str, noun: str) -> "tuple[str, str] | None":
    """Why *file_id* is not an id Slack would accept, or ``None`` when it is.

    *noun* names the thing to the caller, and lower-cased it is the argument the
    refusal code is about -- ``list_id`` for a List, ``canvas_id`` for a canvas.
    """
    if not file_id:
        return (
            f"{noun.lower()}_id_required",
            f"no {noun} id was given. A {noun} id is taken unchanged from a result that reported one",
        )
    if not FILE_ID_RE.match(file_id):
        return (
            f"{noun.lower()}_id_malformed",
            f"a Slack {noun} id is an F followed by upper-case letters and digits, such as F1234ABCD; this is not one",
        )
    return None


def slack_no_request_detail(noun: str) -> str:
    """What to say to a request no Slack path settled."""
    return (
        "this request did not arrive from Slack, so there is no workspace"
        f" for a {noun} to exist in and no token to reach one with"
    )


ACCESS_LEVELS = ("read", "write", "owner")


#: Details for Slack errors that mean the same thing whatever was being reached.
SHARED_FAILURE_DETAIL: Mapping[str, str] = {
    "restricted_action": (
        "this workspace's own rules refuse this action. It is an administrator"
        " setting rather than a missing scope, so reinstalling the app will not"
        " change it"
    ),
    "user_not_found": (
        "one of the user ids names nobody in this workspace. A user id is not"
        " transferable between workspaces, so one taken from elsewhere names"
        " nothing here"
    ),
    "channel_not_found": (
        "one of the chat ids names no conversation this app can see. A"
        " conversation id is not transferable between workspaces, and a private"
        " conversation this app is not in is invisible to it"
    ),
    "not_allowed_token_type": ("this method declines the kind of token this app holds"),
}

#: How Slack's access methods take their targets and levels, for a card that
#: offers them. Shared because it describes the methods rather than the resource.
ACCESS_TARGETS_PROSE = (
    "\nConversations or people, never both in one call. Pass chat_ids "
    "to act on conversations, or user_ids to act on people. Both are "
    "lists. Make two calls when both are needed."
    "\nThe levels are read, write and owner. owner transfers ownership "
    "and applies to exactly one person: it cannot be given to a "
    "conversation, and it cannot be given to several people at once."
    "\nTaking access away. Pass remove instead of a level, with the "
    "same targets."
)

#: What :meth:`SlackJsonToolkitBase._partial_access_result` reports, for a card.
ACCESS_PARTIAL_PROSE = (
    "\nWhen some targets take and others do not. Slack refuses such a "
    "call as a whole and does not say which of them applied, so each "
    "target is then tried on its own and the result lists them "
    "separately: which took, which did not and why, and which were "
    "never determined because the call ran out of time. Read the lists "
    "rather than the overall result."
)

#: The arguments every access card takes. ``user_ids`` is the resource's own,
#: because what it takes depends on how that resource reaches a person.
ACCESS_PARAMS: Mapping[str, Any] = {
    "access_level": {
        "type": "string",
        "enum": list(ACCESS_LEVELS),
        "description": (
            "The level to set. owner applies to one person only. Leave it out and pass remove to take access away."
        ),
    },
    "chat_ids": {
        "type": "array",
        "items": {"type": "string"},
        "description": ("The conversations to set access for. Cannot be given together with user_ids."),
    },
    "remove": {
        "type": "boolean",
        "default": False,
        "description": (
            "Take access away from the named targets instead of setting a level. Pass no access_level with it."
        ),
    },
}


def access_level_error(level: str, remove: bool) -> tuple[str, str] | None:
    if remove and level:
        return (
            "access_level_not_accepted",
            "remove takes access away entirely, so there is no level to set alongside it. Pass one or the other",
        )
    if not remove and not level:
        return (
            "access_level_required",
            (
                "no access level was given. It is one of"
                f" {', '.join(ACCESS_LEVELS)}, or pass remove to take access away instead"
            ),
        )
    if not remove and level not in ACCESS_LEVELS:
        return (
            "access_level_unknown",
            f"{level!r} is not an access level. Slack's three are {', '.join(ACCESS_LEVELS)}",
        )
    return None


def owner_error(level: str, kind: str, count: int, *, resource: str) -> tuple[str, str] | None:
    if level != "owner":
        return None
    if kind in {"chat_ids", "channel_ids"}:
        return (
            "owner_must_be_a_person",
            (
                f"only a person can own a {resource}, and chat_ids names"
                " conversations. Pass the owner as a single entry in user_ids"
            ),
        )
    if count != 1:
        return (
            "owner_is_one_person",
            (
                f"a {resource} has one owner and {count} were named."
                " Ownership transfers to exactly one person; name that"
                " person alone and set the others to read or write"
            ),
        )
    return None


def access_targets(
    chat_ids: Any, user_ids: Any, *, reject_dms: bool = False
) -> tuple[str, list[str], tuple[str, str] | None]:
    """Validate one kind of sharing target, returning a refusal when invalid."""
    rooms, people = string_ids(chat_ids), string_ids(user_ids)
    if rooms is None or people is None:
        return (
            "",
            [],
            (
                "access_targets_malformed",
                "chat_ids and user_ids are each a list of identifiers. One of them was not a list of strings",
            ),
        )
    if rooms and people:
        return (
            "",
            [],
            (
                "access_targets_ambiguous",
                (
                    "chat_ids and user_ids cannot both be given: Slack sets access"
                    " for conversations or for people, never for both in one call."
                    " Make one call for each"
                ),
            ),
        )
    if not rooms and not people:
        return (
            "",
            [],
            (
                "access_targets_required",
                (
                    "no target was named. Pass chat_ids to act on conversations or"
                    " user_ids to act on people; exactly one of the two"
                ),
            ),
        )
    if rooms:
        bad = [room for room in rooms if not CONVERSATION_ID_RE.match(room)]
        if bad:
            return (
                "",
                [],
                (
                    "chat_id_malformed",
                    (
                        "a Slack conversation id is a C, D or G followed by"
                        " upper-case letters and digits."
                        f" {', '.join(sorted(bad))[:200]} is not one"
                    ),
                ),
            )
        dms = [room for room in rooms if room.startswith("D")] if reject_dms else []
        if dms:
            return (
                "",
                [],
                (
                    "chat_id_is_a_direct_message",
                    (
                        "a canvas is not shared with a direct message."
                        f" {', '.join(sorted(dms))} names one. Share it with the"
                        " people in it by passing user_ids instead"
                    ),
                ),
            )
        return "chat_ids", rooms, None
    bad = [who for who in people if not USER_ID_RE.match(who)]
    if bad:
        return (
            "",
            [],
            (
                "user_id_malformed",
                (
                    "a Slack user id is a U or W followed by upper-case letters"
                    f" and digits. {', '.join(sorted(bad))[:200]} is not one"
                ),
            ),
        )
    return "user_ids", people, None


class SlackJsonToolkitBase(SlackToolBinding, Generic[Failure]):
    """Bind Slack requests and handle JSON API calls with deadlines."""

    #: Raised for every Slack failure this toolkit reports.
    _failure_type: type[Failure]

    #: Wall-clock budget for one tool call, including retries.
    DEFAULT_TIMEOUT_SECONDS = 60.0

    #: Give each SDK method call a deadline of its own, rather than sharing the
    #: one budget :meth:`_load_settings` opens for the whole tool call.
    _deadline_per_call = False

    def __init__(
        self,
        *,
        metadata: dict[str, Any] | None = None,
        metadata_provider: Callable[[], Any] | None = None,
        client: Any | None = None,
        workspaces: Any | None = None,
        timeout_seconds: float | None = None,
        sleep: Callable[..., Any] = asyncio.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        http_client_factory: Any = None,
    ) -> None:
        super().__init__(
            metadata=metadata,
            metadata_provider=metadata_provider,
            client=client,
            workspaces=workspaces,
        )
        self._timeout_seconds = max(
            1.0,
            float(self.DEFAULT_TIMEOUT_SECONDS if timeout_seconds is None else timeout_seconds),
        )
        self._sleep = sleep
        self._monotonic = monotonic
        self._http_client_factory = http_client_factory
        self._deadline = 0.0

    async def _load_settings(self, metadata: Mapping[str, Any]) -> Mapping[str, Any]:
        """Bind this call to the install it arrived from, and open its budget.

        Returns resolved settings for that install, so a toolkit
        reading more of the config than the token reads it from one answer.
        """
        slack = await super()._load_settings(metadata)
        self._deadline = self._monotonic() + self._timeout_seconds
        return slack

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        return self._workspaces.client_for(self._bot_token)

    def _remaining(self) -> float:
        return self._deadline - self._monotonic()

    def _timeout_code(self, method: str) -> str:
        return "slack_call_timed_out"

    def _failure(self, data: Mapping[str, Any], method: str) -> Failure:
        code = _safe_error_code(data.get("error"), self._bot_token)
        detail = ""
        raw = data.get("detail")
        if raw:
            detail, _, _ = redact_credentials(raw, bot_token=self._bot_token, cap=_MAX_DETAIL_CHARS)
        return self._failure_type(code, method, detail=detail)

    async def _invoke(
        self,
        method: str,
        make_call: Callable[[Any], Any],
        *,
        deadline: float | None = None,
    ) -> dict[str, Any]:
        try:
            client = self._get_client()
        except _SlackCallFailure as exc:
            if isinstance(exc, self._failure_type):
                exc.method = exc.method or method
                raise
            raise self._failure_type(exc.code, exc.method or method) from None
        retries = 0
        while True:
            remaining = (self._deadline if deadline is None else deadline) - self._monotonic()
            if remaining <= 0:
                raise self._failure_type(self._timeout_code(method), method)
            try:
                response = await asyncio.wait_for(make_call(client), timeout=remaining)
            except TimeoutError:
                raise self._failure_type(self._timeout_code(method), method) from None
            except Exception as exc:  # noqa: BLE001 - Slack SDK exception types vary.
                delay = _retry_after_seconds(exc)
                if delay is not None and retries < _MAX_RATE_LIMIT_RETRIES:
                    retries += 1
                    await self._sleep(delay)
                    continue
                data = _as_mapping(getattr(exc, "response", None))
                raise self._failure(data, method) from None

            data = _as_mapping(response)
            if data.get("ok", True) is False:
                failure = self._failure(data, method)
                if failure.code == "ratelimited" and retries < _MAX_RATE_LIMIT_RETRIES:
                    retries += 1
                    await self._sleep(_DEFAULT_RETRY_AFTER_SECONDS)
                    continue
                raise failure
            return data

    async def _api(self, method: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        body = dict(payload)
        return await self._invoke(method, lambda client: client.api_call(method, json=body))

    async def _call(self, method: str, **kwargs: Any) -> dict[str, Any]:
        deadline = self._monotonic() + self._timeout_seconds if self._deadline_per_call else None
        return await self._invoke(method, lambda client: getattr(client, method)(**kwargs), deadline=deadline)

    async def _access_call(self, request: SlackAccessRequest, ids: Sequence[str]) -> dict[str, Any]:
        return await self._api(request.method, request.payload(ids))

    async def _share_access(
        self,
        request: SlackAccessRequest,
        *,
        logger: logging.Logger,
        details: Mapping[str, str],
        refusal: Callable[[Failure], str],
        extra: Callable[[Failure], Mapping[str, Any]] | None = None,
    ) -> str:
        try:
            await self._access_call(request, request.ids)
        except self._failure_type as exc:
            if exc.code in PARTIAL_ACCESS_FAILURE_CODES and len(request.ids) > 1:
                return await self._partial_access_result(
                    exc,
                    request,
                    logger=logger,
                    detail=details.get(exc.code, f"Slack refused {exc.where}"),
                    extra=extra(exc) if extra else None,
                )
            return refusal(exc)

        logger.info(
            "slack %s: %s access for %d %s on %s",
            request.resource_key.removesuffix("_id"),
            "revoked" if request.removes else request.level,
            len(request.ids),
            request.kind,
            request.resource_id,
        )
        return json.dumps(
            {
                "ok": True,
                request.resource_key: request.resource_id,
                "access_level": request.reported_level,
                "granted": list(request.ids),
            },
            ensure_ascii=False,
        )

    async def _per_target(
        self, ids: Sequence[str], call: Callable[[str], Awaitable[Any]]
    ) -> tuple[list[str], list[dict[str, str]], list[str]]:
        """Retry each target after a partial access failure."""
        async def attempt(target: str) -> str | None:
            try:
                await call(target)
            except self._failure_type as exc:
                return exc.code
            return None

        return await run_targets(ids, attempt, self._remaining)

    async def _partial_access_result(
        self,
        exc: Failure,
        request: SlackAccessRequest,
        *,
        logger: logging.Logger,
        detail: str,
        extra: Mapping[str, Any] | None = None,
    ) -> str:
        granted, failed, undetermined = await self._per_target(
            request.ids, lambda target: self._access_call(request, [target])
        )
        result: dict[str, Any] = {
            "ok": False,
            "error": "access_partially_applied" if granted else exc.code,
            "detail": (
                f"{detail}. Slack did not identify which targets it changed."
                " The tool retried each target. Results follow"
            ),
            request.resource_key: request.resource_id,
            "access_level": request.reported_level,
            "granted": granted,
            "failed": failed,
        }
        if undetermined:
            result["undetermined"] = undetermined
            result["detail"] += (
                ". The tool ran out of time before retrying the undetermined targets. Their status remains unknown"
            )
        if extra:
            result.update(extra)
        logger.warning(
            "slack %s: access partially applied on %s -- %d granted, %d failed, %d undetermined",
            request.resource_key.removesuffix("_id"),
            request.resource_id,
            len(granted),
            len(failed),
            len(undetermined),
        )
        return json.dumps(result, ensure_ascii=False)


class SlackMethodToolkitBase(SlackJsonToolkitBase[_SlackCallFailure]):
    """Use simple Slack errors and a fresh timeout for each SDK method call."""

    _failure_type = _SlackCallFailure
    _deadline_per_call = True

    def _failure(self, data: Mapping[str, Any], method: str) -> _SlackCallFailure:
        return _SlackCallFailure(_safe_error_code(data.get("error"), self._bot_token), method)
