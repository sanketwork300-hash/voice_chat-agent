from typing import Protocol


class Embedder(Protocol):
    name: str
    dimensions: int

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Unit-length vectors for passages to be stored."""
        ...

    def embed_query(self, text: str) -> list[float]:
        """Unit-length vector for a search query."""
        ...
