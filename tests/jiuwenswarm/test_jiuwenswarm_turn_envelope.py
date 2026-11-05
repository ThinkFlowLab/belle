"""Check the rail against JiuwenSwarm's rendered turn envelope."""

from types import SimpleNamespace

import pytest
from jiuwenswarm.server.runtime.agent_adapter.user_turn import UserTurn

from belle_agent.integrations.openjiuwen.rails.second_brain_rail import SecondBrainRail
from belle_agent.second_brain.recall import query_text

_QUESTION = "What taper width {and} gating temperature does the deployment pin?"


def _rendered(text, **kwargs):
    kwargs.setdefault("channel", "slack")
    kwargs.setdefault("language", "en")
    return UserTurn(text=text, files={}, **kwargs).render()


@pytest.mark.parametrize(
    "turn",
    [
        {},
        {"language": "zh"},
        {"channel": "cron"},
        {"channel": "__cron__"},
        {"metadata": {"sender_name": "Requester", "chat_type": "channel"}},
        {"metadata": {"interaction_context": 'Re: {"source": "x", "content": "decoy"}'}},
    ],
)
def test_rail_extracts_the_message_from_the_envelope(turn) -> None:
    rendered = _rendered(_QUESTION, **turn)
    assert rendered != _QUESTION
    assert query_text(rendered) == _QUESTION


@pytest.mark.asyncio
async def test_rail_searches_with_the_user_message(tmp_path, monkeypatch) -> None:
    from openjiuwen.core.single_agent.rail.base import InvokeInputs

    async def search(manager, query, *, limit, min_score, **_):
        return await manager.search(
            query, opts={"maxResults": limit, "minScore": min_score}
        )

    monkeypatch.setattr("belle_agent.second_brain.recall.search_corpus", search)

    class Manager:
        query = None

        async def search(self, query, opts=None):
            self.query = query
            return []

    rail = SecondBrainRail(corpus_dir=tmp_path)
    rail._manager = manager = Manager()
    await rail.before_invoke(SimpleNamespace(inputs=InvokeInputs(query=_rendered(_QUESTION))))
    assert rail._prefetch_task is not None
    await rail._prefetch_task
    assert manager.query == _QUESTION
