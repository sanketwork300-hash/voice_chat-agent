"""Voice calls: loads the speech models once and runs one pipeline per browser connection."""

import asyncio
import logging
import os
from pathlib import Path

from faster_whisper import WhisperModel
from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection
from pipecat.transports.smallwebrtc.request_handler import (
    SmallWebRTCPatchRequest, SmallWebRTCRequest, SmallWebRTCRequestHandler,
)
from pipecat.workers.runner import WorkerRunner

from backend.agent.agent import SupportAgent
from backend.config import Settings
from backend.voice.pipeline import build_worker

log = logging.getLogger(__name__)


class VoiceService:
    def __init__(self, settings: Settings, agent: SupportAgent, whisper: WhisperModel):
        self._settings = settings
        self._agent = agent
        self._whisper = whisper
        self._piper_dir = Path(settings.piper_cache_dir).expanduser()
        self._handler = SmallWebRTCRequestHandler()
        self._calls: set[asyncio.Task] = set()

    @classmethod
    async def load(cls, settings: Settings, agent: SupportAgent) -> "VoiceService":
        """Load the Whisper model (a few seconds) off the event loop."""
        threads = settings.stt_threads or max(1, (os.cpu_count() or 2) // 2)
        whisper = await asyncio.to_thread(
            WhisperModel, settings.stt_model, device="cpu", compute_type=settings.stt_compute_type,
            cpu_threads=threads)
        return cls(settings, agent, whisper)

    async def offer(self, request: SmallWebRTCRequest) -> dict:
        """Answer a browser's WebRTC offer and start a pipeline for the connection."""
        async def start(connection: SmallWebRTCConnection) -> None:
            worker, conversation_id = build_worker(
                connection, self._agent, whisper=self._whisper, piper_voice=self._settings.tts_voice,
                piper_dir=self._piper_dir)
            log.info("voice call started: conversation %s", conversation_id)
            task = asyncio.create_task(WorkerRunner(handle_sigint=False).run(worker))
            self._calls.add(task)
            task.add_done_callback(self._calls.discard)

        return await self._handler.handle_web_request(request, start)

    async def ice_candidates(self, request: SmallWebRTCPatchRequest) -> None:
        await self._handler.handle_patch_request(request)

    async def close(self) -> None:
        for task in list(self._calls):
            task.cancel()
        await asyncio.gather(*self._calls, return_exceptions=True)
        await self._handler.close()
