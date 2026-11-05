"""Run, stop, recover and report the runs of detached sessions through the conversation host."""

import asyncio
import logging
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

from belle_agent.backend import (
    EVENT_FAILED,
    EVENT_INTERRUPTED,
    OP_CANCEL,
    OP_CONTINUE,
    OP_PENDING,
    OP_RECOVER,
    RECOVERY_NOT_SUBMITTED,
    RECOVERY_RUNNING,
    Recovery,
    TurnEvent,
)
from belle_agent.host.expiry import delete_owned_conversation
from belle_agent.platforms.base import TRIGGER_NONE, InboundMessage
from belle_agent.scheduling.address import Address, address_from_json
from belle_agent.scheduling.clock import iso_utc
from belle_agent.scheduling.host_turn import SessionRunTurn
from belle_agent.scheduling.label import run_label
from belle_agent.scheduling.records import DetachedSession, Job, SessionRun, request_id_of, utc_time
from belle_agent.scheduling.refusals import Refused
from belle_agent.scheduling.runner import RECOVERED_OUTCOMES, RunEnd
from belle_agent.scheduling.session_toolkit import check_session_posting
from belle_agent.scopes import SECTION_DELIVERY
from belle_agent.silence import NO_REPLY_INSTRUCTION, is_no_reply_sentinel
from belle_agent.skills import command_help_instruction, required_skills_instruction

if TYPE_CHECKING:
    from belle_agent.host import ConversationHost

logger = logging.getLogger(__name__)
#: The outcome of a recovered run while Belle posts its final. It stays when Belle stops before the receipt.
OUTCOME_POST_UNCERTAIN = "post-uncertain"


class SessionRuns:
    """Host operations for the runs of every session kind."""

    @property
    def _session_host(self) -> "ConversationHost":
        return cast("ConversationHost", self)

    def session_posting_tool(self, platform: str) -> str:
        """Return the registered posting tool or refuse unsupported detached delivery."""
        host = self._session_host
        name = host._hosted[platform].platform.posting_tool
        if not name or host._tools._tool(platform, name) is None:
            raise Refused("capability_unavailable", "detached sessions need a registered platform posting tool")
        return name

    async def start_run(self, job: Job | None, session: DetachedSession, run: SessionRun) -> None:
        """Start a run once no other run of its session runs: a session's conversation takes one turn at a time."""
        host = self._session_host
        if not host._running or host.scheduler is None:
            raise RuntimeError("session host is not running")
        conversation = session.owned_conversation
        lock, users = host._session_locks.get(conversation, (asyncio.Lock(), 0))
        host._session_locks[conversation] = (lock, users + 1)
        task = asyncio.create_task(self._start_when_free(job, session, run, lock))
        host._run_waiters[run.run_id] = task
        host._tasks.add(task)
        task.add_done_callback(host._tasks.discard)
        task.add_done_callback(lambda done: host._run_waiters.pop(run.run_id, None))
        task.add_done_callback(lambda done: self._release_lock(conversation))

    def _release_lock(self, conversation: str) -> None:
        """Drop a session's lock when no run holds it or waits for it."""
        locks = self._session_host._session_locks
        lock, users = locks[conversation]
        if users == 1:
            del locks[conversation]
        else:
            locks[conversation] = (lock, users - 1)

    async def _start_when_free(self, job: Job | None, session: DetachedSession, run: SessionRun, lock: asyncio.Lock) -> None:
        host = self._session_host
        started = False
        try:
            async with lock:
                if not await self._run_still_active(run.run_id):
                    await self._end_waiting_run(session, run, "interrupted", "")
                    return
                turn = await self._session_run_turn(job, session, run)
                started = True
                await host._start(turn)
        except asyncio.CancelledError:
            if not started and host._running:
                await self._end_waiting_run(session, run, "interrupted", "")
            raise
        except Exception as exc:
            if not started:
                code = f"{exc.code}: " if hasattr(exc, "code") else ""
                await self._end_waiting_run(session, run, "failed", f"{code}{exc}")
            else:
                raise

    async def _run_still_active(self, run_id: str) -> bool:
        host = self._session_host
        assert host.scheduler is not None
        async with host.scheduler.store.transaction() as snapshot:
            found = snapshot.find_run(run_id)
        return found is not None and found[1]["state"] == "running" and not found[1]["stop_requested"]

    async def _end_waiting_run(self, session: DetachedSession, run: SessionRun, outcome: str, error: str) -> None:
        host = self._session_host
        assert host.scheduler is not None
        end = RunEnd(outcome, "", [], error, run.turn, iso_utc(host.scheduler.clock.now()))
        await host.scheduler.ended_run(session.session_id, run.run_id, end)
        await self.report_run(session.session_id, run.run_id)

    async def delete_owned_conversation(self, session: DetachedSession) -> None:
        """Delete the harness conversation of an expired session."""
        host = self._session_host
        await delete_owned_conversation(host._backend, host.backend_capabilities, session)

    async def can_continue_session(self, session: DetachedSession) -> bool:
        host = self._session_host
        if not host._supports(OP_CONTINUE):
            return False
        try:
            return await host._backend.retained(session.owned_conversation)
        except Exception as exc:
            logger.warning("backend conversation check failed", exc_info=True)
            raise Refused("capability_unavailable", "backend conversation check failed") from exc

    async def _session_run_turn(self, job: Job | None, session: DetachedSession, run: SessionRun) -> Any:
        """Build the host turn of one run, in the conversation that its session owns."""
        if run.delivery == "post_final":
            if job is None:
                raise Refused("job_not_found", f"session {session.session_id} has no job to post its run's final for")
            inbound, scope, delivery, reply_optional, target = await self._posting_run(job, session, run)
        else:
            inbound, scope, delivery, reply_optional, target = await self._tool_run(session, run)
        host = self._session_host
        turn = host._turn(inbound, session.owned_conversation, delivery, reply_optional=reply_optional)
        metadata = {**turn.request.metadata, "belle_schedule": {
            "job_id": session.job_id, "creator": session.requested_by_user_id, "origin": session.origin,
            "origin_chat_type": session.origin_chat_type,
        }}
        # A continuation was checked against the retained conversation, so the backend must not make an empty one.
        continues = run.request is not None and run.request.continues
        turn.request = replace(turn.request, scope=scope, metadata=metadata, existing_conversation_only=continues)
        turn.bound.metadata = metadata
        turn.bound.tools = host._bound_tools(scope.tools)
        turn.bound.content_types = host._backend.capabilities().content_for(scope.model)
        turn.session_run = SessionRunTurn(session.session_id, run.run_id, target, run.questions, run.turn, run.delivery)
        return turn

    async def _posting_run(self, job: Job, session: DetachedSession, run: SessionRun) -> tuple[Any, ...]:
        """A run whose final Belle posts to its place, with the job's audience and scope checked again."""
        host = self._session_host
        toolkit, scheduler = host._schedule_components()
        target, scope, chat_type = await toolkit.validate_run(
            job, deliver_to=Address(**run.place), turn=run.turn,
            approved_readers=run.request.approved_readers if run.request is not None else None,
        )
        label = run_label(job, session.session_id, run, target,
                          question_timeout_seconds=scheduler.settings.question_timeout_seconds)
        inbound = InboundMessage(
            target.platform, target.workspace, target.conversation, "",
            job.created_by_user_id if (target.platform, target.workspace) == (
                job.origin["platform"], job.origin["workspace"]
            ) else "",
            run.prompt, event_id=request_id_of(run),
            thread="" if target.thread == "new" else target.thread,
            reply_thread=target.thread, chat_type=chat_type,
        )
        delivery = host._settle(inbound, SECTION_DELIVERY)
        additions = [label]
        if delivery.get("prompt"):
            additions.append(str(delivery["prompt"]))
        additions.append(NO_REPLY_INSTRUCTION)
        if host._second_brain is not None and host._second_brain.available:
            additions.append(host._second_brain.instructions)
        if "belle-command-help" in scope.skills_required:
            prefix = host._hosted[target.platform].platform.command_prefix(target.workspace)
            if instruction := command_help_instruction(prefix):
                additions.append(instruction)
        if host._supplies_skills and (instruction := required_skills_instruction(scope.skills_required)):
            additions.append(instruction)
        return inbound, replace(scope, system_additions=tuple(additions)), delivery, False, target

    async def _tool_run(self, session: DetachedSession, run: SessionRun) -> tuple[Any, ...]:
        """A run that delivers through the platform posting tool, under the requester's current permissions."""
        host = self._session_host
        toolkit = host.schedule_toolkit
        assert toolkit is not None
        origin = address_from_json(session.origin)
        user = session.requested_by_user_id
        target, target_chat_type = await toolkit._target(origin.json(), origin, user)
        posting_tool = self.session_posting_tool(origin.platform)
        context = run.request.inbound if run.request is not None else {}
        optional = context.get("reply_optional", False)
        advertised_posting_tool = host.backend_capabilities.tool_name(posting_tool)
        guidance = (
            f"This is a detached session. Deliver any substantive answer only through {advertised_posting_tool}, "
            "using the original message context and current audience permissions. "
            "Your final response is stored privately by Belle and is never automatically posted. "
            "Belle sends a separate short completion notice only when notify is true. "
        )
        guidance += ("A reply is optional; choose silence when there is nothing useful to add."
                     if optional else (
                         f"Before your final response, call {advertised_posting_tool} to deliver the answer "
                         "and check its result. If posting fails, report the failure in your stored final response."
                     ))
        _, scope = await toolkit._turn(
            run.turn, origin, target, user, session.origin_chat_type,
            target_chat_type=target_chat_type, label=guidance, refuse_questions=run.questions == "refuse",
        )
        check_session_posting(posting_tool, scope, optional=optional)
        inbound = InboundMessage(
            origin.platform, origin.workspace, origin.conversation, context.get("message_id", ""), user,
            run.prompt, event_id=request_id_of(run), thread=origin.thread,
            reply_thread=context.get("reply_thread", origin.thread), chat_type=session.origin_chat_type,
            envelope=context.get("envelope", ""), trigger=context.get("trigger", TRIGGER_NONE),
        )
        return inbound, scope, host._settle(inbound, SECTION_DELIVERY), optional, target

    async def stop_run(self, session: DetachedSession, run: SessionRun) -> bool:
        """Stop a running run, or one that waits for its session's conversation."""
        host = self._session_host
        for turn in tuple(host._admission.turn_initiators.values()):
            if turn.session_run is not None and turn.session_run.run_id == run.run_id:
                return await host._cancel(turn)
        if waiting := host._run_waiters.get(run.run_id):
            waiting.cancel()
            await asyncio.gather(waiting, return_exceptions=True)
            # A task cancelled before its first instruction cannot report its own end.
            if host._running:
                await self._end_waiting_run(session, run, "interrupted", "")
            return True
        return False

    async def stop_turn(self, turn: Any, *, active: Callable[[], bool] = lambda: True) -> bool:
        """Stop a turn that a person stops. Return whether the backend stopped it.

        The run of a session turn is marked first, so that its end counts as cancelled. ``active`` is checked after
        the mark: a turn that ended meanwhile gets no stop. A stop that is not sent, or that raises, clears the mark.
        """
        host = self._session_host
        scheduler = host.scheduler
        run_id = turn.session_run.run_id if turn.session_run is not None and scheduler is not None else ""
        if scheduler is not None and run_id:
            async with scheduler.store.transaction() as snapshot:
                found = snapshot.find_run(run_id)
                if found is not None and found[1]["state"] == "running":
                    found[1]["stop_requested"] = True
        stopped = False
        try:
            stopped = active() and await host._cancel(turn)
        finally:
            if scheduler is not None and run_id and not stopped:
                await scheduler.unmark_stop([run_id])
        return stopped

    async def recover_run(self, session: DetachedSession, run: SessionRun) -> Recovery | None:
        """Inspect a started backend request without sending its prompt again."""
        host = self._session_host
        conversation = session.owned_conversation
        job = await self._job_of(session) if run.delivery == "post_final" else None
        if not host._supports(OP_RECOVER) or (run.delivery == "post_final" and job is None):
            if host._supports(OP_CANCEL):
                await host._backend.cancel(conversation)
            return None
        try:
            turn = await self._session_run_turn(job, session, run)
        except Refused as exc:
            if not host._supports(OP_CANCEL) or not await host._backend.cancel(conversation):
                raise RuntimeError("cannot stop a recovered turn whose current scope is refused") from exc
            return Recovery("terminal", TurnEvent(EVENT_FAILED, text=f"scope refused during recovery: {exc}"))
        host._tools.bind(conversation, turn.bound)
        try:
            recovered = await host._backend.recover(turn.request)
        except BaseException:
            host._tools.release(conversation, turn.request_id)
            raise
        if recovered.state == RECOVERY_RUNNING:
            turn.started_utc = run.started_iso_utc or None
            host._remember_recovered(turn)
            task = asyncio.create_task(self._watch_recovered(turn), name=f"belle-run-{run.run_id}")
            host._tasks.add(task)
            task.add_done_callback(host._tasks.discard)
        else:
            host._tools.release(conversation, turn.request_id)
        return recovered

    async def _job_of(self, session: DetachedSession) -> Job | None:
        """The job of a scheduled session, while it exists."""
        host = self._session_host
        assert host.scheduler is not None
        async with host.scheduler.store.transaction() as snapshot:
            stored = snapshot.jobs.get(session.job_id)
        return Job.from_json(stored, f"jobs.{session.job_id}") if stored is not None else None

    async def _await_recovery(self, turn: Any, now: Callable[[], datetime]) -> TurnEvent | None:
        """Watch a turn that the backend runs from before this host started, until it ends.

        Ask its questions again while it waits. A turn that still runs ``TURN_TIMEOUT_SECONDS`` after its start, on
        the clock ``now`` that recorded the start, is stopped and fails. Return its terminal event, or None when the
        host stops first.
        """
        from belle_agent.host import TURN_TIMEOUT_SECONDS

        host = self._session_host
        deadline = utc_time(turn.started_utc) + timedelta(seconds=TURN_TIMEOUT_SECONDS)
        while host._running:
            try:
                if host._supports(OP_PENDING):
                    for question in await host._backend.pending(turn.conversation):
                        await host._ask(turn, question)
                recovered = await host._backend.recover(turn.request)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("request %s recovery probe failed; retrying", turn.request_id)
                await asyncio.sleep(2)
                continue
            if recovered.state == RECOVERY_NOT_SUBMITTED:
                return TurnEvent(EVENT_INTERRUPTED, text="backend lost the accepted turn during recovery")
            if recovered.state != RECOVERY_RUNNING:
                return recovered.terminal
            if now() >= deadline:
                return await self._stop_recovered(turn)
            await asyncio.sleep(2)
        return None

    async def _stop_recovered(self, turn: Any) -> TurnEvent:
        """Stop a recovered turn that reached the turn timeout. Return the failure that ends it."""
        from belle_agent.host import TURN_TIMEOUT_NOTICE, TURN_TIMEOUT_SECONDS

        host = self._session_host
        turn.bound.stopped = True
        try:
            stopped = host._supports(OP_CANCEL) and await host._backend.cancel(turn.conversation)
        except Exception:  # The turn still fails: Belle no longer waits for it.
            logger.exception("request %s: the stop at the turn timeout failed", turn.request_id)
            stopped = False
        logger.warning("request %s reached no end in %ss after its start and %s", turn.request_id,
                       TURN_TIMEOUT_SECONDS, "was stopped" if stopped else "could not be stopped")
        return TurnEvent(EVENT_FAILED, text=TURN_TIMEOUT_NOTICE)

    async def _watch_recovered(self, turn: Any) -> None:
        host = self._session_host
        marker = turn.session_run
        assert host.scheduler is not None
        try:
            terminal = await self._await_recovery(turn, host.scheduler.clock.now)
        finally:
            turn.bound.stopped = True
            turn.session_run_ended = True
            for question_id, waiting in tuple(host._questions.items()):
                if waiting is turn:
                    del host._questions[question_id]
                    host._cancel_question_timer(question_id)
            host._forget_active_work(turn.status_id)
            host._admission.forget_turn(turn.conversation, turn.request_id)
            host._tools.release(turn.conversation, turn.request_id)
        if terminal is not None and host._running and host.scheduler is not None:
            await host.scheduler.ended_run(marker.session_id, marker.run_id, RunEnd(
                RECOVERED_OUTCOMES[terminal.kind], terminal.text, [], terminal.text if terminal.kind == EVENT_FAILED else "",
                marker.turn, iso_utc(host.scheduler.clock.now()),
            ))
            await self.report_run(marker.session_id, marker.run_id)

    async def report_run(self, session_id: str, run_id: str) -> None:
        """Deliver what a run owes once it ended outside a host turn's own delivery.

        A ``notice`` run gets its short notice. A ``post_final`` run that recovery ended gets its final posted.
        Each delivery happens once. The run becomes uncertain before the post. A post that fails or a stop before
        the receipt leaves it uncertain, and Belle posts no copy.
        """
        from belle_agent.host import OUTCOME_STORED_FINAL

        host = self._session_host
        if host.scheduler is None or host.schedule_toolkit is None:
            return
        async with host.scheduler.store.transaction() as snapshot:
            found = snapshot.find_run(run_id)
            if found is None:
                return
            session, run = found
            # A post_final run stores its final only when recovery ended it: a host turn posts or withholds it.
            if run["delivery"] == "post_final" and run["state"] == "succeeded" and run["outcome"] == OUTCOME_STORED_FINAL:
                final, place = run["result"].strip(), address_from_json(run["place"])
            elif run["report"]["state"] == "pending":
                final, place = "", address_from_json(session["origin"])
            else:
                return
            if place.platform not in host._hosted:
                error = f"Belle did not post: platform {place.platform} is not configured in this Belle"
                logger.error("run %s of session %s: %s", run_id, session_id, error)
                if run["delivery"] == "post_final":
                    run["error"] = error
                else:
                    run["report"].update(state="refused", error=error)
                return
            if run["delivery"] == "post_final":
                run["outcome"] = OUTCOME_POST_UNCERTAIN
            else:
                run["report"]["state"] = "uncertain"
            user = session["requested_by_user_id"]
            state = run["state"]
            reply_thread = ((run["request"] or {}).get("inbound") or {}).get("reply_thread")
        if run["delivery"] == "post_final":
            await self._post_final(run_id, final, place)
            return
        try:
            await host.schedule_toolkit._target(place.json(), place, user)
        except Refused as exc:
            await self._set_report(run_id, state="refused", error=str(exc))
            return
        message = f"Session `{session_id}` {state}."
        platform = host._hosted[place.platform].platform
        message += (f"\nIn a DM, run `{platform.command_prefix(place.workspace)}` with "
                    f"`sessions result {session_id}` as its command text to inspect the stored result.")
        try:
            posted = await platform.post(
                place.conversation, place.thread if reply_thread is None else reply_thread,
                message, workspace=place.workspace
            )
        except Exception:
            logger.exception("session %s report delivery is uncertain", session_id)
            return
        await self._set_report(run_id, state="sent", message_id=posted, error="")

    async def _post_final(self, run_id: str, final: str, place: Address) -> None:
        """Post the final of a recovered run to its place, and record the post."""
        from belle_agent.host import OUTCOME_POSTED_FINAL, OUTCOME_WITHHELD

        host = self._session_host
        assert host.scheduler is not None
        if not final or is_no_reply_sentinel(final):
            outcome, posted = OUTCOME_WITHHELD, []
        else:
            platform = host._hosted[place.platform].platform
            thread = "" if place.thread == "new" else place.thread
            try:
                message_id = await platform.post(place.conversation, thread, final, workspace=place.workspace)
            except Exception:
                logger.exception("the post of recovered run %s is uncertain", run_id)
                return
            posted = [{**place.json(), "thread": thread or (message_id if place.thread == "new" else ""),
                       "message_id": message_id}]
            outcome = OUTCOME_POSTED_FINAL
        async with host.scheduler.store.transaction() as snapshot:
            found = snapshot.find_run(run_id)
            if found is not None:
                found[1].update(outcome=outcome, posted=posted)

    async def _set_report(self, run_id: str, **report: str) -> None:
        host = self._session_host
        assert host.scheduler is not None
        async with host.scheduler.store.transaction() as snapshot:
            found = snapshot.find_run(run_id)
            if found is not None:
                found[1]["report"].update(report)
