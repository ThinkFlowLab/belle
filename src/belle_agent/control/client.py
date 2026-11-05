"""One-call asynchronous client and command-line snapshot probe."""

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from belle_agent.control.peek import (
    PeekOpenResult,
    PeekPollResult,
    validate_peek_open_result,
    validate_peek_poll_result,
)
from belle_agent.control.protocol import (
    MAX_REQUEST_BYTES,
    MAX_RESPONSE_BYTES,
    VERSION,
    ControlError,
    Snapshot,
    decode_line,
    decode_response,
    encode_line,
    encode_request,
    validate_request,
)


async def get_snapshot(socket: Path, *, request_id: str | None = None, timeout: float = 2.0) -> Snapshot:
    """Read one validated snapshot. Transport failures raise OSError or TimeoutError."""
    identity = request_id if request_id is not None else uuid4().hex
    request = encode_request(identity)
    return await _request(socket, request, identity, timeout)


async def toggle_schedule(socket: Path, job_id: str, expected_state: str, instance_id: str) -> Snapshot:
    """Toggle only the job state shown by the selected service instance."""
    return await _control_action(socket, "schedule.toggle", "job_id", job_id, expected_state, instance_id)


async def stop_work(socket: Path, work_id: str, expected_state: str, instance_id: str) -> Snapshot:
    """Stop the selected running work item."""
    return await _control_action(socket, "work.stop", "work_id", work_id, expected_state, instance_id)


async def drop_work(socket: Path, work_id: str, expected_state: str, instance_id: str) -> Snapshot:
    """Drop the selected queued work item."""
    return await _control_action(socket, "work.drop", "work_id", work_id, expected_state, instance_id)


async def delete_schedule(socket: Path, job_id: str, expected_state: str, instance_id: str) -> Snapshot:
    """Delete the selected scheduled job."""
    return await _control_action(socket, "schedule.delete", "job_id", job_id, expected_state, instance_id)


async def peek_open(socket: Path, work_id: str, instance_id: str) -> PeekOpenResult:
    """Open a live peek on a running main turn."""
    identity = uuid4().hex
    request = encode_line(validate_request({
        "version": VERSION, "id": identity, "method": "peek.open",
        "params": {"work_id": work_id, "instance_id": instance_id},
    }), MAX_REQUEST_BYTES)
    return validate_peek_open_result(await _request_result(socket, request, identity, 2.0))


async def peek_poll(socket: Path, peek_id: str, after_seq: int) -> PeekPollResult:
    """Poll frames after ``after_seq`` for an open peek."""
    identity = uuid4().hex
    request = encode_line(validate_request({
        "version": VERSION, "id": identity, "method": "peek.poll",
        "params": {"peek_id": peek_id, "after_seq": after_seq},
    }), MAX_REQUEST_BYTES)
    return validate_peek_poll_result(await _request_result(socket, request, identity, 2.0))


async def peek_close(socket: Path, peek_id: str) -> None:
    """Close a peek subscriber."""
    identity = uuid4().hex
    request = encode_line(validate_request({
        "version": VERSION, "id": identity, "method": "peek.close",
        "params": {"peek_id": peek_id},
    }), MAX_REQUEST_BYTES)
    await _request_result(socket, request, identity, 2.0)


async def _control_action(socket: Path, method: str, id_field: str, value: str,
                          expected_state: str, instance_id: str) -> Snapshot:
    identity = uuid4().hex
    request = encode_line(validate_request({
        "version": VERSION, "id": identity, "method": method,
        "params": {id_field: value, "expected_state": expected_state, "instance_id": instance_id},
    }), MAX_REQUEST_BYTES)
    return await _request(socket, request, identity, 2.0)


async def _request(socket: Path, request: bytes, identity: str, timeout: float) -> Snapshot:
    return decode_response(await _read_response(socket, request, timeout), identity)


async def _request_result(socket: Path, request: bytes, identity: str, timeout: float) -> Any:
    value = decode_line(await _read_response(socket, request, timeout), MAX_RESPONSE_BYTES)
    if not isinstance(value, dict) or set(value) not in ({'version', 'id', 'result'}, {'version', 'id', 'error'}):
        raise ControlError('invalid_request', 'response must have version, id, and exactly one of result or error')
    if type(value['version']) is not int or value['version'] != VERSION:
        raise ControlError('unsupported_version', f'response.version must be {VERSION}')
    if value['id'] != identity:
        raise ControlError('invalid_request', 'response.id must be the request id')
    if 'error' in value:
        error = value['error']
        if not isinstance(error, dict):
            raise ControlError('invalid_request', 'response.error must be an object')
        raise ControlError(str(error.get('code') or 'invalid_request'), str(error.get('message') or 'error'))
    return value['result']


async def _read_response(socket: Path, request: bytes, timeout: float) -> bytes:
    async with asyncio.timeout(timeout):
        reader, writer = await asyncio.open_unix_connection(socket, limit=MAX_RESPONSE_BYTES)
        try:
            writer.write(request)
            await writer.drain()
            try:
                return await reader.readline()
            except ValueError:
                raise ControlError('message_too_large', 'response exceeds max_response_bytes') from None
        finally:
            writer.close()
            await writer.wait_closed()


def main() -> None:
    """Print one snapshot from the socket selected by the operator."""
    parser = argparse.ArgumentParser(description='Read one Belle status snapshot')
    parser.add_argument('--socket', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(get_snapshot(args.socket)), indent=2))


if __name__ == '__main__':
    main()
