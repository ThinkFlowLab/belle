"""Shared reply-stream behavior without a platform or host SDK."""

import asyncio

import pytest

from belle_agent.streaming import SnapshotWriter, streamed_remainder


@pytest.mark.parametrize(
    ("sent", "final", "tail"),
    [
        ("", "answer", "answer"),
        ("answer", "answer continues", " continues"),
        ("answer\n", "answer continues", " continues"),
        ("answer continues\n", "answer", ""),
        ("earlier step\n\nanswer begins\n", "answer begins\nthen ends", "then ends"),
        ("answer begins wrong\nanswer begins\n", "answer begins\nthen ends", "then ends"),
        ("the draft ends\n", "endless, really", None),
        ("different", "", ""),
    ],
)
def test_final_tail_matches_only_existing_stream_text(sent, final, tail):
    assert streamed_remainder(sent, final) == tail


async def test_writer_coalesces_updates_and_stop_waits_for_inflight_write():
    entered = asyncio.Event()
    release = asyncio.Event()
    writes = []

    class Surface:
        async def write(self, handle, text, sequence):
            writes.append((handle, text, sequence))
            entered.set()
            await release.wait()

    writer = SnapshotWriter(Surface(), "handle")
    writer.replace("first")
    await entered.wait()
    writer.replace("second")
    writer.replace("latest")
    release.set()
    await asyncio.sleep(0)
    assert writes == [("handle", "first", 0), ("handle", "latest", 0)]

    release.clear()
    entered.clear()
    writer.replace("in flight")
    await entered.wait()
    stopping = asyncio.create_task(writer.stop())
    await asyncio.sleep(0)
    assert not stopping.done()
    writer.replace("pending")
    release.set()
    assert await stopping == "handle"
    assert writes[-1] == ("handle", "in flight", 0)


async def test_writer_cancel_stops_a_waiting_writer():
    class Surface:
        async def write(self, *args):
            raise AssertionError("there is no snapshot to write")

    writer = SnapshotWriter(Surface(), "handle")
    writer.cancel()
    assert await writer.stop() == "handle"
