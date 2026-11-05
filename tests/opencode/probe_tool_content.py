"""What OpenCode does with tool-result content that is not text, and with a file attached to a prompt.

    python tests/opencode/probe_tool_content.py [form...]

Run it beside OpenCode, whose `API_BASE` names the recording proxy (OCB_PROXY).
With the stand-in model behind the proxy it costs nothing: it shows what
OpenCode accepts, what it stores and what it sends to the model endpoint. With
the provider behind the proxy it sends the named forms to the real model, one
request each, to learn whether the model sees the content. The recording proxy
answers the first request of each turn with the tool call, so the model is
asked once, with the tool's result in hand. Each line of output is one JSON
record. Long strings are cut to their length and their first characters.

A form is one way to hand content over: an MCP content block in a tool result
(``image``, ``audio``, ``resource-text``, ``resource-png``, ``resource-pdf``,
``resource-link``), a file attached to the prompt (``prompt-png``,
``prompt-pdf``), or an image of a given size in a tool result (``image-1m``,
``image-4m``, ``image-16m``, ``image-28m``).
"""

import base64
import json
import os
import pathlib
import secrets
import struct
import sys
import zlib

sys.path[:0] = [str(pathlib.Path(__file__).parent), str(pathlib.Path(__file__).parents[1] / "conformance")]

import httpx
from drive import Client, Events, mount, turn
from listeners import McpServer

PROXY = os.environ.get("OCB_PROXY", "")
#: Whether the model behind the proxy is the real one. Settled in ``main`` from the model OpenCode names.
REAL = True
ARTEFACTS = os.path.join(os.environ.get("OCB_STATE", "/state"), "artefacts")
DENIED = [
    {"action": name, "resource": "*", "effect": "deny"}
    for name in (*filter(None, os.environ.get("OCB_KEY_REACHING_TOOLS", "").split(",")), "skill", "question")
]

# A 5 by 7 bitmap of the digit 7.
SEVEN = ("11111", "00001", "00010", "00100", "01000", "01000", "01000")
CODE = "PROBE-4711"


def png(side: int = 256, noise: int = 0) -> bytes:
    """A white 7 on red. ``noise`` bytes of random pixels in a band at the foot make the file as large as asked."""
    cell = side // 9
    rows = []
    band = (noise // (side * 3)) + 1 if noise else 0
    for y in range(side + band):
        row = bytearray()
        for x in range(side):
            column, line = x // cell - 2, y // cell - 1
            lit = 0 <= line < 7 and 0 <= column < 5 and SEVEN[line][column] == "1"
            row += b"\xff\xff\xff" if lit else b"\xd0\x10\x10"
        rows.append(b"\x00" + (os.urandom(side * 3) if y >= side else bytes(row)))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    header = struct.pack(">IIBBBBB", side, side + band, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(b"".join(rows), 1)) + chunk(b"IEND", b"")


def pdf() -> bytes:
    """One page that reads the code, with a correct cross-reference table."""
    stream = f"BT /F1 36 Tf 60 700 Td ({CODE}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (number, body)
    start = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, start)
    return bytes(out)


def wav() -> bytes:
    """A quarter second of silence, 8 kHz mono."""
    data = b"\x80" * 2000
    return b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVEfmt " + struct.pack("<IHHIIHH", 16, 1, 1, 8000, 8000, 1, 8) + b"data" + struct.pack("<I", len(data)) + data


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def blocks(form: str) -> list[dict]:
    said = {"type": "text", "text": "The content follows."}
    if form == "image":
        return [said, {"type": "image", "data": b64(png()), "mimeType": "image/png"}]
    if form.startswith("image-"):
        return [said, {"type": "image", "data": b64(png(noise=int(form[6:-1]) * 1_000_000)), "mimeType": "image/png"}]
    if form == "audio":
        return [said, {"type": "audio", "data": b64(wav()), "mimeType": "audio/wav"}]
    if form == "resource-text":
        return [said, {"type": "resource", "resource": {"uri": "file:///probe.txt", "mimeType": "text/plain", "text": f"The code is {CODE}."}}]
    if form == "resource-png":
        return [said, {"type": "resource", "resource": {"uri": "file:///probe.png", "mimeType": "image/png", "blob": b64(png())}}]
    if form == "resource-pdf":
        return [said, {"type": "resource", "resource": {"uri": "file:///probe.pdf", "mimeType": "application/pdf", "blob": b64(pdf())}}]
    if form == "resource-link":
        return [said, {"type": "resource_link", "uri": "https://probe.invalid/probe.png", "name": "probe.png", "mimeType": "image/png"}]
    raise SystemExit(f"no form {form}")


def cut(value, keep: int = 80):
    """The same structure with each long string as its length and its start."""
    if isinstance(value, str):
        return value if len(value) <= keep * 3 else f"<{len(value)} chars: {value[:keep]}...>"
    if isinstance(value, list):
        return [cut(item, keep) for item in value]
    if isinstance(value, dict):
        return {key: cut(item, keep) for key, item in value.items()}
    return value


def exchanges(session: str) -> list[dict]:
    path = os.path.join(ARTEFACTS, "provider-requests.jsonl")
    with open(path) as handle:
        entries = [json.loads(line) for line in handle]
    return [e for e in entries if e.get("session") == session and e.get("answered") != "title"]


QUESTION = (
    " Then answer in one line: which digit does the picture show and what colour is its background, or which"
    " code does the document state. If you were given no picture and no document, answer exactly NOTHING RECEIVED."
)


def probe(client: Client, events: Events, form: str) -> dict:
    session = client.session(permissions=DENIED)
    files = None
    if form.startswith("prompt-"):
        kind, data = ("image/png", png()) if form == "prompt-png" else ("application/pdf", pdf())
        files = [{"uri": f"data:{kind};base64,{b64(data)}", "name": f"probe.{kind.split('/')[1]}"}]
        text = f"{secrets.token_hex(3)} Look at the attached file." + QUESTION
    else:
        text = f"{secrets.token_hex(3)} Call the tool probe_content once with the form {form}." + QUESTION
        httpx.post(PROXY + "/_script", json={"answers": [{"calls": [["probe_content", {"form": form}]]}]}).raise_for_status()
    end = turn(client, events, session, text, timeout=240, **({"files": files} if files else {}))
    seen = exchanges(session)
    last = seen[-1] if seen else {}
    messages = last.get("body", {}).get("messages", [])
    user = max((i for i, m in enumerate(messages) if m.get("role") == "user" and text[:6] in json.dumps(m)), default=0)
    if files:
        messages = [{"the_prompt_message": messages[user]}, *messages[user:]]
        user = 0
    stored = [
        {"role": message.get("role") or (message.get("info") or {}).get("role"), "parts": cut(message.get("parts") or message)}
        for message in client.messages(session)
    ]
    response = last.get("response", {})
    return {
        "form": form,
        "real_model": REAL,
        "ended": end.get("type"),
        "error": cut((end.get("data") or {}).get("error")),
        "requests": len(seen),
        "sent_to_the_model": cut(messages[user:]),
        "request_bytes": len(json.dumps(last.get("body", {}))),
        "model_answered": {"status": response.get("status"), "text": (response.get("summary") or {}).get("content") or response.get("local")},
        "stored": stored,
    }


def main() -> None:
    global REAL
    forms = sys.argv[1:] or ["image", "audio", "resource-text", "resource-png", "resource-pdf", "resource-link", "prompt-png", "prompt-pdf"]
    client = Client()
    REAL = "standin" not in str(client.wait_ready().get("id"))
    if not REAL:
        # The proxy forwards to the stand-in of this process, which answers a request it is not told about with "pong".
        from opencode_kit import standin

        standin()
    # What OpenCode holds about the model, without its settings: those hold the provider key.
    default = client.call("GET", "/api/model/default")[1]
    described = {key: (default.get("data") or {}).get(key) for key in ("capabilities", "limit")}
    print(json.dumps({"model_as_opencode_describes_it": cut(described)}), flush=True)
    events = Events(client)
    mcp = McpServer()
    mcp.add("content", lambda arguments: blocks(arguments.get("form", "")), "Return the probe content.", {"form": {"type": "string"}})
    mount(client, "probe")
    for form in forms:
        try:
            print(json.dumps(probe(client, events, form), ensure_ascii=False), flush=True)
        except Exception as exc:  # noqa: BLE001 - one form that fails is a result, and the next is still run
            print(json.dumps({"form": form, "probe_failed": f"{type(exc).__name__}: {str(exc)[:600]}"}), flush=True)
    events.close()
    mcp.close()


if __name__ == "__main__":
    main()
