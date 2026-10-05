"""The knowledge base: documentation chunks with embeddings, and similarity search over them."""

import asyncio
import logging
import re
from datetime import date
from typing import Protocol

from psycopg import errors
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel

from backend.providers.embeddings import Embedder

log = logging.getLogger(__name__)

_COLUMNS = ("chunk_id, document_id, position, service, category, topic, section, source, source_url, "
            "source_date, content")


class DocChunk(BaseModel):
    chunk_id: str
    document_id: str
    position: int
    service: str
    category: str
    topic: str    # page title
    section: str  # heading path within the page
    source: str
    source_url: str
    source_date: date
    content: str

    def embedding_text(self) -> str:
        return f"{self.section}\n\n{self.content}"


class Retrieved(DocChunk):
    score: float  # cosine similarity to the query, 0..1 for the models used here


class KnowledgeBase(Protocol):
    async def search(self, query: str, *, service: str | None, k: int, min_score: float) -> list[Retrieved]:
        """The most similar chunks scoring at least `min_score`, best first. Empty when nothing is relevant."""
        ...

    async def count(self) -> int: ...


def _vector(values: list[float]) -> str:
    return "[" + ",".join(f"{v:.7f}" for v in values) + "]"


async def _search_with_fallback(search_once, service: str | None) -> list[Retrieved]:
    """Prefer chunks of the conversation's service; widen to all services if none of them is relevant."""
    if service:
        found = await search_once(service)
        if found:
            return found
    return await search_once(None)


class PgKnowledgeBase:
    def __init__(self, pool: AsyncConnectionPool, embedder: Embedder, table: str = "document_chunks"):
        if not re.fullmatch(r"[a-z_][a-z0-9_]*", table):
            raise ValueError(f"invalid table name {table!r}")
        self._pool = pool
        self._embedder = embedder
        self._table = table  # interpolated into SQL below; validated above

    async def setup(self, *, recreate: bool = False) -> None:
        async with self._pool.connection() as conn:
            await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
            if recreate:
                await conn.execute(f"DROP TABLE IF EXISTS {self._table}")
            await conn.execute(f"""
                CREATE TABLE IF NOT EXISTS {self._table} (
                    chunk_id        TEXT PRIMARY KEY,
                    document_id     TEXT NOT NULL,
                    position        INT NOT NULL,
                    service         TEXT NOT NULL,
                    category        TEXT NOT NULL,
                    topic           TEXT NOT NULL,
                    section         TEXT NOT NULL,
                    source          TEXT NOT NULL,
                    source_url      TEXT NOT NULL,
                    source_date     DATE NOT NULL,
                    content         TEXT NOT NULL,
                    embedding_model TEXT NOT NULL,
                    embedding       vector({self._embedder.dimensions}) NOT NULL
                )""")
            await conn.execute(f"CREATE INDEX IF NOT EXISTS {self._table}_service ON {self._table} (service)")
            await conn.execute(f"CREATE INDEX IF NOT EXISTS {self._table}_document ON {self._table} (document_id)")
            await conn.execute(
                f"CREATE INDEX IF NOT EXISTS {self._table}_embedding ON {self._table} "
                "USING hnsw (embedding vector_cosine_ops)")

    async def replace_document(self, document_id: str, chunks: list[DocChunk]) -> None:
        """Store a page's chunks, replacing whatever was stored for it before."""
        vectors = await asyncio.to_thread(
            self._embedder.embed_documents, [c.embedding_text() for c in chunks])
        async with self._pool.connection() as conn, conn.transaction():
            await conn.execute(f"DELETE FROM {self._table} WHERE document_id = %s", (document_id,))
            for chunk, vector in zip(chunks, vectors):
                await conn.execute(
                    f"INSERT INTO {self._table} ({_COLUMNS}, embedding_model, embedding) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::vector)",
                    (chunk.chunk_id, chunk.document_id, chunk.position, chunk.service, chunk.category,
                     chunk.topic, chunk.section, chunk.source, chunk.source_url, chunk.source_date,
                     chunk.content, self._embedder.name, _vector(vector)))

    async def delete_except(self, document_ids: list[str]) -> int:
        """Remove pages that are no longer listed. Returns the number of chunks removed."""
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                f"DELETE FROM {self._table} WHERE NOT (document_id = ANY(%s))", (document_ids,))
            return cursor.rowcount

    async def count(self) -> int:
        try:
            async with self._pool.connection() as conn:
                cursor = await conn.execute(
                    f"SELECT count(*) AS n FROM {self._table} WHERE embedding_model = %s", (self._embedder.name,))
                return (await cursor.fetchone())["n"]
        except errors.UndefinedTable:
            return 0

    async def search(self, query: str, *, service: str | None, k: int, min_score: float) -> list[Retrieved]:
        vector = _vector(await asyncio.to_thread(self._embedder.embed_query, query))

        async def search_once(only_service: str | None) -> list[Retrieved]:
            where = "embedding_model = %(model)s" + (" AND service = %(service)s" if only_service else "")
            async with self._pool.connection() as conn:
                cursor = await conn.execute(
                    f"SELECT {_COLUMNS}, 1 - (embedding <=> %(q)s::vector) AS score FROM {self._table} "
                    f"WHERE {where} ORDER BY embedding <=> %(q)s::vector LIMIT %(k)s",
                    {"q": vector, "model": self._embedder.name, "service": only_service, "k": k})
                rows = await cursor.fetchall()
            return [Retrieved(**row) for row in rows if row["score"] >= min_score]

        try:
            return await _search_with_fallback(search_once, service)
        except errors.UndefinedTable:
            log.warning("the knowledge base has not been ingested yet (run `make ingest`)")
            return []


class MemoryKnowledgeBase:
    """Chunks held in the process, for tests and for running without Postgres."""

    def __init__(self, embedder: Embedder):
        self._embedder = embedder
        self._entries: list[tuple[DocChunk, list[float]]] = []

    def add(self, chunks: list[DocChunk]) -> None:
        vectors = self._embedder.embed_documents([c.embedding_text() for c in chunks])
        self._entries.extend(zip(chunks, vectors))

    async def count(self) -> int:
        return len(self._entries)

    async def search(self, query: str, *, service: str | None, k: int, min_score: float) -> list[Retrieved]:
        query_vector = self._embedder.embed_query(query)

        async def search_once(only_service: str | None) -> list[Retrieved]:
            scored = [
                Retrieved(**chunk.model_dump(), score=sum(a * b for a, b in zip(query_vector, vector)))
                for chunk, vector in self._entries
                if only_service is None or chunk.service == only_service
            ]
            scored.sort(key=lambda r: r.score, reverse=True)
            return [r for r in scored[:k] if r.score >= min_score]

        return await _search_with_fallback(search_once, service)


class NoKnowledgeBase:
    """Used when the embedding model cannot be loaded: every search finds nothing."""

    async def count(self) -> int:
        return 0

    async def search(self, query: str, *, service: str | None, k: int, min_score: float) -> list[Retrieved]:
        return []
