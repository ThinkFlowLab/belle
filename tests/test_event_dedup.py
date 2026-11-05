"""Deduplicate events during a storage outage and after recovery."""


from belle_agent.admission.dedup import EventDedupStore


async def test_read_failure_uses_the_remembered_window(tmp_path, monkeypatch):
    path = tmp_path / "seen.json"
    store = EventDedupStore(path)
    assert await store.remember("handled")
    original = type(path).read_text

    def unavailable(candidate, *args, **kwargs):
        if candidate == path:
            raise PermissionError("temporarily unreadable")
        return original(candidate, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(type(path), "read_text", unavailable)
        assert await store.holds("handled")
        assert not await store.remember("handled")
        assert await store.remember("during-outage")
        assert await store.holds("during-outage")

    assert await store.holds("during-outage")
    assert not await store.remember("during-outage")
    restarted = EventDedupStore(path)
    assert await restarted.holds("handled")
    assert await restarted.holds("during-outage")


async def test_write_failure_keeps_memory_events_after_recovery(tmp_path, monkeypatch):
    path = tmp_path / "seen.json"
    store = EventDedupStore(path, max_entries=3)
    assert await store.remember("before")

    def unavailable(*args):
        raise OSError("temporarily unwritable")

    with monkeypatch.context() as patch:
        patch.setattr(store, "_write_unlocked", unavailable)
        assert await store.remember("during-outage")

    assert await store.remember("after")
    restarted = EventDedupStore(path)
    for key in ("before", "during-outage", "after"):
        assert not await restarted.remember(key)


async def test_healthy_store_does_not_restore_evicted_memory_entries(tmp_path):
    path = tmp_path / "seen.json"
    first = EventDedupStore(path, max_entries=1)
    second = EventDedupStore(path, max_entries=1)
    assert await first.remember("old")
    assert await second.remember("new")
    assert await first.remember("old")
