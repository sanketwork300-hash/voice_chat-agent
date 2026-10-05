import time

from fastapi import FastAPI, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

HTTP_REQUEST_SECONDS = Histogram(
    "http_request_duration_seconds", "HTTP request duration", ["method", "path", "status"])

CHAT_TURNS = Counter("chat_turns_total", "Chat turns handled", ["status"])

LLM_FIRST_TOKEN_SECONDS = Histogram(
    "llm_first_token_seconds", "Time from request to first generated token", ["model"],
    buckets=[0.25, 0.5, 1, 2, 3, 5, 10, 20, 40])

LLM_RESPONSE_SECONDS = Histogram(
    "llm_response_seconds", "Time from request to the end of the reply", ["model"],
    buckets=[1, 2, 5, 10, 20, 40, 80, 160])


TOOL_SECONDS = Histogram(
    "tool_call_seconds", "Tool execution time", ["tool"],
    buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10])

TOOL_CALLS = Counter("tool_calls_total", "Tool calls by outcome", ["tool", "outcome"])

RAG_SECONDS = Histogram(
    "rag_search_seconds", "Knowledge-base search time, including embedding the query",
    buckets=[0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2])

RAG_SEARCHES = Counter("rag_searches_total", "Knowledge-base searches by outcome", ["outcome"])

VOICE_STAGE_SECONDS = Histogram(
    "voice_stage_seconds", "Voice turn latency in seconds: stt (speech end to transcript), first_token and first_audio (from the transcript), total (speech end to end of reply)", ["stage"],
    buckets=[0.1, 0.25, 0.5, 1, 2, 3, 5, 10, 20, 40])

SPEECH_SERVICE_SECONDS = Histogram(
    "speech_service_seconds", "Speech services on a call: time to first byte and processing time per request",
    ["service", "kind"], buckets=[0.05, 0.1, 0.25, 0.5, 1, 2, 3, 5, 10])

VOICE_INTERRUPTIONS = Counter(
    "voice_interruptions_total", "Times a customer spoke while the agent was replying on a call (barge-in)")

ESCALATIONS = Counter("escalations_total", "Conversations handed to a human, by the rule that triggered it", ["trigger"])


def setup_metrics(app: FastAPI) -> None:
    @app.middleware("http")
    async def record_request(request: Request, call_next):
        start = time.perf_counter()
        response = await call_next(request)
        # The route template keeps label cardinality bounded.
        route = request.scope.get("route")
        path = route.path if route else "unmatched"
        HTTP_REQUEST_SECONDS.labels(request.method, path, response.status_code).observe(
            time.perf_counter() - start)
        return response

    @app.get("/metrics", include_in_schema=False)
    def metrics() -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
