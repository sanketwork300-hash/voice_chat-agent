"""Developer-only views of what the agent did. Registered only when DEBUG_UI=1."""

from fastapi import APIRouter, HTTPException, Request

router = APIRouter(prefix="/api/debug")


@router.get("/conversations")
async def conversations(request: Request) -> list[dict]:
    return await request.app.state.store.list_conversations()


@router.get("/conversations/{conversation_id}")
async def conversation(conversation_id: str, request: Request) -> dict:
    state = await request.app.state.agent.get_state(conversation_id)
    if state is None:
        raise HTTPException(404, "No such conversation")
    store = request.app.state.store
    return {
        "state": state.model_dump(mode="json"),
        "audit": await store.audit_trail(conversation_id),
        "escalations": await store.escalations(conversation_id),
    }


@router.get("/escalations")
async def escalations(request: Request, limit: int = 50) -> list[dict]:
    """Conversations handed to a human, newest first, each with what the engineer needs to pick it up."""
    return await request.app.state.store.escalations(limit=limit)
