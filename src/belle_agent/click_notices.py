"""What a person who presses a button is told: on a question, on an approval, and on a turn's stop button.

Every platform shows these words, so a click reads the same on each.
"""

from belle_agent.tools.host import APPROVE, REJECT

#: The label of each option of an approval.
APPROVAL_LABELS = {APPROVE: "Approve", REJECT: "Reject"}

# Shown to the person who clicked and to nobody else, when a clicks rule does
# not let them answer. The question stays open for somebody who may.
ANSWER_REFUSED_NOTICE = "You are not permitted to answer this request."

# Shown on a message that was clicked anyway, in place of the answered note.
WITHDRAWN_QUESTION_NOTE = "This request is no longer waiting for an answer."

# Shown to the person who pressed stop and to nobody else. Three outcomes, and
# they are kept apart because they are three different things to do next: the
# work is being stopped, the turn had already ended so there is nothing to stop,
# or this person may not stop it.
STOP_ACCEPTED_NOTICE = "Stopping this turn."
STOP_STALE_NOTICE = (
    "That turn has already finished, so there is nothing left to stop."
)
STOP_REFUSED_NOTICE = "You are not permitted to stop this turn."


def answered_note(label: str, who: str) -> str:
    """What a question says once it is answered: the option, and who chose it where the platform names them."""
    return f"Answered{f' by {who}' if who else ''}: {label}"


def stop_notice(outcome: str) -> tuple[str, str]:
    """What the person who pressed stop is told of the host's outcome, and the label a log line gives it."""
    if outcome.startswith("stopped"):
        return STOP_ACCEPTED_NOTICE, "stop-accepted"
    if outcome.startswith("refused"):
        return STOP_REFUSED_NOTICE, "stop-refused"
    return STOP_STALE_NOTICE, "stop-stale"
