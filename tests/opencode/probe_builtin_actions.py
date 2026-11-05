"""Which permission action gates each of OpenCode's built-in tools, measured with the stand-in model.

    python tests/opencode/probe_builtin_actions.py

Run it in the test container beside the plain OpenCode image, with the
variables of an OpenCode check (`containers/test/README.md`) and OCB_PLAIN=1.
OpenCode offers ``todowrite`` only with ``{"experimental": {"todo": true}}`` in
its configuration, which the guide gives it. The probe stops when the tool is
not offered. The probe mounts an MCP server with no tools, as Belle mounts
its own: ``opencode_list_mcp_resources`` asks about each mounted server.

The adapter writes a scope's tool levels as session permission rules, and a
rule names an action, not a tool. This probe measures the pinned build for
the table `integrations/opencode/permissions.py` holds. It drives the wire
itself, so the adapter's own translation takes no part. Each line of output
is one JSON record.

The roster holds the tools of a provider request and the Code Mode tools
that its catalog lists, each by the name OpenCode gives it:
``opencode.worktree_create`` is ``opencode_worktree_create``. The model calls a
Code Mode tool through ``execute``.

- ``asked``: the scripted model calls each tool once under the one rule
  ``* ask``, and once under an ``ask`` rule on the tool's own name. The action
  and resources of the permission request it raises are recorded, and the
  request is rejected. A tool that answers was not asked about.
- ``denied``: under one ``deny`` rule per name, tool or action, the tools that
  leave the roster of a provider request are recorded beside a control. Under
  a deny of a Code Mode tool, the model also calls that tool and then
  ``opencode_models`` in one ``execute``, and the result is recorded.
"""

import json
import pathlib
import re
import secrets
import sys
import time

sys.path[:0] = [str(pathlib.Path(__file__).parent), str(pathlib.Path(__file__).parents[1] / "conformance")]

from drive import Client, Events, mount, turn
from listeners import McpServer
from opencode_kit import standin
from standin import call, said, tools

CALLS = {
    "shell": {"command": "echo probe", "description": "a probe"},
    "execute": {"code": "1"},
    "read": {"path": "/workspace"},
    "grep": {"pattern": "probe"},
    "glob": {"pattern": "*"},
    "edit": {"path": "/workspace/.opencode/skills/chat-digest/SKILL.md", "oldString": "chat-digest", "newString": "chat-digest-PROBE"},
    "write": {"path": "/workspace/PROBE.txt", "content": "probe"},
    "webfetch": {"url": "http://probe.invalid/"},
    "websearch": {"query": "probe"},
    "subagent": {"agent": "general", "description": "a probe", "prompt": "SAY=x"},
    "skill": {"id": "chat-digest"},
    "question": {"questions": [{"question": "a?", "header": "a", "options": [{"label": "y", "description": "y"}]}]},
    # Offered only under `experimental.todo`, which the probe's OpenCode needs in its configuration.
    "todowrite": {"todos": [{"content": "a probe", "status": "pending"}]},
}
# Code Mode tools, by the name OpenCode gives each, and the arguments of a call through `execute`.
CODE_MODE = {
    "opencode_session_rename": {"title": "a probe"},
    "opencode_session_move": {"directory": "/workspace"},
    "opencode_worktree_create": {"name": "probe"},
    "opencode_worktree_list": {},
    "opencode_worktree_remove": {"directory": "/workspace/probe-none"},
    "opencode_models": {"query": "probe"},
    "opencode_list_mcp_resources": {},
    "opencode_read_mcp_resource": {"server": "none", "uri": "probe://none"},
}
CATALOG_ENTRY = re.compile(r"^\s*- tools\.([\w.]+)\(", re.MULTILINE)
# Actions the permission engine knows that are no tool's name.
OTHER_ACTIONS = ("external_directory",)


def code(name: str) -> str:
    """The `execute` code that calls the Code Mode tool ``name``."""
    namespace, tool = name.split("_", 1)
    return f"return await tools.{namespace}.{tool}({json.dumps(CODE_MODE[name])})"


def directive(name: str) -> str:
    return call("execute", code=code(name)) if name in CODE_MODE else call(name, **CALLS[name])


def offered(body: dict) -> set[str]:
    """The tools one provider request offers, with the Code Mode tools of its catalog."""
    listed = {path.replace(".", "_") for text in said(body, "system") for path in CATALOG_ENTRY.findall(text)}
    return set(tools(body)) | listed


def roster(client: Client, events: Events, model, rules: list[dict]) -> set[str]:
    session = client.session(permissions=rules)
    turn(client, events, session, f"roster {secrets.token_hex(4)} SAY=x")
    return offered(model.requests(session)[0])


def denied_call(client: Client, events: Events, model, name: str) -> dict:
    """Under a deny of the Code Mode tool ``name``, call it and then another Code Mode tool in one ``execute``."""
    session = client.session(permissions=[{"action": name, "resource": "*", "effect": "deny"}])
    other = "opencode_models" if name != "opencode_models" else "opencode_worktree_list"
    script = f"const r = {{}}; try {{ r.denied = await (async () => {{ {code(name)} }})() }} catch (e) {{ r.denied = String(e) }}; "
    script += f"r.other = await (async () => {{ {code(other)} }})(); return r"
    turn(client, events, session, f"{secrets.token_hex(4)} " + call("execute", code=script))
    answered = [text for body in model.requests(session) for text in said(body, "tool")]
    return {"denied": name, "called with": other, "answer": (answered or [""])[-1][:400]}


def asked(client: Client, events: Events, model, name: str, action: str) -> dict:
    """Call one tool under an ``ask`` rule on ``action`` and report the permission request it raises, if any.

    The request is read from its event: ``GET .../permission`` answers 400 while a request of
    ``grep`` or ``glob`` with no path waits, because its metadata holds a value that is not JSON.
    """
    session = client.session(permissions=[{"action": action, "resource": "*", "effect": "ask"}])
    start = events.mark()
    client.prompt(session, f"{secrets.token_hex(4)} " + directive(name))
    waited = {"permission.asked", "form.created", "session.execution.succeeded", "session.execution.failed"}
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        seen = [event for event in events.of(None, start) if event.get("type") in waited and session in json.dumps(event)]
        if seen:
            break
        time.sleep(0.2)
    requests = [event["data"] for event in seen if event["type"] == "permission.asked"]
    for request in requests:
        client.call("POST", f"/api/session/{session}/permission/{request['id']}/reply", {"decision": "reject"})
    client.call("POST", f"/api/session/{session}/interrupt", {})
    answered = [text for body in model.requests(session) for text in said(body, "tool")]
    return {
        "tool": name,
        "rule": f"{action} ask",
        "asked": [{"action": r.get("action"), "resources": r.get("resources")} for r in requests],
        "then": sorted({event["type"] for event in seen} - {"permission.asked"}),
        "answer": (answered or [""])[-1][:160],
    }


def main() -> None:
    model = standin()
    client = Client()
    client.wait_ready()
    events = Events(client)
    mcp = McpServer()
    mount(client, "probe")
    control = roster(client, events, model, [])
    print(json.dumps({"built-in tools offered with no rule": sorted(control)}), flush=True)
    if "todowrite" not in control:
        sys.exit('todowrite is not offered: give OpenCode {"experimental": {"todo": true}}')
    for name in (*CALLS, *CODE_MODE):
        for action in ("*", name):
            print(json.dumps(asked(client, events, model, name, action)), flush=True)
    for name in (*CALLS, *CODE_MODE, *OTHER_ACTIONS):
        gone = control - roster(client, events, model, [{"action": name, "resource": "*", "effect": "deny"}])
        print(json.dumps({"denied": name, "tools gone from the roster": sorted(gone)}), flush=True)
    for name in CODE_MODE:
        print(json.dumps(denied_call(client, events, model, name)), flush=True)
    events.close()
    mcp.close()


if __name__ == "__main__":
    main()
