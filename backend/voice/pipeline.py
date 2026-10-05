"""The voice pipeline: browser audio in, the same support agent, synthesized speech out.

    WebRTC in → Whisper (segmented by VAD) → user-turn aggregator → AgentLanguageStep → Piper → WebRTC out

The agent is not a Pipecat LLM service. `AgentLanguageStep` sits in the slot an
LLM would occupy: it takes each completed user turn, runs `SupportAgent`, and
pushes the reply text downstream for the TTS to speak. Everything the agent
decides (tools, confirmations, escalation) stays in the agent.
"""

import asyncio
import logging
import re
import time
import uuid
from pathlib import Path

from faster_whisper import WhisperModel
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import (
    BotStartedSpeakingFrame, BotStoppedSpeakingFrame, Frame, InterruptionFrame, LLMContextFrame,
    LLMFullResponseEndFrame, LLMFullResponseStartFrame, LLMTextFrame, MetricsFrame, TTSSpeakFrame, TTSTextFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.metrics.metrics import ProcessingMetricsData, TTFBMetricsData
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMAssistantAggregator, LLMUserAggregator, LLMUserAggregatorParams,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.processors.frameworks.rtvi import RTVIObserverParams, RTVIServerMessageFrame
from pipecat.services.piper.tts import PiperTTSService
from pipecat.services.whisper.stt import WhisperSTTService
from pipecat.transcriptions.language import Language
from pipecat.transports.base_transport import TransportParams
from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport
from pipecat.turns.user_start.vad_user_turn_start_strategy import VADUserTurnStartStrategy
from pipecat.turns.user_stop.speech_timeout_user_turn_stop_strategy import SpeechTimeoutUserTurnStopStrategy
from pipecat.turns.user_turn_strategies import UserTurnStrategies

from backend.agent.agent import SupportAgent
from backend.metrics import SPEECH_SERVICE_SECONDS, VOICE_INTERRUPTIONS, VOICE_STAGE_SECONDS
from backend.providers.llm import LLMError

log = logging.getLogger(__name__)

VAD_START_SECS = 0.1         # speech needed before the customer counts as speaking (barge-in reacts after this)
VAD_STOP_SECS = 0.8          # silence that ends the customer's turn
USER_SPEECH_TIMEOUT = 0.5    # extra wait after VAD stop before the turn is closed
TURN_STOP_TIMEOUT = 20.0     # longest wait for the transcript after VAD stop; a slower one is lost as an empty turn
STT_SAMPLE_RATE = 16000
SPOKEN_ERROR = "Sorry, I couldn't process that. Please say it again."
PROTECTED_WAIT_S = 15        # longest a barge-in waits for a confirmed change to finish applying and verifying

# Identifiers that are unpleasant to hear and carry nothing for the listener.
_SECURITY_GROUP = re.compile(r"security group sg-[0-9a-f]+")
_INSTANCE_ID_ASIDE = re.compile(r" \(i-[0-9a-f]+\)")
_HOST_SUFFIX = re.compile(r"(\d{1,3}(?:\.\d{1,3}){3})/32")
_MARKDOWN = re.compile(r"[*_`#]+")
_NAME_SEPARATOR = re.compile(r"(?<=\w)[-_](?=\w)")  # demo-web-01 is read more reliably as "demo web 01"
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+|\n+")
_CITATION = re.compile(r"\s*\[\d+(?:,\s*\d+)*\]")


def spoken(text: str) -> str:
    """Make a reply fit for speech: no markup, no raw identifiers that only matter on screen."""
    text = _MARKDOWN.sub("", text)
    text = _CITATION.sub("", text)  # the page still shows the sources; "[1]" is not something to say
    text = _SECURITY_GROUP.sub("the instance's security group", text)
    text = _INSTANCE_ID_ASIDE.sub("", text)
    text = _HOST_SUFFIX.sub(r"\1", text)
    text = _NAME_SEPARATOR.sub(" ", text)
    return text.replace("TCP port", "port")


class SharedWhisperSTTService(WhisperSTTService):
    """Whisper STT that reuses one loaded model for every call instead of loading per connection."""

    def __init__(self, model: WhisperModel, **kwargs):
        self._shared_model = model  # before the base class, whose __init__ calls _load()
        super().__init__(**kwargs)

    def _load(self):
        self._model = self._shared_model


class TurnTiming:
    """Timestamps of one voice turn, from the end of the customer's speech to the end of the reply."""

    def __init__(self) -> None:
        self.user_stopped: float | None = None
        self.transcript: float | None = None
        self.first_token: float | None = None
        self.first_audio: float | None = None

    def stages(self, now: float) -> dict[str, float]:
        """Seconds per stage; a stage is left out when its start was not seen."""
        out: dict[str, float] = {}
        if self.user_stopped and self.transcript:
            out["stt"] = self.transcript - self.user_stopped
        if self.transcript and self.first_token:
            out["first_token"] = self.first_token - self.transcript
        if self.transcript and self.first_audio:
            # From the transcript too: the holding phrase can be heard before the first model token.
            out["first_audio"] = self.first_audio - self.transcript
        if self.user_stopped:
            out["total"] = now - self.user_stopped
        return {k: round(v, 3) for k, v in out.items()}


class AgentLanguageStep(FrameProcessor):
    """Runs the support agent on each completed user turn and speaks its reply."""

    def __init__(self, agent: SupportAgent, conversation_id: str, **kwargs):
        super().__init__(**kwargs)
        self._agent = agent
        self._conversation_id = conversation_id
        self._timing = TurnTiming()
        self._response_task = None
        self._responding = False  # the agent is still producing this turn's reply
        self._protected = False   # the agent is applying a confirmed change: never cancelled mid-way
        # The turn has been heard in full when as many characters have been played as were pushed to the TTS.
        # Speaking spans are no guide: the TTS closes a span whenever the model pauses for a few seconds.
        self._pushed_chars = 0
        self._played_chars = 0

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, VADUserStoppedSpeakingFrame):
            self._timing.user_stopped = time.monotonic()
        elif isinstance(frame, BotStartedSpeakingFrame):
            if self._timing.first_audio is None:
                self._timing.first_audio = time.monotonic()
        elif isinstance(frame, InterruptionFrame):
            await self._interrupted()

        if isinstance(frame, LLMContextFrame):
            text = _last_user_message(frame.context)
            if text:
                await self._cancel_response()
                self._timing, previous = TurnTiming(), self._timing  # an unreported turn is dropped here
                self._timing.user_stopped, self._timing.transcript = previous.user_stopped, time.monotonic()
                self._pushed_chars = self._played_chars = 0
                self._response_task = self.create_task(self._respond(text))
            return  # the context is not needed downstream; the agent keeps its own history
        await self.push_frame(frame, direction)

    def text_played(self, text: str) -> None:
        """Called by the tracker after the transport as each piece of reply text is played."""
        self._played_chars += _chars(text)

    async def bot_stopped(self) -> dict | None:
        """Called by the tracker when the bot stops speaking. Returns the finished turn's stages, if it is over."""
        if self._responding or self._played_chars < self._pushed_chars:
            return None
        return self._report_turn()

    async def _respond(self, text: str) -> None:
        self._responding = True
        await self.push_frame(LLMFullResponseStartFrame())
        pending = ""  # tokens are cleaned a sentence at a time, since a marker like "[1]" can span tokens
        try:
            async for event in self._agent.handle_turn(self._conversation_id, channel="voice", text=text):
                if event["type"] == "token":
                    if self._timing.first_token is None:
                        self._timing.first_token = time.monotonic()
                    pending += event["text"]
                    *sentences, pending = _SENTENCE_END.split(pending)
                    for sentence in sentences:
                        await self._speak(sentence)
                elif event["type"] == "interim":
                    # "Let me check web-01." while the slow part runs. A TTSSpeakFrame plays at once; the
                    # sentence aggregator would hold a lone sentence until more text came.
                    phrase = spoken(event["text"])
                    self._pushed_chars += _chars(phrase)
                    await self.push_frame(TTSSpeakFrame(phrase))
                else:
                    if event["type"] == "state":
                        self._protected = event["status"] in ("EXECUTING", "VERIFYING")
                    # Checks, sources, pending changes, and status go to the page as data, not speech.
                    await self.push_frame(RTVIServerMessageFrame(data={"agent": event}))
            await self._speak(pending)
        except LLMError as e:
            log.warning("voice turn failed (conversation %s): %s", self._conversation_id, e)
            await self._speak(SPOKEN_ERROR)
        except asyncio.CancelledError:
            self._responding = self._protected = False  # interrupted: Pipecat has already cleared the TTS
            raise
        finally:
            await self.push_frame(LLMFullResponseEndFrame())
            if self._responding:
                self._responding = self._protected = False
                if self._pushed_chars == 0:  # nothing to play, so the bot will not stop speaking later
                    stages = self._report_turn()
                    if stages:
                        await self.push_frame(RTVIServerMessageFrame(data={"latency": stages}))

    async def _speak(self, sentence: str) -> None:
        """Push one cleaned sentence of the reply to the TTS."""
        sentence = spoken(sentence).strip()
        if sentence:
            self._pushed_chars += _chars(sentence)
            await self.push_frame(LLMTextFrame(sentence + " "))

    async def _interrupted(self) -> None:
        """The customer spoke over the agent: Pipecat has stopped playback; stop the agent's work too."""
        if self._responding or self._played_chars < self._pushed_chars:
            VOICE_INTERRUPTIONS.inc()
            log.info("voice turn %s interrupted by the customer", self._conversation_id)
        self._timing = TurnTiming()  # the interrupted turn is not reported
        self._pushed_chars = self._played_chars = 0
        await self._cancel_response()

    async def _cancel_response(self) -> None:
        if self._response_task and not self._response_task.done():
            if self._protected:
                # A confirmed change is being applied and verified; it finishes (a few seconds at most).
                await asyncio.wait_for(asyncio.shield(self._response_task), timeout=PROTECTED_WAIT_S)
            else:
                await self.cancel_task(self._response_task)
        self._response_task = None

    def _report_turn(self) -> dict | None:
        """Record the finished turn's stage latencies; the caller shows them on the page."""
        stages = self._timing.stages(time.monotonic())
        self._timing = TurnTiming()
        if not stages:
            return None
        for stage, seconds in stages.items():
            VOICE_STAGE_SECONDS.labels(stage).observe(seconds)
        log.info("voice turn %s: %s", self._conversation_id, stages)
        return stages


class PlaybackTracker(FrameProcessor):
    """Sits after the transport, where text frames arrive as their audio is played, and reports back to the step."""

    def __init__(self, step: AgentLanguageStep, **kwargs):
        super().__init__(**kwargs)
        self._step = step

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, TTSTextFrame):
            self._step.text_played(frame.text)
        elif isinstance(frame, BotStoppedSpeakingFrame):
            stages = await self._step.bot_stopped()
            if stages:
                await self.push_frame(RTVIServerMessageFrame(data={"latency": stages}))
        elif isinstance(frame, MetricsFrame):
            _export_speech_metrics(frame)
        await self.push_frame(frame, direction)


_SPEECH_SERVICES = {"SharedWhisperSTTService": "stt", "PiperTTSService": "tts"}


def _export_speech_metrics(frame: MetricsFrame) -> None:
    """Pipecat's per-request timings for the speech services, as Prometheus histograms."""
    for item in frame.data:
        service = _SPEECH_SERVICES.get(item.processor.split("#")[0])
        if service is None:
            continue
        if isinstance(item, TTFBMetricsData):
            SPEECH_SERVICE_SECONDS.labels(service, "ttfb").observe(item.value)
        elif isinstance(item, ProcessingMetricsData):
            SPEECH_SERVICE_SECONDS.labels(service, "processing").observe(item.value)


def _chars(text: str) -> int:
    """Characters that count as spoken text; whitespace differs between what was pushed and what was played."""
    return sum(1 for c in text if not c.isspace())


def _last_user_message(context: LLMContext) -> str:
    for message in reversed(context.messages):
        if message.get("role") == "user":
            content = message.get("content")
            if isinstance(content, list):
                content = " ".join(part.get("text", "") for part in content if isinstance(part, dict))
            return (content or "").strip()
    return ""


def build_worker(connection: SmallWebRTCConnection, agent: SupportAgent, *, whisper: WhisperModel,
                 piper_voice: str, piper_dir: Path, conversation_id: str | None = None,
                 allow_interruptions: bool = True) -> tuple[PipelineWorker, str]:
    """Assemble the pipeline for one browser connection. Returns the worker and the conversation id."""
    conversation_id = conversation_id or uuid.uuid4().hex
    transport = SmallWebRTCTransport(
        connection, TransportParams(audio_in_enabled=True, audio_out_enabled=True))

    stt = SharedWhisperSTTService(
        whisper, settings=WhisperSTTService.Settings(model="small", language=Language.EN, no_speech_prob=0.4))
    tts = PiperTTSService(settings=PiperTTSService.Settings(voice=piper_voice), download_dir=piper_dir)

    context = LLMContext()
    user_turns = LLMUserAggregator(context, params=LLMUserAggregatorParams(
        vad_analyzer=SileroVADAnalyzer(params=VADParams(start_secs=VAD_START_SECS, stop_secs=VAD_STOP_SECS)),
        user_turn_stop_timeout=TURN_STOP_TIMEOUT,
        empty_user_turn=None,  # a turn with no transcript (a cough) gets no reply; Pipecat's recovery prompt is for LLMs
        user_turn_strategies=UserTurnStrategies(
            start=[VADUserTurnStartStrategy(enable_interruptions=allow_interruptions)],
            stop=[SpeechTimeoutUserTurnStopStrategy(user_speech_timeout=USER_SPEECH_TIMEOUT)]),
    ))
    step = AgentLanguageStep(agent, conversation_id)

    async def announce(worker):
        await worker.queue_frame(RTVIServerMessageFrame(data={"conversation_id": conversation_id}))

    pipeline = Pipeline([
        transport.input(), stt, user_turns, step, tts, transport.output(), PlaybackTracker(step),
        LLMAssistantAggregator(context),
    ])
    worker = PipelineWorker(
        pipeline,
        params=PipelineParams(audio_in_sample_rate=STT_SAMPLE_RATE, enable_metrics=True),
        rtvi_observer_params=RTVIObserverParams(metrics_enabled=True),
        conversation_id=conversation_id,
        cancel_on_idle_timeout=False,
    )

    @transport.event_handler("on_client_connected")
    async def on_connected(_transport, _client):
        await announce(worker)

    @transport.event_handler("on_client_disconnected")
    async def on_disconnected(_transport, _client):
        await worker.cancel()

    return worker, conversation_id
