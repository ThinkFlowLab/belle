#!/usr/bin/env python3
"""A stand-in for belle-matrix-helper: the same fd-3 socket and protocol, with no Matrix behind it.

``FAKE_HELPER_MODE`` picks a behaviour: ``normal``, ``badproto`` (a hello of another protocol),
``stubborn`` (ignores shutdown and SIGTERM), ``crash`` (exits on ``send``), ``alert`` (reports a
rolled-back store after its start), ``slow`` (answers each request after ``FAKE_HELPER_DELAY`` seconds),
``die`` (exits with code 7 right after its start), ``unreachable`` (refuses its start: the homeserver did not answer).
``FAKE_HELPER_LOG`` names a file that gets each request as one JSON line.
"""

import json
import os
import signal
import socket
import sys
import time

from belle_agent.platforms.matrix.protocol import PROTOCOL

MODE = os.environ.get("FAKE_HELPER_MODE", "normal")
LOG = os.environ.get("FAKE_HELPER_LOG", "")
DELAY = float(os.environ.get("FAKE_HELPER_DELAY", "0"))
ROOM = "!dm:syn.example"
USER = "@belle:syn.example"


def main() -> None:
    if sys.argv[1:2] != ["--max-line-bytes"] or not sys.argv[2:3]:
        sys.exit("usage: fake_helper.py --max-line-bytes <bytes>")
    if MODE == "stubborn":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    sock = socket.socket(fileno=3)
    stream = sock.makefile("rwb")

    def send(message: dict) -> None:
        stream.write(json.dumps(message).encode() + b"\n")
        stream.flush()

    send({"event": "hello", "protocol": 99 if MODE == "badproto" else PROTOCOL, "helper_version": "0.0.0-fake", "sdk_version": "none"})
    print("fake helper ready", file=sys.stderr, flush=True)
    for line in stream:
        request = json.loads(line)
        if LOG:
            with open(LOG, "a") as log:
                log.write(json.dumps(request) + "\n")
        if DELAY:
            time.sleep(DELAY)
        rid, op = request["id"], request["op"]
        if op == "start":
            if MODE == "unreachable":
                send({"id": rid, "ok": False, "error": {"code": "helper_start_refused", "reason": "homeserver_unreachable",
                                                         "message": "error sending request: connection refused"}})
                continue
            if request["store_passphrase"] == "wrong":
                send({"id": rid, "ok": False, "error": {"code": "helper_start_refused", "reason": "store_open_failed",
                                                         "message": "cannot open the store: aead::Error"}})
                continue
            if request["store_passphrase"] == "locked":
                send({"id": rid, "ok": False, "error": {"code": "helper_start_refused", "reason": "store_locked",
                                                         "message": "another helper process uses this store"}})
                continue
            if request.get("reset_identity"):
                send({"id": rid, "ok": False, "error": {"code": "helper_start_refused", "reason": "identity_reset",
                                                         "message": "the identity was reset"}})
                continue
            if "recovery_key" not in request and request.get("password"):
                send({"id": rid, "ok": False, "error": {"code": "helper_start_refused", "reason": "recovery_key_missing",
                                                         "message": "this new device has no recovery key"}})
                continue
            send({"id": rid, "ok": True, "result": {"user_id": USER, "device_id": "FAKEDEVICE", "new_device": False, "backup": "enabled"}})
            if MODE == "die":
                print("fake helper dies after its start", file=sys.stderr, flush=True)
                time.sleep(0.05)
                os._exit(7)
            if MODE == "alert":
                send({"event": "alert", "kind": "store_rolled_back", "message": "duplicate one-time key"})
                continue
            send({"event": "invite", "seq": 1, "room": ROOM, "inviter": "@alice:syn.example"})
            send({"event": "timeline", "seq": 2, "room": ROOM, "encrypted": True, "direct": True, "room_event": {
                "type": "m.room.message", "sender": "@alice:syn.example", "event_id": "$hello",
                "content": {"msgtype": "m.text", "body": "hello belle"}}})
            send({"event": "timeline", "seq": 3, "room": ROOM, "encrypted": True, "direct": True, "room_event": {
                "type": "m.reaction", "sender": "@alice:syn.example", "event_id": "$like", "content": {}}})
        elif op == "send":
            if MODE == "crash":
                print("fake helper crashes on send", file=sys.stderr, flush=True)
                os._exit(3)
            send({"id": rid, "ok": True, "result": {"event_id": f"$sent{rid}"}})
        elif op == "room_info":
            send({"id": rid, "ok": True, "result": {"room": request["room"], "encrypted": True, "public": False, "direct": request["room"] == ROOM,
                                                    "joined_members": 2}})
        elif op == "event":
            send({"id": rid, "ok": False, "error": {"code": "M_NOT_FOUND", "message": "Event not found."}})
        elif op == "redact":
            send({"id": rid, "ok": True, "result": {"event_id": f"$redaction{rid}"}})
        elif op == "devices":
            send({"id": rid, "ok": True, "result": {"devices": [
                {"device_id": "FAKEDEVICE", "display_name": "belle", "last_seen_ts": 1792277730000, "current": True},
                {"device_id": "STALE", "current": False}]}})
        elif op == "shutdown":
            if MODE == "stubborn":
                continue
            send({"id": rid, "ok": True, "result": {}})
            return
        else:
            send({"id": rid, "ok": True, "result": {}})


if __name__ == "__main__":
    main()
