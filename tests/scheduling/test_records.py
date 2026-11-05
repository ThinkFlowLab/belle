"""Record ids have their approved form, and a stored record that is wrong is refused by the path of its field."""

import pytest

from belle_agent.scheduling.records import (
    RUN_ID,
    SESSION_ID,
    RecordInvalid,
    SessionRun,
    new_job_id,
    new_run_id,
    new_session_id,
)

from .records_kit import detached


def test_ids_have_the_approved_form() -> None:
    jobs = {new_job_id() for _ in range(1000)}
    assert len(jobs) == 1000
    assert all(len(value) == 14 and value.startswith("job-") and value[4:].isalnum() for value in jobs)
    assert SESSION_ID.fullmatch(new_session_id())
    assert RUN_ID.fullmatch(new_run_id())


def test_a_wrong_record_in_a_record_is_named_by_its_field() -> None:
    run = detached().runs[0].to_json()
    del run["request"]["fingerprint"]
    with pytest.raises(RecordInvalid, match="run.request misses fingerprint"):
        SessionRun.from_json(run, "run")
