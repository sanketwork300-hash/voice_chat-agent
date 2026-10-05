"""Stored evaluation runs, for the /evaluation page. Registered only when DEBUG_UI=1."""

from fastapi import APIRouter, HTTPException, Request

router = APIRouter(prefix="/api/evaluation")


@router.get("/runs")
async def runs(request: Request) -> list[dict]:
    return await request.app.state.store.list_evaluations()


@router.get("/runs/{run_id}")
async def run(run_id: str, request: Request) -> dict:
    evaluation = await request.app.state.store.get_evaluation(run_id)
    if evaluation is None:
        raise HTTPException(404, "No such evaluation run")
    return evaluation
