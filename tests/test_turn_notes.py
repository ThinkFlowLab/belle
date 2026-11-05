"""The notes for a conversation's next turn, kept across a restart."""

import json
import logging

from belle_agent.host import notes as turn_notes
from belle_agent.host.notes import MESSAGE, SYSTEM_ADDITIONS, TurnNotes


def test_a_note_survives_a_restart_and_is_taken_once(tmp_path):
    path = tmp_path / "state" / "turn_notes.json"
    TurnNotes(path).add(MESSAGE, "loopback:C1", "stopped")
    TurnNotes(path).add(SYSTEM_ADDITIONS, "loopback:C1", "refused")

    after = TurnNotes(path)
    assert after.take("loopback:C1") == {SYSTEM_ADDITIONS: ["refused"], MESSAGE: ["stopped"]}
    assert TurnNotes(path).take("loopback:C1") == {SYSTEM_ADDITIONS: [], MESSAGE: []}


def test_the_file_keeps_the_bounds_of_memory(tmp_path, monkeypatch):
    monkeypatch.setattr(turn_notes, "MAX_NOTED_SESSIONS", 2)
    monkeypatch.setattr(turn_notes, "MAX_NOTE_CHARS", 3)
    path = tmp_path / "turn_notes.json"
    notes = TurnNotes(path)
    for key in ("a", "b", "c"):
        notes.add(MESSAGE, key, "longer")

    assert json.loads(path.read_text())[MESSAGE] == {"b": ["lon"], "c": ["lon"]}
    # A file written with more is read within the bounds too.
    path.write_text(json.dumps({"version": 1, MESSAGE: {"a": ["x"], "b": ["y"], "c": ["longer"]}}))
    after = TurnNotes(path)
    assert [after.take(key)[MESSAGE] for key in ("a", "b", "c")] == [[], ["y"], ["lon"]]


def test_a_corrupt_file_is_named_and_ignored(tmp_path, caplog):
    path = tmp_path / "turn_notes.json"
    caplog.set_level(logging.WARNING, logger=turn_notes.__name__)
    for content, said in (
        ("{not json", "is unreadable"),
        ('{"version": 2}', "version 1"),
        ('{"version": 1, "message": {"C1": "one"}}', "message is not a map of conversation keys to lists of strings"),
    ):
        path.write_text(content)
        caplog.clear()

        assert TurnNotes(path).take("C1") == {SYSTEM_ADDITIONS: [], MESSAGE: []}
        assert [str(path) in record.getMessage() and said in record.getMessage() for record in caplog.records] == [True]


def test_an_unwritable_directory_keeps_the_notes_in_memory_and_says_so_once(tmp_path, caplog):
    blocker = tmp_path / "file"
    blocker.write_text("")
    caplog.set_level(logging.WARNING, logger=turn_notes.__name__)
    notes = TurnNotes(blocker / "turn_notes.json")

    notes.add(MESSAGE, "C1", "one")
    notes.add(MESSAGE, "C1", "two")

    assert notes.take("C1")[MESSAGE] == ["one", "two"]
    written = [record.getMessage() for record in caplog.records if "cannot be written" in record.getMessage()]
    assert len(written) == 1 and str(blocker) in written[0]
