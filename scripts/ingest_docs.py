"""Build the knowledge base from the pages listed in the manifest.

For each page: download it (once), clean the Markdown, split it by heading,
attach metadata, embed the chunks, and store them in Postgres (pgvector).
Safe to run repeatedly: each page's chunks are replaced, and pages no longer in
the manifest are removed.

Usage: python scripts/ingest_docs.py [options]
"""

import argparse
import asyncio
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from backend.config import get_settings
from backend.providers.embeddings import create_embedder
from backend.rag import chunking, corpus
from backend.rag.knowledge import DocChunk, PgKnowledgeBase


def build_chunks(spec: corpus.DocumentSpec, markdown: str) -> list[DocChunk]:
    title, chunks = chunking.split(chunking.clean(markdown))
    return [
        DocChunk(
            chunk_id=f"{spec.document_id}#{position}", document_id=spec.document_id, position=position,
            service=spec.service, category=spec.category, topic=title or spec.file, section=chunk.section,
            source=spec.source, source_url=spec.source_url, source_date=spec.source_date, content=chunk.content,
        )
        for position, chunk in enumerate(chunks)
    ]


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--manifest", type=Path, default=corpus.DEFAULT_MANIFEST,
                        help=f"pages to ingest (default: {corpus.DEFAULT_MANIFEST})")
    parser.add_argument("--corpus-dir", type=Path, default=corpus.DEFAULT_CORPUS_DIR,
                        help=f"where downloaded pages are kept (default: {corpus.DEFAULT_CORPUS_DIR})")
    parser.add_argument("--refetch", action="store_true", help="download every page again, even if already present")
    parser.add_argument("--offline", action="store_true", help="never download; fail if a page is not present")
    parser.add_argument("--recreate", action="store_true",
                        help="drop and recreate the chunk table first (needed after changing EMBEDDING_MODEL "
                             "to a model with a different vector size)")
    parser.add_argument("--dry-run", action="store_true", help="download and chunk only; do not embed or store")
    args = parser.parse_args()

    started = time.monotonic()
    specs = corpus.load_manifest(args.manifest)
    documents: list[tuple[corpus.DocumentSpec, list[DocChunk]]] = []
    downloaded = 0
    with httpx.Client(timeout=30, follow_redirects=True) as client:
        for spec in specs:
            try:
                markdown, fetched = corpus.fetch(spec, args.corpus_dir, client, refetch=args.refetch, offline=args.offline)
            except (httpx.HTTPError, FileNotFoundError) as e:
                print(f"Could not get {spec.document_id}: {e}", file=sys.stderr)
                return 1
            downloaded += fetched
            chunks = build_chunks(spec, markdown)
            if not chunks:
                print(f"Warning: {spec.document_id} produced no chunks", file=sys.stderr)
            documents.append((spec, chunks))

    per_service = Counter()
    for spec, chunks in documents:
        per_service[spec.service] += len(chunks)
    total = sum(per_service.values())
    print(f"{len(documents)} pages ({downloaded} downloaded now), {total} chunks")
    for service, count in sorted(per_service.items()):
        print(f"  {service:<11} {count}")

    if args.dry_run:
        print("Dry run: nothing embedded or stored.")
        return 0

    settings = get_settings()
    if settings.database_url.startswith("memory"):
        print("DATABASE_URL is memory://, so there is no database to ingest into.", file=sys.stderr)
        return 1
    embedder = create_embedder(settings)
    async with AsyncConnectionPool(
            settings.database_url.replace("+psycopg", ""), open=False,
            kwargs={"autocommit": True, "row_factory": dict_row, "prepare_threshold": 0}) as pool:
        knowledge = PgKnowledgeBase(pool, embedder)
        await knowledge.setup(recreate=args.recreate)
        for spec, chunks in documents:
            await knowledge.replace_document(spec.document_id, chunks)
        removed = await knowledge.delete_except([spec.document_id for spec, _ in documents])
        stored = await knowledge.count()

    print(f"Stored {stored} chunks with {embedder.name} ({embedder.dimensions} dimensions); "
          f"removed {removed} stale chunks; {time.monotonic() - started:.1f} s")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
