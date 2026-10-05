"""WebRTC signalling for voice calls. The Pipecat JavaScript client posts an SDP offer here."""

from fastapi import APIRouter, HTTPException, Request
from pipecat.transports.smallwebrtc.request_handler import SmallWebRTCPatchRequest, SmallWebRTCRequest

router = APIRouter(prefix="/api/voice")


def _service(request: Request):
    service = getattr(request.app.state, "voice", None)
    if service is None:
        raise HTTPException(503, "Voice is not available: the speech models could not be loaded")
    return service


@router.post("/offer")
async def offer(body: SmallWebRTCRequest, request: Request) -> dict:
    return await _service(request).offer(body)


@router.patch("/offer")
async def ice_candidates(body: SmallWebRTCPatchRequest, request: Request) -> dict:
    await _service(request).ice_candidates(body)
    return {"status": "ok"}
