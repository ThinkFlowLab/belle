"""Render Mattermost posts within the server's character budget."""

from belle_agent.platforms.rendering import RenderedPart, measure_of
from belle_agent.text_split import split_index, split_text

_MEASURE = measure_of("chars")


class MattermostRenderer:
    """Render Markdown as Mattermost message text."""

    def measure(self, markdown: str) -> int:
        """Measure post characters."""
        return _MEASURE(markdown)

    def parts(self, markdown: str, budget: int) -> list[RenderedPart[dict[str, str]]]:
        """Split posts by character count; zero means unlimited."""
        found = split_text(markdown, budget, preserve_whitespace=True) if budget and len(markdown) > budget else [markdown]
        return [RenderedPart(part, {"message": part}) for part in found]

    def cut_index(self, markdown: str, budget: int) -> int:
        """Return the first post's cut index."""
        return split_index(markdown, budget)

    def escape(self, text: str) -> str:
        """Keep Mattermost Markdown and mentions intact."""
        return text


RENDERER = MattermostRenderer()
