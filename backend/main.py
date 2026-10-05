import asyncio
import logging
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import asdict

import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool, PoolTimeout

from backend import hardware
from backend.agent.agent import SupportAgent, checkpoint_serde
from backend.api import chat, debug, evaluation, voice
from backend.cloud.base import CloudProvider
from backend.cloud.boto3_provider import Boto3CloudProvider
from backend.config import Settings, get_settings
from backend.memory.store import MemoryStore, PostgresStore
from backend.metrics import setup_metrics
from backend.providers.embeddings import create_embedder
from backend.providers.llm import LLMProvider, LLMUnavailable, create_provider, resolve_model
from backend.rag.knowledge import KnowledgeBase, MemoryKnowledgeBase, NoKnowledgeBase, PgKnowledgeBase
from backend.tools.gateway import ToolGateway

log = logging.getLogger("backend")


async def open_storage(settings: Settings, stack: AsyncExitStack):
    """The audit/conversation store and the state checkpointer.

    DATABASE_URL=memory:// keeps both in the process, for tests and for running
    without Postgres; nothing then survives a restart. The third value is the
    connection pool, or None in memory mode.
    """
    if settings.database_url.startswith("memory"):
        return MemoryStore(), InMemorySaver(serde=checkpoint_serde()), None

    pool = AsyncConnectionPool(
        settings.database_url.replace("+psycopg", ""), open=False,
        kwargs={"autocommit": True, "row_factory": dict_row, "prepare_threshold": 0})
    try:
        await pool.open(wait=True, timeout=5)
    except PoolTimeout as e:
        raise RuntimeError(
            "Postgres is not reachable at DATABASE_URL. Start it with `make up`, "
            "or set DATABASE_URL=memory:// to run without it.") from e
    stack.push_async_callback(pool.close)

    store = PostgresStore(pool)
    await store.setup()
    checkpointer = AsyncPostgresSaver(pool, serde=checkpoint_serde())
    await checkpointer.setup()
    return store, checkpointer, pool


async def open_knowledge(settings: Settings, pool) -> KnowledgeBase:
    """The knowledge base. A missing embedding model disables it instead of stopping the backend."""
    try:
        embedder = await asyncio.to_thread(create_embedder, settings)
    except Exception as e:  # the model may be absent with no network; anything raised here is non-fatal
        log.warning("knowledge base disabled: the embedding model could not be loaded (%s)", e)
        return NoKnowledgeBase()
    if pool is None:
        return MemoryKnowledgeBase(embedder)
    return PgKnowledgeBase(pool, embedder)


async def open_voice(settings: Settings, agent: SupportAgent):
    """The voice service, or None when voice is off or its models cannot be loaded."""
    if not settings.voice_enabled:
        return None
    try:
        from backend.voice.service import (
            VoiceService,  # imports Pipecat and the speech models
        )

        service = await VoiceService.load(settings, agent)
    except Exception as e:  # a missing model or library must not stop the chat from working
        log.warning("voice disabled: %s", e)
        return None
    log.info("voice: Whisper %s (%s), Piper voice %s", settings.stt_model, settings.stt_compute_type, settings.tts_voice)
    return service


def create_app(settings: Settings | None = None, llm: LLMProvider | None = None,
               cloud: CloudProvider | None = None, knowledge: KnowledgeBase | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with AsyncExitStack() as stack:
            app.state.llm = llm or create_provider(settings)
            stack.push_async_callback(app.state.llm.aclose)

            hw = hardware.detect()
            loaded_gb = await app.state.llm.loaded_memory_bytes() / 2**30
            selection = hardware.select_models(hw, settings, reclaimable_gb=loaded_gb)
            log.info("hardware: %s", asdict(hw))
            log.info("model tier %s (%s)", selection.tier, selection.reason)
            app.state.hardware = hw
            app.state.selection = selection
            app.state.llm_model = await resolve_model(
                app.state.llm, selection.llm_model, settings.llm_fallback_model)
            log.info("LLM: %s via %s", app.state.llm_model, app.state.llm.name)

            app.state.store, checkpointer, pool = await open_storage(settings, stack)
            app.state.knowledge = knowledge or await open_knowledge(settings, pool)
            chunks = await app.state.knowledge.count()
            log.info("knowledge base: %d chunks%s", chunks, "" if chunks else " (run `make ingest`)")
            gateway = ToolGateway(cloud or Boto3CloudProvider(settings), app.state.store)
            app.state.agent = SupportAgent(
                app.state.llm, app.state.llm_model, gateway, app.state.store, checkpointer, app.state.knowledge,
                rag_top_k=settings.rag_top_k, rag_min_score=settings.rag_min_score)
            app.state.voice = await open_voice(settings, app.state.agent)
            if app.state.voice:
                stack.push_async_callback(app.state.voice.close)
            yield

    app = FastAPI(title="Cloud Support Agent", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware, allow_origins=[settings.frontend_origin], allow_methods=["GET", "POST", "PATCH"],
        allow_headers=["*"])
    setup_metrics(app)
    app.include_router(chat.router)
    app.include_router(voice.router)
    if settings.debug_ui:
        app.include_router(debug.router)
        app.include_router(evaluation.router)

    @app.get("/health")
    async def health(request: Request) -> dict:
        state = request.app.state
        try:
            available = state.llm_model in await state.llm.list_models()
            llm_status = "ok" if available else "model_missing"
        except LLMUnavailable:
            llm_status = "unreachable"
        return {
            "status": "ok" if llm_status == "ok" else "degraded",
            "llm": {"provider": state.llm.name, "model": state.llm_model, "status": llm_status},
            "knowledge_base": {"chunks": await state.knowledge.count()},
            "voice": {"enabled": state.voice is not None},
        }

    @app.get("/api/system")
    def system(request: Request) -> dict:
        state = request.app.state
        return {
            "hardware": asdict(state.hardware),
            "selection": asdict(state.selection),
            "llm_model": state.llm_model,
        }

    return app


app = create_app()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = get_settings()
    uvicorn.run(app, host=settings.backend_host, port=settings.backend_port)
