"""The answer text of one JiuwenSwarm request, a paragraph apart for each model call."""

import time
from dataclasses import dataclass, field


def after_step(text: str) -> str:
    """Return ``text`` ready for the next model call's text, a paragraph below it."""
    return text.rstrip() + "\n\n" if text.strip() else text


@dataclass
class StepText:
    """What the model calls of a request wrote, as JiuwenSwarm joins it and as Belle shows it.

    JiuwenSwarm's final carries the text of every model call of the request with nothing between them,
    so the last sentence of one call runs into the first of the next. A tool result ends a call.
    """

    joined: str = ""
    shown: str = ""
    stepped: bool = False
    touched_at: float = field(default_factory=time.monotonic)

    def step(self) -> None:
        """A tool ran: the next text is the next model call's."""
        self.stepped = True
        self.touched_at = time.monotonic()

    def add(self, delta: str) -> bool:
        """Take one delta of answer text. Return whether it starts the text of another model call."""
        stepped, self.stepped = self.stepped and bool(self.shown.strip()), False
        if stepped:
            self.shown = after_step(self.shown)
        self.joined += delta
        self.shown += delta
        self.touched_at = time.monotonic()
        return stepped

    def final(self, content: str) -> str:
        """Return the final with its model calls a paragraph apart, or as it came when it is other text."""
        if self.shown == self.joined or content.strip() != self.joined.strip():
            return content
        return self.shown.strip()
