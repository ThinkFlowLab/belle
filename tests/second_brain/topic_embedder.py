"""A deterministic embedder for the corpus index tests."""

import re

from belle_agent.second_brain.backend import Embedder


class TopicEmbedder:
    """Embeds a text as its count of words from each of a few topics: a meaning with no shared word.

    It records what it was asked to embed, and fails while ``failing`` is set.
    """

    TOPICS = (
        {"lighthouse", "lens", "fresnel", "keeper", "keepers", "lamp", "beacon", "mantle"},
        {"kite", "bridle", "tow", "wind", "stall", "stalls", "glider"},
        {"tide", "tides", "estuary", "slipway", "ferry", "harbour", "flood", "floods"},
        {"hive", "brood", "varroa", "mites", "bees", "beekeeper", "frames"},
        {"starter", "rye", "dough", "hydration", "bread", "loaf"},
    )

    def __init__(self, model: str = "topics-v1") -> None:
        self.model = model
        self.documents: list[str] = []
        self.queries: list[str] = []
        self.failing = False

    def vector(self, text: str) -> list[float]:
        words = re.findall(r"[a-z]+", text.lower())
        # The last dimension keeps a text with no topic word off the zero vector.
        return [float(sum(word in topic for word in words)) for topic in self.TOPICS] + [0.01]

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if self.failing:
            raise RuntimeError("embedding endpoint unreachable")
        self.documents.extend(texts)
        return [self.vector(text) for text in texts]

    async def embed_query(self, text: str) -> list[float]:
        if self.failing:
            raise RuntimeError("embedding endpoint unreachable")
        self.queries.append(text)
        return self.vector(text)

    def embedder(self) -> Embedder:
        return Embedder(model=self.model, embed_documents=self.embed_documents, embed_query=self.embed_query)
