#!/usr/bin/env python3
"""Answer numbered questions about the pinned OpenCode build, one check for each.

Run in the test container, beside a running OpenCode container on an internal
network, with OCB_OPENCODE_URL and OCB_PEER set. The stand-in model, the relay
endpoint and the MCP server are threads of this process, so OpenCode reaches
all three across the network.

    main         every question that needs one server lifetime
    restarted    after the OpenCode container was stopped and started
    configured   a fresh server whose configuration mounts the MCP server and
                 defines an agent
    hold         the long waits: a relay hook and an MCP call held for minutes
    overflow     a stalled subscriber behind many small events, then behind 256 MB

Each check prints one JSON line: the question number and what was observed.
A roster or prompt claim is read from the stand-in's request log. A refused
tool call is read from the MCP server's log, which must hold no call.
"""

import json
import os
import socket
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [HERE, os.path.join(HERE, "..", "conformance")]
from drive import TERMINAL, Client, Events, text, turn
from listeners import McpServer, Relay
from standin import StandIn, call, said, text_of, tools

STATE = os.environ.get("OCB_STATE", "/state")
ARTEFACTS = os.path.join(STATE, "artefacts")
PEER = os.environ.get("OCB_PEER", "ocb-belle")
MCP_URL = f"http://{PEER}:18300/mcp"
SKILL = "group-chat-participation"
EXPERT_MARK = "EXPERT_SYSTEM_MARK_Q13"

stage = sys.argv[1] if len(sys.argv) > 1 else "main"
client = Client() if stage != "config" else None
results: list[dict] = []


def record(question: int, check: str, **observed) -> None:
    entry = {"question": question, "stage": stage, "check": check, **observed}
    results.append(entry)
    print(json.dumps(entry), flush=True)


class Policy:
    """The relay's answers: per hook and session, a dict, an HTTP status, or a function of the event."""

    def __init__(self):
        self.rules: dict[tuple[str, str], object] = {}

    def set(self, hook: str, session: str, answer) -> None:
        self.rules[(hook, session)] = answer

    def clear(self, session: str) -> None:
        self.rules = {key: answer for key, answer in self.rules.items() if key[1] != session}

    def __call__(self, hook: str, event: dict):
        answer = self.rules.get((hook, event.get("sessionID")), {})
        return answer(event) if callable(answer) else answer


def until(condition, timeout: float = 30, step: float = 0.05):
    deadline = time.monotonic() + timeout
    while True:
        value = condition()
        if value or time.monotonic() > deadline:
            return value
        time.sleep(step)


def mount(name: str = "belle", **extra):
    config = {"type": "remote", "url": MCP_URL, "oauth": False, "codemode": False, **extra}
    return client.call("PUT", f"/api/experimental/mcp/{name}", {"config": config})


def roster(session: str, index: int = -1) -> list[str]:
    return tools(model.requests(session)[index])


def tool_results(session: str) -> list[str]:
    return said(model.requests(session)[-1], "tool")


def pending(session: str) -> list[dict]:
    return client.api("GET", f"/api/session/{session}/permission")


# ---------------------------------------------------------------- main stage


def q15_version():
    status, info = client.call("GET", "/api/info")
    record(15, "GET /api/info", status=status, version=info.get("version"), keys=sorted(info))
    # Found on the way, not one of the numbered checks: the readiness call returns the provider key.
    settings = client.api("GET", "/api/model/default").get("settings") or {}
    record(
        15,
        "GET /api/model/default, the readiness call",
        settings_keys=sorted(settings),
        api_key_returned_verbatim=settings.get("apiKey") == "standin",  # the placeholder of a stand-in run
    )


def q1_relay_across_containers():
    plugins = [p["id"] for p in client.api("GET", "/api/plugin") if p["source"]["type"] != "builtin"]
    session = client.session()
    policy.set("prompt", session, {"text": "hello SAY=CROSSED_THE_NETWORK"})
    end = turn(client, events, session, "hello SAY=local")
    record(
        1,
        "prompt hook in the OpenCode container, answered by a listener in the Belle container",
        non_builtin_plugins=plugins,
        terminal=end["type"],
        streamed_text=text(events.of(session)),
        last_user_message_in_provider_request=said(model.requests(session)[0], "user")[-1],
        prompt_round_trips=relay.count("prompt", session),
        context_round_trips=relay.count("context", session),
        relay_token_rejections=len(relay.unauthorized),
        provider_requests_with_the_session_id_header=sum(e["session"] == session for e in model.log),
        stored_request_has_authorization_header=any("authorization" in e["headers"] for e in model.log),
    )


def q7_mount_headers_and_signal():
    mark = events.mark()
    sent = time.monotonic()
    status, body = mount(headers={"Authorization": "Bearer MCP_RUN_TOKEN", "X-Belle-Probe": "1"})
    returned = time.monotonic()
    session = client.session()
    turn(client, events, session, "immediately after the PUT")
    immediate = "belle_echo" in roster(session)
    connected = until(lambda: [s for s in client.api("GET", "/api/mcp") if s["name"] == "belle"])
    time.sleep(3)
    signal = [
        (round(at - sent, 3), e["type"], (e.get("data") or {}).get("status") or "")
        for at, e in events.seen[mark:]
        if e["type"].startswith(("mcp.", "command."))
    ]
    first_list = next((round(e["at"] - sent, 3) for e in mcp.log if e["method"] == "tools/list"), None)
    record(
        7,
        "wire mount: headers and refresh signal",
        put_status=status,
        put_body=body,
        put_took_s=round(returned - sent, 3),
        tool_in_roster_of_a_turn_sent_right_after_the_put=immediate,
        api_mcp_entry=connected,
        events_after_put=signal,
        tools_list_received_at_s=first_list,
        headers_seen_by_the_mcp_server={
            k: v
            for k, v in mcp.log[-1]["headers"].items()
            if k in ("authorization", "x-belle-probe", "user-agent", "mcp-protocol-version")
        },
        every_mcp_request_carried_the_header=all(
            e["headers"].get("authorization") == "Bearer MCP_RUN_TOKEN" for e in mcp.log
        ),
    )
    # Is an event the signal? Mount again under a new name, wait for the event, then send a turn at once.
    observed = {}
    for kind in ("mcp.status.changed", "command.updated"):
        name = "sig" + kind.split(".")[0]
        mark = events.mark()
        mount(name)
        if kind == "mcp.status.changed":  # the second one: the first says `pending`
            until(lambda kind=kind, mark=mark: sum(e["type"] == kind for e in events.of(start=mark)) >= 2, 10)
        else:
            events.wait_for_type(kind, start=mark, timeout=10)
        session = client.session()
        turn(client, events, session, "after the event")
        observed[f"tool_in_roster_right_after_{kind}"] = f"{name}_echo" in roster(session)
    # How long until a new turn sees a new mount, with nothing but the PUT to go on.
    mount("sigpoll")
    sent = time.monotonic()
    for attempt in range(1, 60):
        session = client.session()
        turn(client, events, session, "poll")
        if "sigpoll_echo" in roster(session):
            break
        time.sleep(0.1)
    observed["turns_until_the_tool_appeared"] = attempt
    observed["seconds_until_the_tool_appeared"] = round(time.monotonic() - sent, 2)
    observed["mcp_status_events"] = [
        (e["type"], e.get("data")) for e in events.of(start=mark) if e["type"].startswith("mcp.")
    ][:6]
    record(7, "is there a signal that the tool catalog has refreshed", **observed)


def q2_q3_q16_mcp_tools_and_hooks():
    session = client.session()
    turn(client, events, session, "alpha")
    context = relay.events("context", session)[-1]
    full = roster(session)
    record(
        2,
        "MCP tools in the context hook",
        context_hook_tools=context["tools"],
        mcp_names_in_hook=[n for n in context["tools"] if "belle" in n],
        provider_roster=full,
        context_event_keys=sorted(context),
    )
    # Remove the MCP tool for one turn, and have the model call it anyway.
    policy.set("context", session, {"tools": [n for n in context["tools"] if n != "belle_echo"]})
    before = len(mcp.calls("echo"))
    turn(client, events, session, call("belle_echo", token="Q2_REMOVED"))
    removed_roster, removed_result = roster(session, -2), tool_results(session)
    removed_calls = len(mcp.calls("echo")) - before
    hooks_removed = (relay.count("execute.before", session), relay.count("evaluate", session))
    policy.clear(session)
    # Control, and the hold that orders `execute.before` against `session.tool.called`.
    policy.set("execute.before", session, lambda event: time.sleep(2) or {})
    mark = events.mark()
    turn(client, events, session, call("belle_echo", token="Q2_CONTROL"))
    record(
        2,
        "a tool removed in the context hook",
        removed_turn_roster_has_tool="belle_echo" in removed_roster,
        removed_turn_roster_size=len(removed_roster),
        full_roster_size=len(full),
        mcp_calls_on_removed_turn=removed_calls,
        tool_result_on_removed_turn=removed_result,
        hook_round_trips_on_removed_turn=dict(zip(("execute.before", "evaluate"), hooks_removed)),
        mcp_calls_on_control_turn=len(mcp.calls("echo")) - before,
        tool_result_on_control_turn=tool_results(session),
    )
    before_event = relay.events("execute.before", session)[-1]
    evaluate_event = relay.events("evaluate", session)[-1]
    record(
        3,
        "execute.before and evaluate on an MCP tool call",
        execute_before_round_trips=relay.count("execute.before", session) - hooks_removed[0],
        evaluate_round_trips=relay.count("evaluate", session) - hooks_removed[1],
        execute_before_event=before_event,
        evaluate_event=evaluate_event,
    )
    asked = next(at for at, hook, event in relay.hits if hook == "execute.before" and event is before_event)
    answered = next(at for at, hook in reversed(relay.answered) if hook == "execute.before")
    called = next(at for at, e in events.seen[mark:] if e["type"] == "session.tool.called")
    order = [("relay " + hook, at) for at, hook, e in relay.hits if e.get("sessionID") == session and at >= asked - 1]
    order += [(e["type"], at) for at, e in events.seen[mark:] if e["type"].startswith("session.tool.")]
    record(
        16,
        "order of execute.before, evaluate and session.tool.called, with the execute.before answer held 2 s",
        tool_called_event_after_execute_before_asked_s=round(called - asked, 3),
        tool_called_event_after_execute_before_answered_s=round(called - answered, 3),
        order=[name for name, _ in sorted(order, key=lambda item: item[1])],
        tool_called_event_data=next(e["data"] for at, e in events.seen[mark:] if e["type"] == "session.tool.called"),
    )
    return evaluate_event["action"]


def q4_permission_rules(action: str):
    observed = {}
    for effect in ("deny", "ask"):
        session = client.session(permissions=[{"action": action, "resource": "*", "effect": effect}])
        before, mark = len(mcp.calls("echo")), events.mark()
        client.prompt(session, call("belle_echo", token=f"Q4_{effect}"))
        if effect == "ask":
            asked = until(lambda session=session: pending(session), 20)
            observed["ask_pending_request"] = asked
            observed["ask_mcp_calls_while_pending"] = len(mcp.calls("echo")) - before
            reply = client.call(
                "POST", f"/api/session/{session}/permission/{asked[0]['id']}/reply", {"decision": "once"}
            )
            observed["ask_reply_status"] = reply[0]
        events.wait(session, start=mark)
        observed[f"{effect}_roster_has_tool"] = "belle_echo" in roster(session, 0)
        observed[f"{effect}_mcp_calls"] = len(mcp.calls("echo")) - before
        observed[f"{effect}_tool_result"] = tool_results(session)
        observed[f"{effect}_evaluate_effects"] = [e["effect"] for e in relay.events("evaluate", session)]
    # The same rule, rejected by the person.
    session = client.session(permissions=[{"action": action, "resource": "*", "effect": "ask"}])
    before, mark = len(mcp.calls("echo")), events.mark()
    client.prompt(session, call("belle_echo", token="Q4_reject"))
    asked = until(lambda: pending(session), 20)
    client.call(
        "POST", f"/api/session/{session}/permission/{asked[0]['id']}/reply", {"decision": "reject", "message": "no"}
    )
    end = events.wait(session, start=mark)
    record(
        4,
        "session permission rules on an MCP tool",
        action=action,
        **observed,
        reject_mcp_calls=len(mcp.calls("echo")) - before,
        reject_terminal=end["type"],
        reject_tool_result=tool_results(session),
    )


def q11_wildcard_and_builtins():
    plain = client.session()
    turn(client, events, plain, "alpha")
    observed = {"roster_with_no_rule": roster(plain)}
    for label, rules in (
        ("action_star", [{"action": "*", "resource": "*", "effect": "deny"}]),
        (
            "action_star_then_allow_read",
            [
                {"action": "*", "resource": "*", "effect": "deny"},
                {"action": "read", "resource": "*", "effect": "allow"},
            ],
        ),
        (
            "allow_read_then_action_star",
            [
                {"action": "read", "resource": "*", "effect": "allow"},
                {"action": "*", "resource": "*", "effect": "deny"},
            ],
        ),
        ("prefix_belle_star", [{"action": "belle_*", "resource": "*", "effect": "deny"}]),
    ):
        status, created = client.call("POST", "/api/session", {"permissions": rules})
        if status != 200:
            observed[label] = {"create_status": status, "body": created}
            continue
        session = created["data"]["id"]
        turn(client, events, session, "alpha")
        observed[label] = roster(session)
    session = client.session(permissions=[{"action": "*", "resource": "*", "effect": "deny"}])
    before = len(mcp.calls("echo"))
    turn(client, events, session, call("belle_echo", token="Q11"))
    record(
        11,
        "wildcard action in a session permission rule, and the built-in tool names",
        **observed,
        call_under_action_star_deny={
            "mcp_calls": len(mcp.calls("echo")) - before,
            "tool_result": tool_results(session),
        },
    )


def q5_mcp_timeout_short():
    status, body = mount("belleshort", timeout={"execution": 2000})
    until(lambda: [s for s in client.api("GET", "/api/mcp") if s["name"] == "belleshort"])
    time.sleep(3)
    session = client.session()
    before, started = len(mcp.calls("slow")), time.monotonic()
    end = turn(client, events, session, call("belleshort_slow", seconds=6))
    took = time.monotonic() - started
    time.sleep(6)
    cancels = [e for e in mcp.log if e["method"] == "notifications/cancelled"]
    record(
        5,
        "an MCP call that outlives timeout.execution=2000 on its mount",
        mount_status=status,
        mount_body=body,
        terminal=end["type"],
        turn_took_s=round(took, 2),
        tools_call_requests=len(mcp.calls("slow")) - before,
        tool_result=tool_results(session),
        cancel_notifications=[e["params"] for e in cancels],
        late_answers_dropped=list(mcp.dropped),
    )
    # A mount whose listener is away: does OpenCode connect once the listener is up?
    away = f"http://{PEER}:18399/mcp"
    client.call("PUT", "/api/experimental/mcp/away", {"config": {"type": "remote", "url": away, "oauth": False}})

    def status():
        return next(s["status"] for s in client.api("GET", "/api/mcp") if s["name"] == "away")

    time.sleep(1)
    while_away = status()
    late = McpServer(port=18399)
    late.add("echo", lambda a: "late")
    time.sleep(5)
    after_listener = status()
    connect = client.call("POST", "/api/experimental/mcp/away/connect", {})[0]
    time.sleep(1)
    record(
        7,
        "a mount whose MCP server is not listening yet",
        status_while_away=while_away,
        status_5_s_after_the_listener_came_up=after_listener,
        connect_call_status=connect,
        status_after_the_connect_call=status(),
    )
    # A tool the server gains after the mount: is the catalog read again?
    mcp.add("late", lambda a: "late")
    session = client.session()
    turn(client, events, session, "alpha")
    record(
        7,
        "a tool added to the MCP server after the mount, with no listChanged notification",
        in_roster="belle_late" in roster(session),
        tools_list_requests=sum(e["method"] == "tools/list" for e in mcp.log),
    )
    del mcp.tools["late"]
    session = client.session()
    before, started = len(mcp.calls("slow")), time.monotonic()
    end = turn(client, events, session, call("belle_slow", seconds=45), timeout=120)
    record(
        5,
        "a 45 s MCP call on a mount with no timeout setting",
        terminal=end["type"],
        turn_took_s=round(time.monotonic() - started, 2),
        tools_call_requests=len(mcp.calls("slow")) - before,
        tool_result=tool_results(session),
    )


def q6_meta_and_child_session():
    session = client.session()
    child_prompt = "child task " + call("belle_echo", token="Q6_CHILD")
    turn(
        client,
        events,
        session,
        call("subagent", agent="general", description="probe child", prompt=child_prompt),
        timeout=120,
    )
    children = [s["id"] for s in client.api("GET", f"/api/session?parentID={session}")]
    made = [e for e in mcp.calls("echo") if e["params"]["arguments"].get("token") == "Q6_CHILD"]
    direct = mcp.calls("echo")[0]
    record(
        6,
        "what an MCP tools/call carries",
        params_keys=sorted(direct["params"]),
        meta=direct["params"].get("_meta"),
        jsonrpc_id=direct["id"],
        headers=sorted(direct["headers"]),
        tool_call_id_present=any(
            "call_standin" in json.dumps(e["params"]) + json.dumps(e["headers"]) for e in mcp.calls()
        ),
    )
    record(
        6,
        "a call from a child session",
        parent=session,
        children=children,
        child_call_meta=[e["params"].get("_meta") for e in made],
        carries_child_id=[e["params"]["_meta"]["ai.opencode/sessionID"] in children for e in made],
        child_execute_before_session=[
            e["sessionID"] for e in relay.events("execute.before") if e["input"].get("token") == "Q6_CHILD"
        ],
        child_session=[
            {k: s.get(k) for k in ("id", "parentID", "agent")}
            for s in client.api("GET", f"/api/session?parentID={session}")
        ],
    )


def q8_ids_and_metadata():
    ids = {}
    for candidate in (
        "ses_belle_q8_0001",
        "ses_loopback:C1:1700000000.000100",
        "sesX",
        "belle-1",
        "ses_" + "a" * 200,
        "ses_ünïcode",
    ):
        status, body = client.call("POST", "/api/session", {"id": candidate})
        ids[candidate[:40]] = [status, body["data"]["id"][:40] if status == 200 else body]
    again = client.call("POST", "/api/session", {"id": "ses_belle_q8_0001"})
    session = "ses_belle_q8_0001"
    messages = {}
    for candidate in ("msg_belle_q8_0001", "msg_loopback:C1:evt-42", "evt-42", "msg_" + "b" * 200):
        status, body = client.call(
            "POST", f"/api/session/{session}/prompt", {"id": candidate, "text": "SAY=ok", "resume": False}
        )
        messages[candidate[:40]] = [status, body["data"]["id"][:40] if status == 200 else body]
    meta = {"conversation": "loopback:C1", "belle": {"scope": "s1"}}
    with_meta = client.session(metadata=meta)
    listed = [s for s in client.api("GET", "/api/session?limit=200") if s["id"] == with_meta]
    record(
        8,
        "ids the server accepts, and session metadata",
        session_ids=ids,
        second_create_with_the_same_id=[again[0], again[1]["data"]["id"] if again[0] == 200 else again[1]],
        message_ids=messages,
        metadata_sent=meta,
        metadata_on_get=client.api("GET", f"/api/session/{with_meta}").get("metadata"),
        metadata_in_list=listed[0].get("metadata") if listed else "session not listed",
        list_entry_keys=sorted(listed[0]) if listed else None,
    )


def q9_throws():
    observed = {}
    # context: the round trip fails before the first provider request.
    session = client.session()
    policy.set("context", session, 500)
    mark = events.mark()
    status, _ = client.prompt(session, "SAY=never")
    end = events.wait(session, start=mark, timeout=60)
    observed["context"] = {
        "prompt_status": status,
        "terminal": end["type"],
        "terminal_data": end["data"],
        "provider_requests": len(model.requests(session)),
        "events": [e["type"] for e in events.of(session, mark)],
    }
    policy.clear(session)
    observed["context"]["control_turn_after"] = turn(client, events, session, "SAY=after")["type"]
    observed["context"]["control_user_messages"] = said(model.requests(session)[-1], "user")
    # evaluate: the round trip fails while a tool call waits for its verdict.
    session = client.session()
    policy.set("evaluate", session, 500)
    before, mark = len(mcp.calls("echo")), events.mark()
    end = turn(client, events, session, call("belle_echo", token="Q9_EVALUATE"))
    observed["evaluate"] = {
        "terminal": end["type"],
        "terminal_data": end["data"],
        "mcp_calls": len(mcp.calls("echo")) - before,
        "provider_requests": len(model.requests(session)),
        "tool_result": tool_results(session),
        "tool_events": [
            (e["type"], e["data"].get("error"))
            for e in events.of(session, mark)
            if e["type"].startswith("session.tool.")
        ],
    }
    policy.clear(session)
    turn(client, events, session, call("belle_echo", token="Q9_EVALUATE_CONTROL"))
    observed["evaluate"]["control_mcp_calls"] = len(mcp.calls("echo")) - before
    # http.response: the round trip fails after the provider answered.
    session = client.session()
    policy.set("prompt", session, {"response": True})
    policy.set("http.response", session, 500)
    mark = events.mark()
    client.prompt(session, "SAY=never_shown")
    end = events.wait(session, start=mark, timeout=120)
    observed["http.response"] = {
        "terminal": end["type"],
        "terminal_data": end["data"],
        "provider_requests": len(model.requests(session)),
        "http_response_round_trips": relay.count("http.response", session),
        "streamed_text": text(events.of(session, mark)),
        "events": sorted({e["type"] for e in events.of(session, mark)}),
        "stored": [
            (m["type"], m.get("content") or m.get("text") or m.get("outcome")) for m in client.messages(session)
        ],
    }
    policy.clear(session)
    observed["http.response"]["control_turn_after"] = turn(client, events, session, "SAY=after")["type"]
    record(9, "a failed round trip, so a throw, in context, evaluate and http.response", **observed)


def q14_prompt_during_a_turn():
    observed = {}
    for delivery in ("default", "steer", "queue"):
        session = client.session()
        mark = events.mark()
        sent_first = time.monotonic()
        # Two steps: a 3 s tool call, then the final text.
        first_id = client.prompt(session, "first " + call("belle_slow", seconds=3))[1]["data"]["id"]
        time.sleep(1)
        extra = {} if delivery == "default" else {"delivery": delivery}
        sent_second = time.monotonic()
        status, body = client.prompt(session, "second SAY=SECOND", **extra)
        second_id = body["data"]["id"]
        until(lambda session=session, mark=mark: "SECOND" in text(events.of(session, mark)), 40)
        events.wait(session, start=mark)
        time.sleep(1)
        which = {first_id: "first", second_id: "second"}
        timeline = []
        for at, e in events.seen[mark:]:
            if (e.get("data") or {}).get("sessionID") != session:
                continue
            if e["type"] in TERMINAL or e["type"] in (
                "session.execution.started",
                "session.step.started",
                "session.tool.success",
            ):
                timeline.append((at, e["type"]))
            if e["type"].startswith("session.inbox."):
                item = next((name for key, name in which.items() if key in json.dumps(e["data"])), "?")
                timeline.append((at, f"{e['type']} ({item})"))
        for at, hook, e in relay.hits:
            if hook == "prompt" and e.get("sessionID") == session:
                timeline.append((at, f"relay prompt hook ({which.get(e['messageID'])}) delivery={e.get('delivery')}"))
        requests = model.requests(session)
        observed[delivery] = {
            "second_prompt_status": status,
            "delivery_in_response": body["data"].get("delivery"),
            "second_sent_at_s": round(sent_second - sent_first, 2),
            "timeline": [(round(at - sent_first, 2), name) for at, name in sorted(timeline, key=lambda item: item[0])],
            "executions_started": sum(e["type"] == "session.execution.started" for e in events.of(session, mark)),
            "terminal_events": [e["type"] for e in events.of(session, mark) if e["type"] in TERMINAL],
            "provider_requests": [
                [m["role"] + ":" + (text_of(m)[:12] if m["role"] != "tool" else "result") for m in r["messages"][1:]]
                for r in requests
            ],
            "streamed_text": text(events.of(session, mark)),
        }
    record(
        14,
        "a prompt sent 1 s into a turn of two steps (a 3 s tool call, then the final), by delivery value",
        **observed,
    )


def q12_packaged_skill():
    skills = {s["id"]: s for s in client.api("GET", "/api/skill")}
    if SKILL not in skills:
        return record(
            12,
            "a packaged Belle skill",
            listed=sorted(skills),
            loaded=False,
            note="the skill directory was not copied into the OpenCode container",
        )
    marker = skills[SKILL]["content"].strip().splitlines()[0]
    session = client.session()
    turn(client, events, session, "one")
    turn(client, events, session, "two", skills=[{"id": SKILL}])
    turn(client, events, session, "three")
    requests = model.requests(session)
    policy_session = client.session()
    policy.set("prompt", policy_session, {"skills": [{"id": SKILL}]})
    turn(client, events, policy_session, "through the relay")
    status, body = client.prompt(session, "four", skills=[{"id": "no-such-skill"}])
    record(
        12,
        "a packaged Belle skill in /workspace/.opencode/skills",
        listed=sorted(skills),
        skill_entry={k: skills[SKILL].get(k) for k in ("id", "name", "path", "autoinvoke", "description")},
        first_line_of_skill=marker,
        skill_text_per_request=[marker in json.dumps(r["messages"]) for r in requests],
        attached_through_the_relay_prompt_answer=marker in json.dumps(model.requests(policy_session)[0]["messages"]),
        skill_named_in_a_request_that_did_not_attach_it=SKILL in json.dumps(requests[0]),
        where_the_attached_text_is=[m["role"] for m in requests[1]["messages"] if marker in json.dumps(m)],
        unknown_skill_id=[status, body],
    )


def q18_outcomes():
    observed = {}
    spec = client.call("GET", "/openapi.json")[1]
    dumped = json.dumps(spec)
    observed["session_outcome_enum_in_openapi"] = '"enum": ["succeeded", "failed", "interrupted"]' in dumped
    session = client.session()
    observed["plain"] = turn(client, events, session, "SAY=fine")["type"]
    mark = events.mark()
    client.prompt(session, "SLOW TICKS=20")
    time.sleep(1.5)
    interrupt = client.call("POST", f"/api/session/{session}/interrupt", {})
    end = events.wait(session, start=mark)
    observed["interrupted"] = {"interrupt_answer": interrupt[1], "terminal": end["type"], "data": end["data"]}
    observed["interrupt_when_idle"] = client.call("POST", f"/api/session/{session}/interrupt", {})[1]
    for status in (400, 500):
        session = client.session()
        mark, started = events.mark(), time.monotonic()
        client.prompt(session, f"FAIL={status}")
        try:
            end = events.wait(session, start=mark, timeout=75)
            observed[f"provider_http_{status}"] = {
                "terminal": end["type"],
                "data": end["data"],
                "after_s": round(time.monotonic() - started, 1),
                "provider_requests": len(model.requests(session)),
                "stored_outcome": [m.get("outcome") for m in client.messages(session) if m["type"] == "idle"],
                "other_events": sorted({e["type"] for e in events.of(session, mark)} - TERMINAL),
            }
        except AssertionError:
            observed[f"provider_http_{status}"] = {
                "terminal": "none within 75 s",
                "provider_requests": len(model.requests(session)),
                "events": sorted({e["type"] for e in events.of(session, mark)}),
                "interrupt": client.call("POST", f"/api/session/{session}/interrupt", {})[1],
            }
            observed[f"provider_http_{status}"]["after_interrupt"] = events.wait(session, start=mark)["type"]
    seen = sorted({e["type"] for _, e in events.seen if e["type"].startswith("session.execution.")})
    record(18, "execution outcome events", **observed, execution_event_types_seen_in_this_run=seen)


def q19_files():
    session = client.session()
    observed = {}
    for label, files in (
        ("data_uri", [{"uri": "data:text/plain;base64,UTE5X0ZJTEVfQk9EWQ==", "name": "note.txt"}]),
        ("file_uri", [{"uri": "file:///etc/hostname", "name": "hostname"}]),
        ("path_not_uri", [{"path": "/etc/hostname"}]),
    ):
        status, body = client.call(
            "POST", f"/api/session/{session}/prompt", {"text": f"{label} SAY=ok", "files": files}
        )
        if status != 200:
            observed[label] = [status, body]
            continue
        until(lambda label=label: f"{label} SAY=ok" in json.dumps(model.requests(session)[-1:]), 10)
        time.sleep(1)
        last = model.requests(session)[-1]["messages"][-1]
        observed[label] = {
            "status": status,
            "stored": body["data"].get("payload"),
            "last_message_role": last.get("role"),
            "content_part_types": [p.get("type") for p in last["content"]]
            if isinstance(last.get("content"), list)
            else "string",
            "file_body_in_request": "Q19_FILE_BODY" in json.dumps(model.requests(session)[-1]["messages"]),
        }
    record(
        19,
        "the prompt's files field",
        schema="files: [{uri: string, name?: string, mention?: {start, end, text}}]",
        **observed,
    )


def q17_overflow():
    """A subscriber that stops reading while the server emits tens of megabytes of events."""
    host, port = client.url.split("//")[1].split(":")
    sock = socket.socket()
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
    sock.connect((host, int(port)))
    sock.sendall(
        f"GET /api/event HTTP/1.1\r\nHost: {host}\r\nAuthorization: {client.headers['Authorization']}\r\n"
        "Accept: text/event-stream\r\n\r\n".encode()
    )
    time.sleep(1)
    session = client.session()
    mark = events.mark()
    # An admitted prompt is one `session.inbox.enqueued` event that carries its text. No model is involved.
    megabyte = "x" * 1_000_000
    statuses = [
        client.call("POST", f"/api/session/{session}/prompt", {"text": megabyte, "resume": False}, timeout=60)[0]
        for _ in range(64)
    ]
    time.sleep(2)
    reader = sum(e["type"] == "session.inbox.enqueued" for e in events.of(session, mark))
    sock.settimeout(8)
    received, ending = 0, "still open, nothing more to read"
    tail, frames = b"", 0
    try:
        while True:
            piece = sock.recv(1 << 20)
            if not piece:
                ending = "closed by the server"
                break
            received += len(piece)
            frames += piece.count(b'"type":"session.inbox.enqueued"')
            tail = (tail + piece)[-400:]
    except TimeoutError:
        pass
    except ConnectionResetError:
        ending = "reset by the server"
    sock.close()
    record(
        17,
        "a subscriber that does not read while 64 events of 1 MB each are emitted",
        prompt_statuses=sorted(set(statuses)),
        events_seen_by_a_reading_subscriber=reader,
        events_received_by_the_stalled_subscriber=frames,
        megabytes_received_by_the_stalled_subscriber=round(received / 1e6, 1),
        stalled_stream_ending=ending,
        tail_of_stalled_stream=tail[-200:].decode("utf-8", "replace"),
        reading_subscriber_still_open=not events.closed.is_set(),
    )


def _stalled_subscriber() -> socket.socket:
    host, port = client.url.split("//")[1].split(":")
    sock = socket.socket()
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
    sock.connect((host, int(port)))
    sock.sendall(
        f"GET /api/event HTTP/1.1\r\nHost: {host}\r\nAuthorization: {client.headers['Authorization']}\r\n"
        "Accept: text/event-stream\r\n\r\n".encode()
    )
    time.sleep(1)
    return sock


def _drain(sock: socket.socket, needle: bytes) -> dict:
    """Read everything a stalled subscriber is still sent, and say how its stream ended."""
    sock.settimeout(8)
    received, frames, tail, ending = 0, 0, b"", "still open, nothing more to read"
    try:
        while True:
            piece = sock.recv(1 << 20)
            if not piece:
                ending = "closed by the server"
                break
            received += len(piece)
            frames += (tail[-len(needle) :] + piece).count(needle)
            tail = (tail + piece)[-400:]
    except TimeoutError:
        pass
    except ConnectionResetError:
        ending = "reset by the server"
    sock.close()
    return {
        "events_received_by_the_stalled_subscriber": frames,
        "megabytes_received_by_the_stalled_subscriber": round(received / 1e6, 1),
        "stalled_stream_ending": ending,
        "tail_of_stalled_stream": tail[-200:].decode("utf-8", "replace"),
        "reading_subscriber_still_open": not events.closed.is_set(),
    }


def overflow_stage():
    """Check 17 once more, harder. An earlier run stalled a subscriber behind 64 events of 1 MB and lost nothing."""
    # By count: three turns of 30,000 text deltas each, which is as many small events.
    sock = _stalled_subscriber()
    mark = events.mark()
    sessions = [client.session() for _ in range(3)]
    for session in sessions:
        assert client.prompt(session, "TICKS=30000")[0] == 200
    outcomes = [events.wait(session, start=mark, timeout=600)["type"] for session in sessions]
    time.sleep(2)
    reader = sum(e["type"] == "session.text.delta" for e in events.of(start=mark))
    record(
        17,
        "a subscriber that does not read while three turns emit 30,000 text deltas each",
        outcomes=outcomes,
        deltas_seen_by_a_reading_subscriber=reader,
        **_drain(sock, b'"type":"session.text.delta"'),
    )
    # OpenCode batches fast deltas, so that was few events. By count again: 5,000 admitted prompts, one event
    # each, spread over 50 sessions because a session's inbox slows down as it grows.
    sock = _stalled_subscriber()
    mark = events.mark()
    statuses = set()
    for _ in range(50):
        session = client.session()
        for number in range(100):
            body = {"text": f"small {number}", "resume": False}
            statuses.add(client.call("POST", f"/api/session/{session}/prompt", body)[0])
    time.sleep(5)
    record(
        17,
        "a subscriber that does not read while 5,000 small events are emitted",
        prompt_statuses=sorted(statuses),
        events_seen_by_a_reading_subscriber=sum(e["type"] == "session.inbox.enqueued" for e in events.of(start=mark)),
        **_drain(sock, b'"type":"session.inbox.enqueued"'),
    )
    # By volume: four times that 64 MB, within the 2 GB of the container.
    sock = _stalled_subscriber()
    session = client.session()
    mark = events.mark()
    megabyte = "x" * 1_000_000
    statuses = [
        client.call("POST", f"/api/session/{session}/prompt", {"text": megabyte, "resume": False}, timeout=120)[0]
        for _ in range(256)
    ]
    time.sleep(5)
    record(
        17,
        "a subscriber that does not read while 256 events of 1 MB each are emitted",
        prompt_statuses=sorted(set(statuses)),
        events_seen_by_a_reading_subscriber=sum(e["type"] == "session.inbox.enqueued" for e in events.of(session, mark)),
        **_drain(sock, b'"type":"session.inbox.enqueued"'),
    )


def main_stage():
    q15_version()
    q1_relay_across_containers()
    q7_mount_headers_and_signal()
    action = q2_q3_q16_mcp_tools_and_hooks()
    q4_permission_rules(action)
    q11_wildcard_and_builtins()
    q6_meta_and_child_session()
    q8_ids_and_metadata()
    q9_throws()
    q14_prompt_during_a_turn()
    q12_packaged_skill()
    q18_outcomes()
    q19_files()
    q5_mcp_timeout_short()
    q17_overflow()
    # For the stage after the restart.
    session = client.session(metadata={"conversation": "loopback:restart"})
    turn(client, events, session, "before the restart SAY=KEPT")
    with open(os.path.join(STATE, "questions-main.json"), "w") as handle:
        json.dump({"session": session, "roster": roster(session)}, handle)


# ---------------------------------------------------------------- after a restart of OpenCode


def restarted_stage():
    with open(os.path.join(STATE, "questions-main.json")) as handle:
        before = json.load(handle)
    session = before["session"]
    listed = client.api("GET", "/api/mcp")
    turn(client, events, session, "after the restart")
    after = roster(session)
    record(
        7,
        "the wire mount after a restart of the OpenCode container",
        api_mcp_after_restart=listed,
        roster_before=before["roster"],
        roster_after=after,
        mount_survived="belle_echo" in after,
        mcp_requests_received_after_restart=[e["method"] for e in mcp.log],
        prior_turn_in_provider_request="KEPT" in said(model.requests(session)[-1], "assistant"),
        relay_prompt_round_trips_after_restart=relay.count("prompt", session),
    )
    status, _ = mount()
    time.sleep(3)
    turn(client, events, session, "after mounting again")
    record(
        7,
        "mounting again after the restart",
        put_status=status,
        roster_has_tool="belle_echo" in roster(session),
        session_metadata_after_restart=client.api("GET", f"/api/session/{session}").get("metadata"),
    )


# ---------------------------------------------------------------- configuration, not the wire


def config_content() -> str:
    """The OPENCODE_CONFIG_CONTENT of the `configured` and `hold` stages."""
    server = {
        "type": "remote",
        "url": MCP_URL,
        "oauth": False,
        "codemode": False,
        "headers": {"Authorization": "Bearer MCP_CONFIG_TOKEN"},
    }
    return json.dumps(
        {
            # `cfglong` is the same server with a 20 minute limit on one call.
            "mcp": {"servers": {"cfgbelle": server, "cfglong": {**server, "timeout": {"execution": 1_200_000}}}},
            "agents": {
                "belle-expert": {
                    "description": "A Belle expert defined in the server configuration",
                    "system": EXPERT_MARK,
                    "mode": "primary",
                }
            },
        }
    )


def configured_stage():
    listed = until(lambda: [s for s in client.api("GET", "/api/mcp") if s["name"] == "cfgbelle"], 15)
    started, first_roster_has_tool = time.monotonic(), None
    for _ in range(50):  # the catalog lags the connection, as it does after a wire mount
        session = client.session()
        turn(client, events, session, "alpha")
        name = next((n for n in roster(session) if n == "cfgbelle_echo"), None)
        first_roster_has_tool = bool(name) if first_roster_has_tool is None else first_roster_has_tool
        if name:
            break
        time.sleep(0.2)
    waited = round(time.monotonic() - started, 2)
    before = len(mcp.calls("echo"))
    if name:
        turn(client, events, session, call(name, token="Q7_CONFIG"))
    record(
        7,
        "an MCP server mounted by the server configuration (mcp.servers), with no experimental call",
        api_mcp=client.api("GET", "/api/mcp"),
        listed=bool(listed),
        advertised_as=name,
        tool_in_the_first_turn_after_startup=first_roster_has_tool,
        seconds_until_the_tool_appeared=waited,
        mcp_calls=len(mcp.calls("echo")) - before,
        header_from_configuration=mcp.log[-1]["headers"].get("authorization") if mcp.log else None,
    )
    agents = {a["id"]: a for a in client.api("GET", "/api/agent")}
    observed = {"agents_listed": sorted(agents), "defined_agent": agents.get("belle-expert")}
    if "belle-expert" in agents:
        session = client.session()
        turn(client, events, session, "alpha")
        policy.set("prompt", session, {"agent": "belle-expert"})
        turn(client, events, session, "beta")
        policy.set("prompt", session, {"agent": "build"})
        turn(client, events, session, "gamma")
        requests = model.requests(session)
        observed["expert_mark_in_system_per_turn"] = [EXPERT_MARK in "".join(said(r, "system")) for r in requests]
        observed["stored_assistant_agents"] = [
            m.get("agent") for m in client.messages(session) if m["type"] == "assistant"
        ]
        observed["roster_sizes"] = [len(tools(r)) for r in requests]
        observed["extra_user_messages_after_switch"] = [u[:80] for u in said(requests[-1], "user")]
        created = client.session(agent="belle-expert")
        turn(client, events, created, "delta")
        observed["session_created_with_the_agent_has_mark"] = EXPERT_MARK in "".join(
            said(model.requests(created)[0], "system")
        )
    record(13, "a named agent defined in the server configuration (agents.<name>)", **observed)


# ---------------------------------------------------------------- long holds


def hold_stage():
    seconds = [int(value) for value in sys.argv[2:]] or [330, 900]
    connected = until(lambda: [s["status"]["status"] for s in client.api("GET", "/api/mcp")] == ["connected"] * 2, 15)
    assert connected, client.api("GET", "/api/mcp")
    time.sleep(3)
    runs = {}

    def hook_hold(hold: int):
        session = client.session()
        policy.set("execute.before", session, lambda event: time.sleep(hold) or {})
        started = time.monotonic()
        try:
            end = turn(client, events, session, call("cfgbelle_echo", token=f"HOLD_{hold}"), timeout=hold + 120)["type"]
        except AssertionError as error:
            end = f"no terminal event: {str(error)[:200]}"
        runs[f"execute.before held {hold} s"] = {
            "terminal": end,
            "turn_took_s": round(time.monotonic() - started, 1),
            "mcp_calls_with_the_token": sum(
                e["params"]["arguments"].get("token") == f"HOLD_{hold}" for e in mcp.calls("echo")
            ),
            "tool_result": tool_results(session) if model.requests(session) else None,
            "execute_before_round_trips": relay.count("execute.before", session),
        }

    def mcp_hold(hold: int, server: str = "cfgbelle"):
        session = client.session()
        started = time.monotonic()
        hold += server == "cfglong"  # one second more, so the two servers' calls are told apart
        try:
            end = turn(client, events, session, call(f"{server}_slow", seconds=hold), timeout=hold + 120)["type"]
        except AssertionError as error:
            end = f"no terminal event: {str(error)[:200]}"
        runs[f"MCP tools/call held {hold} s on {server}"] = {
            "terminal": end,
            "turn_took_s": round(time.monotonic() - started, 1),
            "tools_call_requests": sum(e["params"]["arguments"].get("seconds") == hold for e in mcp.calls("slow")),
            "tool_result": tool_results(session),
        }

    threads = [threading.Thread(target=target, args=(hold,)) for hold in seconds for target in (hook_hold, mcp_hold)]
    threads.append(threading.Thread(target=mcp_hold, args=(max(seconds), "cfglong")))
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    record(10, "a relay hook answer held for minutes", **{k: v for k, v in runs.items() if k.startswith("execute")})
    record(
        5,
        "an MCP tools/call held for minutes: cfgbelle has no timeout setting, cfglong has timeout.execution=1200000",
        **{k: v for k, v in runs.items() if k.startswith("MCP")},
        cancel_notifications=[e["params"] for e in mcp.log if e["method"] == "notifications/cancelled"],
    )


if __name__ == "__main__":
    if stage == "config":
        print(config_content())
        sys.exit(0)
    os.makedirs(ARTEFACTS, exist_ok=True)
    # The listeners first: a server whose configuration mounts the MCP server connects when its
    # location loads, which the readiness call triggers, and a failed connection is not retried.
    model = StandIn(log_path=os.path.join(ARTEFACTS, f"standin-{stage}.jsonl"))
    policy = Policy()
    relay = Relay(token=os.environ.get("BELLE_RELAY_TOKEN", ""))
    relay.answer = policy
    mcp = McpServer()
    mcp.add("echo", lambda a: "echo saw " + a.get("token", ""))
    mcp.add(
        "slow",
        lambda a: time.sleep(float(a["seconds"])) or f"slept {a['seconds']}",
        properties={"seconds": {"type": "number"}},
    )
    client.wait_ready()
    events = Events(client)
    stages = {"main": main_stage, "restarted": restarted_stage, "configured": configured_stage, "hold": hold_stage}
    {**stages, "overflow": overflow_stage}[stage]()
    with open(os.path.join(ARTEFACTS, f"questions-{stage}.jsonl"), "w") as handle:
        handle.writelines(json.dumps(entry) + "\n" for entry in results)
