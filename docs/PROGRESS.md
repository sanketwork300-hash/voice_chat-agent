# Progress log

What has been built so far, what is left, and every flag that controls the system. Updated at the end of each milestone. The specification is [SPEC.md](../SPEC.md); how to run things is in the [README](../README.md).

## Status

| # | Milestone | Status |
|---|---|---|
| 0 | Environment, models, FakeCloud fidelity probe | Done |
| 1 | Backend, Ollama provider, streaming chat page | Done |
| 2 | Tools, gateway, state machine, confirmed fixes, debug page | Done |
| 3 | Knowledge base: ingestion and retrieval with citations | Done |
| 4 | Lambda, IAM, CloudWatch tools and root causes | Done |
| 5 | Scenario generator, fixtures, simulated customer, evaluation | Done |
| 6 | Voice, turn-based (VAD, STT, agent, TTS) | Done |
| 7 | Streaming voice, barge-in, spoken confirmation | Done |
| 8 | Escalation flow, full debug page, Grafana, full evaluation, README | Done |

## Change log

### Milestone 0 — environment (done)

Built:

- Python 3.12 environment managed by `uv`; Node 24 LTS through `fnm`, both in user space.
- Ollama models `qwen3:4b-instruct` and `qwen3:1.7b` pulled.
- `docker-compose.yml` with Postgres 17 + pgvector and FakeCloud 0.47.0 (image pinned by digest, IAM enforcement on).
- `backend/hardware.py`: hardware detection and model-tier selection.
- `scripts/probe_fakecloud.py`: seeds each root cause in FakeCloud and reads it back.

Decisions:

- The Docker socket is not mounted into FakeCloud, so it runs as a control plane only.
- RDS was dropped: FakeCloud cannot create database instances without the socket. Its 30 scenarios were redistributed (60 EC2, 35 S3, 35 Lambda, 25 IAM, 25 CloudWatch, 20 out-of-domain).
- EC2 status-check failure is observable only through a seeded `StatusCheckFailed` metric.

Verified: `make probe` passes 24 of 25 checks (the failure is the native status check above); `make hardware` reports the "standard" tier on this machine.

### Milestone 1 — streaming chat (done)

Built:

- FastAPI backend on port 8010: WebSocket chat with token streaming, `/health`, `/api/system`, `/metrics`.
- LLM provider abstraction with Ollama and OpenAI-compatible implementations.
- Next.js chat page with streaming replies, connection status, and a clear button.

Decisions:

- The `wassim249` backend template was not adopted as the code base (it is built around JWT sessions, hosted models, and services the spec excludes); it is used as a reference.
- The lightweight-tier threshold was lowered from 6 GB to 5 GB free RAM, and memory already held by loaded Ollama models counts as free.
- Port 8010 is used because 8000 is taken on this machine.

Verified: a typed question streams an answer in headless Chrome; the metrics endpoint reports the turn.

### Milestone 2 — tools, state machine, confirmed fixes (done)

Built:

- `backend/cloud/`: `CloudProvider` protocol and one boto3 implementation (FakeCloud or real AWS, chosen by `AWS_ENDPOINT_URL`).
- `backend/tools/`: catalog of 12 read tools and 4 write tools for EC2 and S3, and the gateway that validates, executes, and audits every call.
- `backend/agent/`: the LangGraph state machine, taxonomy tables (intents, slots, root causes, resolutions), confirmation matcher, pattern extractors, prompts.
- `backend/memory/store.py`: conversation index and audit log in Postgres; conversation state in the LangGraph Postgres checkpointer.
- `backend/security/redaction.py`: secret redaction for tool results and streamed replies.
- Chat page: activity lines per check, troubleshooting status, Confirm / Cancel card. Debug page at `/debug`.
- `scripts/seed_fakecloud.py` and `backend/evaluation/fixtures.py`: the demo environment.

Decisions (departures from the first draft of the spec, all recorded in SPEC.md):

- The model never passes resource identifiers; the gateway fills them in from the conversation. A 4B model garbled a long instance ID in testing.
- Root causes are concluded only when a check in code finds supporting tool results. The model breaks ties when several causes are supported.
- Each intent lists required checks; the agent runs the ones the model skipped.
- The fix proposal is built in code, not written by the model.
- The agent asks which IP address to allow before proposing an SSH or RDP rule.

Verified: the SSH scenario completes in headless Chrome with Confirm (rule added and verified in FakeCloud) and with Cancel (FakeCloud unchanged); 101 backend tests pass, plus 3 opt-in tests against FakeCloud.

### Milestone 3 — knowledge base (done)

Built:

- `datasets/knowledge/manifest.yaml`: 66 pages from six archived `awsdocs` repositories, each pinned to a commit; `LICENSES.md` records each repository's license statement.
- `backend/rag/`: Markdown cleaning and heading-based chunking (`chunking.py`), manifest loading and download (`corpus.py`), pgvector storage and similarity search (`knowledge.py`).
- `backend/providers/embeddings/`: local embedding provider (fastembed, `BAAI/bge-small-en-v1.5`, 384 dimensions).
- `scripts/ingest_docs.py`: download, clean, chunk, embed, store. 912 chunks; a rerun replaces rather than duplicates.
- Agent: documentation questions are answered from retrieved excerpts with citations; problems on services without tools get guidance from the documentation; a diagnosis lists related pages.
- Chat page: "Searching the documentation" activity and a Sources list with links. Debug page: retrieved documents with scores. `/health` reports the chunk count. Metrics `rag_search_seconds` and `rag_searches_total`.

Decisions:

- The `awsdocs` repositories deleted their content in their last commit, so each is pinned to the commit before it. Sources link to that exact file on GitHub.
- "No documentation" has two checks: a similarity threshold of 0.74, and a rule that an answer reaches the customer only once it cites a retrieved excerpt. The threshold alone does not separate covered from uncovered questions cleanly.
- Embeddings run through fastembed (ONNX, 67 MB) instead of PyTorch. No reranker was added.
- The IAM repository states no license for its documentation text; this is flagged in `LICENSES.md`.

Verified: in headless Chrome, "What are the rules for naming an S3 bucket?" returned an answer citing the bucket-naming page with a working source link, and "How do I create a DynamoDB table?" returned the no-documentation reply. The SSH scenario still passes and now lists related documentation. 116 backend tests pass, plus 3 opt-in tests against FakeCloud.

### Milestone 4 — Lambda, IAM, CloudWatch (done)

Built:

- `backend/tools/lambda_.py`, `iam.py`, `cloudwatch.py`: 13 read tools and the write tool `lambda_update_timeout`. The catalog now has 25 read tools and 5 write tools.
- Cloud layer: functions, log events, IAM users, roles, policies and policy simulation, alarms and alarm history.
- Taxonomy: 7 intents with required details and required checks, 11 root causes, 11 resolutions.
- Agent: the Lambda function, alarm name, IAM user or role, IAM action, and new timeout are collected as details; a named user or role is confirmed to exist (as a user, then as a role) before diagnosis; naming a different resource discards the earlier diagnosis.
- Fixtures and demo seed for the three services; 3 more opt-in tests against FakeCloud (6 in total).

Decisions:

- `cloudwatch_get_metric` reads the alarm's own metric and `iam_get_policy` returns all policies, so the model supplies no identifiers.
- `IAM_WRONG_ROLE` is recognised when the role in use lacks the permission and another role has it.
- Added `CW_ALARM_NOT_FOUND` and `CW_ALARM_OK`; dropped `UNSUPPORTED` as a root cause.
- Required checks stop after a "not found", since the remaining ones could only repeat it.
- A question that names one of the customer's own resources is treated as a problem to investigate, not a documentation question.
- The classifier is asked for single-line JSON, which shortens its output by about a second. Making its detail fields optional was tried and rejected: it was faster but dropped real details.

Verified: in headless Chrome with the real model, one scenario per service: the Lambda timeout was diagnosed, changed from 3 to 30 seconds after Confirm, and verified in FakeCloud; the IAM explicit deny and the CloudWatch alarm in ALARM were diagnosed with the correct evidence. Eight more scenarios passed when run directly against the agent (Lambda code error, Lambda role permission, missing function, IAM missing permission, wrong role, unknown user corrected by the customer, alarm with insufficient data, alarm in OK). 135 backend tests pass, plus 6 opt-in tests against FakeCloud.

Known issue: once during development a seeded alarm stayed in INSUFFICIENT_DATA after its datapoint was published. It did not reproduce in about 20 further attempts. The alarm fixture now checks the state and republishes, and fails loudly if the alarm still has not evaluated.

### Milestone 5 — scenarios and evaluation (done)

Built:

- `backend/evaluation/generator.py` and `scripts/generate_scenarios.py`: 200 scenarios from 26 templates plus an out-of-domain group, and 13 scripted multi-turn conversations, all from a fixed seed. The files are committed under `datasets/`.
- `backend/evaluation/fixtures.py`: named fixture builders with handles (`{vm.instance_id}`), and post-condition checks that read FakeCloud directly.
- `backend/evaluation/simulator.py`: the deterministic simulated customer, and a cloud wrapper that records every change.
- `backend/evaluation/scoring.py`: the checks and the run summary. `backend/evaluation/runner.py` and `scripts/run_evaluation.py`: reset, build fixture, converse, check post-condition, score, store.
- Postgres tables `evaluation_runs` and `evaluation_results`; API under `/api/evaluation`; the `/evaluation` page with a summary, accuracy per check, results per service, and a per-scenario drill-down.

Decisions:

- The simulated customer uses no model: it reads the agent's state to decide what to say.
- Tool selection is reported for the model alone and after the agent's required checks; only the latter decides a pass.
- Write safety is measured at the cloud provider and compared with what the simulated customer confirmed.
- Scenarios accept more than one intent or root cause where the phrasing or the fixture genuinely allows it.

Verified: `make evaluate-smoke` completed and stored run `20261001-115730-0caa0f`: 20 of 20 scenarios passed, 0 write-safety violations, 9.2 minutes. The 13 scripted conversations were then run: 11 passed on the first run, with 0 violations. The two failures were one scoring flaw (looking up a wrong name the customer gave was counted as a wrong tool argument) and one agent bug (in "My alarm shows INSUFFICIENT_DATA" the state name was taken as the alarm's name). Both were fixed and covered by tests. The rerun (`20261001-121452-f5ad54`) passed 13 of 13 with 0 violations in 6.3 minutes. 165 backend tests pass, plus 7 opt-in tests against FakeCloud.

Not done: the full 200-scenario run. It is planned for Milestone 8 and is expected to take close to two hours. The smoke set is mostly first-of-its-kind scenarios with all details given, so its 20 of 20 does not predict the full set.

### Milestone 6 — voice, turn-based (done)

Built:

- `backend/voice/pipeline.py`: one Pipecat pipeline per browser connection: SmallWebRTC transport → shared faster-whisper (`small`, int8) → `LLMUserAggregator` with Silero VAD → `AgentLanguageStep` → Piper → transport. `AgentLanguageStep` runs the same `SupportAgent` with `channel="voice"`, speaks the reply sentence by sentence through `spoken()` (no markdown, citation markers, instance IDs in parentheses, security group IDs, or "/32"), says a holding phrase once when checks start, forwards the agent's other events to the page as RTVI server messages, and records per-stage latency.
- `backend/voice/service.py`: loads Whisper once at startup, answers WebRTC offers, runs one `WorkerRunner` per call, closes calls on shutdown. `backend/api/voice.py`: `POST` and `PATCH /api/voice/offer`. `/health` reports `voice.enabled`.
- `backend/metrics.py`: `voice_stage_seconds{stage}` histogram (`stt`, `first_token`, `first_audio`, `total`).
- `backend/agent/extract.py`: `spoken_key()`; instance names now match by letters and digits with spelled-out numbers, so "demo web zero one" finds `demo-web-01` (used by chat too).
- Frontend: `/voice` page (`app/voice/page.tsx`, `lib/useVoiceClient.ts`) on `@pipecat-ai/client-js` + `@pipecat-ai/small-webrtc-transport`: Start call / End call, states Not in a call / Connecting / Listening / Working on it / Speaking / Problem, transcript of both sides, the chat's check, source, and proposed-change panels, latency line in debug mode. The chat header links to it.
- Dependencies: `pipecat-ai[silero,whisper,webrtc]` 1.12.0, `piper-tts` 1.8.0 (GPL-3.0; recorded in the README's upstream table), onnxruntime pinned down to 1.24.4 by piper. 4 new unit tests (`tests/unit/test_voice.py`).

Decisions:

- The VAD runs inside Pipecat's user aggregator (the 1.12 design); the end of a turn is 0.8 s of silence plus a 0.5 s wait, with interruptions disabled until Milestone 7. The default smart-turn stop strategy was not used because it downloads a model.
- The holding phrase is a `TTSSpeakFrame`, since the sentence aggregator would otherwise hold it until the reply began. A turn is reported when its last pending utterance has played.
- `first_audio` is measured from the transcript rather than from the first token, because the holding phrase is heard first.
- Spoken "yes"/"no" already confirm or decline through the existing matcher; the proposed-change card on the voice page has no buttons.
- Whisper's domain prompt (`initial_prompt`, `hotwords`) made no difference on the test clips and is not set.

Verified: in headless Chrome with Chrome's fake microphone playing Piper-synthesized speech and the real model, "What is the maximum timeout I can set on a Lambda function?" was transcribed, answered with a citation, and spoken (transcribe 1.9 s, first word 9.4 s, total 15.4 s). The four-turn SSH scenario completed by voice: problem, "It's called demo web zero one", the IP address, "Yes, please go ahead", ending Resolved with the rule verified in FakeCloud; the holding phrase was heard 8 s after the transcript while checks ran. Latencies appear in `voice_stage_seconds` and under each reply. 169 backend tests pass, plus 7 opt-in tests against FakeCloud; frontend lint and type check pass.

Measured against the spec's targets: STT 1.5–1.9 s (target 1.5 s); first token 7 s on classification-only turns and 18–20 s on turns that run checks (target 3 s), dominated by the 4B model as in chat; first audio 7–9 s on those turns because the holding phrase waits for classification; Piper's first audio comes 0.3 s after its sentence (target 1 s).

Known limits: no barge-in and no streaming TTS (the reply is synthesized once its text is complete, so long replies start late); a synthetic voice saying "ssh" as a word is transcribed as "a cessation", while spoken letters are transcribed correctly; Whisper sometimes writes "ECE2" for EC2.

### Milestone 7 — streaming voice, barge-in, spoken confirmation (done)

Built:

- Barge-in: interruptions enabled on the user-turn start strategy; on `InterruptionFrame` the step cancels the agent's in-flight turn (model calls included) unless a confirmed change is being applied or verified, in which case the new turn waits for it (`PROTECTED_WAIT_S`). Counter `voice_interruptions_total`. The page marks the cut-off reply "— interrupted" and returns to Listening.
- Agent `interim` events (voice only): "Let me check {name}." at the start of the checks (reading the resolved identifier back), "Let me look that up." for documentation questions, "Applying that now." for a confirmed change. Spoken at once as `TTSSpeakFrame`s; they replace Milestone 6's fixed holding phrase. Chat receives none of them.
- Spoken confirmation rule: a reply to a pending change that is neither yes/no nor a question is re-asked once with the proposal and "please say yes or no"; a second one withdraws the proposal ("I couldn't tell whether that was a yes, so I've left it."). State field `confirmation_reasks`; audit `action_declined` with `via: unclear`.
- End of a voice turn now detected by the characters played (`PlaybackTracker` after the transport counts `TTSTextFrame`s) against the characters pushed, because the TTS splits one reply into several speaking spans when the model pauses.
- **Fix from Milestone 6:** the voice page now plays the agent's audio track (`onTrackStarted` → audio element). The SmallWebRTC client transport does not do this itself, so in Milestone 6 the audio reached the browser but was not audible.
- Hardening found by the barge-in tests: Pipecat's `user_turn_stop_timeout` raised from 5 s to 20 s (a slow transcription was discarded as an empty turn); empty-turn recovery disabled; Whisper CPU threads capped at half the cores (`STT_THREADS`) so a transcription cannot starve the audio loop (the browser's receive jitter buffer fell from 0.5 s to about 0.1 s); VAD start window 0.1 s; one-character resource names ignored ("access H" from a mis-heard "SSH").
- `spoken()` reads hyphenated and underscored names as words ("demo web 01"). Tests: 7 new (voice interims, re-ask rule, question keeps the proposal, chat gets no interims, name separators, one-character names); 176 backend tests in total.

Decisions:

- Reading IDs back letter by letter ("i, dash, demo zero zero one") was not added: customers name instances, and Whisper does not transcribe IDs reliably enough for a read-back to help. The interim phrase reads the resolved name back instead.
- A noise that trips the VAD cancels the reply like any interruption; the agent then stays quiet rather than running Pipecat's LLM-oriented recovery prompt.
- Barge-in waits for a confirmed write to finish rather than cancelling it, so the audit trail never records a change whose verification was abandoned.

Verified (headless Chrome, customer's voice injected through Web Audio so the interruption can be timed, real model, machine swapping with 3 GB of swap in use):

- Documentation question interrupted mid-answer: the page learned of the interruption 0.55 s after speech onset and the agent's audio was silent 0.58 s after speech onset; the follow-up question was answered.
- SSH scenario by voice with the IP address spoken over the explanation: the explanation was cut, the proposal followed, "Yes, go ahead" applied and verified the rule (Resolved).
- SSH scenario with "Umm, hold on a second" after the proposal: re-asked once with "please say yes or no"; "Yes, go ahead" then applied the fix.
- Plain SSH scenario by voice, four turns, Resolved; the diagnosis turn's six sentences are reported as one turn.
- Server-side: VAD detection → interruption → output stopped in 80 ms. Latencies on this loaded machine were about three times Milestone 6's (transcription 4.5–6 s; diagnosis first word 64 s); the Milestone 6 numbers stand for an idle machine.

Known limits: a transcript slower than 20 s is lost; the browser's jitter buffer adds its delay to how long the agent's voice continues after a barge-in; the voice tests depend on a synthetic voice and Whisper's transcription of it varies between runs ("I can't access H" once for "I can't S S H").

### Milestone 8 — escalation flow, debug page, observability, full evaluation, README (done)

Built:

- Escalation: every rule now has a `trigger` key (`customer_request`, `unidentified`, `tool_failures`, `no_progress`, `no_fault_found`, `write_failed`); each escalation writes an `escalations` row (trigger, reason, channel, service, intent, and a `handoff` document with the problem statement, last message, entities, every check with its finding, root cause, proposed fix, rule counters) and emits an `escalated` event. `escalations_total` is labelled by trigger. The chat has a **Talk to a human** button (`{"type": "escalate"}` over the socket, `event="escalate"` in the agent), disabled once escalated; a pending change is cancelled on the way out.
- Debug API: `GET /api/debug/escalations`, and the conversation detail includes its escalations. Debug page: "Pending action and its history" (every transition from the audit log), escalation rule counters with their thresholds, an Escalations section with the handoff, and a "Recent escalations" list when no conversation is selected.
- Observability: `docker compose --profile observability up -d prometheus grafana` (`make observability`). Prometheus scrapes `localhost:8010/metrics` every 5 s; Grafana (port 3001, anonymous admin) is provisioned with the datasource and the dashboard **Cloud Support Agent** (12 panels: chat turns, escalations by rule, barge-ins, LLM first token and response time, tool calls and latency, knowledge-base search, voice per-stage latency p50/p95, Whisper/Piper timings, HTTP latency). New metric `speech_service_seconds{service,kind}` from Pipecat's per-request metrics.
- Full Compose: `backend/Dockerfile` (python:3.12-slim + uv, dependencies from `uv.lock`, model caches mounted from the host), `frontend/Dockerfile` (Next.js standalone build, `NEXT_PUBLIC_*` as build arguments), `backend` and `frontend` services on the host network, health checks. `make up` still starts only Postgres and FakeCloud; `make compose-up` builds and starts everything.
- README restructured to the spec's section list: architecture diagram, running locally and with Compose, observability, voice setup and browser requirements, escalation, debug page, metrics table, troubleshooting (model, memory, FakeCloud, WebRTC, WebSocket, Compose), evaluation results, upstream projects.
- Tests: escalation handoff record, escalation button (agent and WebSocket), Postgres `escalations` table; 180 backend tests plus 8 opt-in FakeCloud/Postgres tests.

Decisions:

- No separate `users`, `messages`, `tool_calls`, `pending_actions` tables: they live in the LangGraph checkpoint and the audit log (see SPEC §22).
- Host networking for the application and observability containers (Linux single-machine prototype; WebRTC and Ollama on the host).

Verified: in headless Chrome, "Talk to a human" after the first SSH message produced the fixed handoff sentence, the status "Handed to a support engineer", a disabled button, and on the debug page the escalation with trigger `customer_request` and its handoff (problem statement, protocol `ssh`, turn 2), plus the entry in "Recent escalations". Prometheus reports the backend target up and `escalations_total{trigger="customer_request"} 1`; the Grafana dashboard renders all 12 panels with live data.

## Remaining phases

None: all eight milestones are done. Known limits and ideas that were deliberately left out are listed per milestone above and in the README's "Known limits" and "Troubleshooting" sections.

## Flags

### Backend environment variables (`.env`, read by `backend/config.py`)

| Variable | Default | Effect | Used since |
|---|---|---|---|
| `LLM_PROVIDER` | `ollama` | `ollama`, `openai-compatible`, or `nvidia` (same as openai-compatible) | M1 |
| `LLM_MODEL` | `qwen3:4b-instruct` | Model for the standard and CUDA tiers | M1 |
| `LLM_FALLBACK_MODEL` | `qwen3:1.7b` | Model for the lightweight tier, and when `LLM_MODEL` is not installed | M1 |
| `LLM_BASE_URL` | `http://localhost:11434` | Provider address; include `/v1` for OpenAI-compatible endpoints | M1 |
| `LLM_API_KEY` | unset | Bearer key for OpenAI-compatible endpoints | M1 |
| `LLM_NUM_CTX` | `4096` | Context window requested from Ollama | M1 |
| `DATABASE_URL` | `postgresql+psycopg://support:support@localhost:5432/support` | Postgres for state, audit log, and document chunks. `memory://` keeps everything in the process; the knowledge base is then empty | M2 |
| `AWS_ENDPOINT_URL` | `http://localhost:4566` | FakeCloud address. Unset means real AWS; the seed script refuses to run then | M2 |
| `AWS_REGION` | `us-east-1` | Region for all AWS calls | M2 |
| `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | `test` / `test` | Keys starting with `test` bypass FakeCloud IAM enforcement (admin) | M2 |
| `BACKEND_HOST` | `127.0.0.1` | Bind address | M1 |
| `BACKEND_PORT` | `8010` | Bind port | M1 |
| `FRONTEND_ORIGIN` | `http://localhost:3000` | Origin allowed by CORS | M1 |
| `LIGHTWEIGHT` | `0` | `1` forces the lightweight tier regardless of hardware | M0 |
| `DEBUG_UI` | `1` | `1` registers the `/api/debug/*` endpoints | M2 |
| `EMBEDDING_MODEL` | `BAAI/bge-small-en-v1.5` | Embedding model for the knowledge base. After changing to a model with a different vector size, run `scripts/ingest_docs.py --recreate` | M3 |
| `EMBEDDING_CACHE_DIR` | `~/.cache/fastembed` | Where the embedding model is downloaded once and then loaded from | M3 |
| `RAG_TOP_K` | `3` | Chunks retrieved and given to the model per documentation answer | M3 |
| `RAG_MIN_SCORE` | `0.74` | Similarity (0 to 1) below which a chunk is not treated as relevant. Tuned for bge-small on this corpus; lower finds more but lets more irrelevant pages through | M3 |
| `STT_MODEL`, `STT_COMPUTE_TYPE` | `small`, `int8` | faster-whisper model name (from the Hugging Face cache; a new name downloads once) and CTranslate2 compute type | M6 |
| `STT_PROVIDER` | `faster-whisper` | Only faster-whisper is implemented | M6 |
| `TTS_VOICE` | `en_US-lessac-medium` | Piper voice; another name is downloaded on first use | M6 |
| `TTS_PROVIDER` | `piper` | Only Piper is implemented | M6 |
| `PIPER_CACHE_DIR` | `~/.cache/pipecat/piper` | Where Piper voices are downloaded once and then loaded from | M6 |
| `STT_THREADS` | `0` | CPU threads for transcription; `0` means half the cores, leaving the rest for the audio loop | M7 |
| `VOICE_ENABLED` | `1` | `0` skips loading Whisper (saves about 1 GB of RAM and 3 s of startup); `/api/voice/offer` then answers 503 and `/health` reports `voice.enabled: false`. Voice is also disabled, with a warning, when the models cannot be loaded | M6 |

### Frontend environment variables (`frontend/.env.local`)

| Variable | Default | Effect |
|---|---|---|
| `NEXT_PUBLIC_BACKEND_WS_URL` | `ws://localhost:8010/ws/chat` | Chat WebSocket address |
| `NEXT_PUBLIC_BACKEND_HTTP_URL` | `http://localhost:8010` | Backend address for the debug page and the voice signalling endpoint (`/api/voice/offer`) |
| `NEXT_PUBLIC_DEBUG_UI` | `1` | `1` shows the model name, per-reply latency, and the debug link in the chat page, and the per-stage latency line on the voice page |

### Docker Compose (`docker-compose.yml`)

| Setting | Value | Effect |
|---|---|---|
| `FAKECLOUD_IAM` | `strict` | IAM policies are enforced for IAM users (`off` and `soft` are the other modes) |
| `FAKECLOUD_LOG` | `info` | FakeCloud log level |
| `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB` | `support` each | Database credentials; must match `DATABASE_URL` |
| Docker socket | not mounted | FakeCloud runs as a control plane only; this is why RDS is out of scope |
| `backend`, `frontend` services | host network | The application in containers; `make up` does not start them, `docker compose up` / `make compose-up` does. Backend env: `BACKEND_HOST=0.0.0.0`, `DATABASE_URL`/`AWS_ENDPOINT_URL`/`LLM_BASE_URL` at localhost, model caches mounted from `~/.cache` |
| `prometheus`, `grafana` services | profile `observability`, host network | Prometheus on 9090 scraping `localhost:8010`; Grafana on 3001 (`GF_SERVER_HTTP_PORT`), anonymous admin, dashboard and datasource provisioned from `observability/` |
| Frontend build args | `NEXT_PUBLIC_BACKEND_WS_URL`, `NEXT_PUBLIC_BACKEND_HTTP_URL`, `NEXT_PUBLIC_DEBUG_UI` | Compiled into the frontend image (`frontend/Dockerfile`); the browser-facing addresses |

### Make targets

| Target | What it does |
|---|---|
| `make setup` | Python and frontend dependencies; creates `frontend/.env.local` |
| `make models` | Pulls the two Ollama models |
| `make up` / `make down` | Starts Postgres and FakeCloud / stops every Compose service, the observability profile included |
| `make hardware` | Prints detected hardware and the chosen model tier |
| `make probe` | FakeCloud fidelity table. Wipes the simulated account |
| `make seed` | Resets FakeCloud and creates the demo environment |
| `make ingest` | Downloads the documentation pages and builds the knowledge base |
| `make scenarios` | Regenerates the scenario and conversation files |
| `make evaluate-smoke` | Runs the 20-scenario subset, stores the run, reseeds the demo |
| `make evaluate` | Runs all 200 scenarios, stores the run, reseeds the demo |
| `make backend` / `make frontend` | Runs the backend / the Next.js dev server |
| `make observability` | Starts Prometheus and Grafana (profile `observability`) |
| `make compose-up` | Builds and starts the whole stack in Compose, waiting for health checks |
| `make test` | Backend tests (without the FakeCloud ones) and frontend lint |
| `make test-fakecloud` | Tests against FakeCloud, then reseeds the demo |

### Script options and test markers

| Command | Option | Effect |
|---|---|---|
| `scripts/probe_fakecloud.py` | `--json` | Prints the rows as JSON instead of a table |
| `scripts/seed_fakecloud.py` | none | Refuses to run when `AWS_ENDPOINT_URL` is unset |
| `scripts/ingest_docs.py` | `--manifest PATH` | Pages to ingest (default `datasets/knowledge/manifest.yaml`) |
| | `--corpus-dir PATH` | Where downloaded pages are kept (default `datasets/knowledge/corpus`) |
| | `--refetch` | Download every page again, even if already present |
| | `--offline` | Never download; fail if a page is not present |
| | `--recreate` | Drop and recreate the chunk table first |
| | `--dry-run` | Download and chunk only; do not embed or store |
| `scripts/generate_scenarios.py` | `--seed N` | Random seed (default 7); the same seed always gives the same files |
| | `--check` | Write nothing; exit 1 if the files on disk differ from what would be generated |
| `scripts/run_evaluation.py` | `--subset smoke\|all\|conversations` | Which set to run (default `all`) |
| | `--only ID,ID` | Run only these scenario ids from the chosen set |
| | `--limit N` | Run at most N scenarios |
| | `--model NAME` | Model to evaluate (default: the one the backend would select) |
| | `--json PATH` | Also write the full run to a JSON file |
| | `--no-store` | Do not store the run in the database |
| `pytest` | `-m fakecloud` | Selects the 7 tests that need FakeCloud and wipe it; excluded by default |

### Tunable constants in code

| Constant | Value | File | Effect |
|---|---|---|---|
| `LIGHTWEIGHT_FREE_RAM_GB` | 5.0 | `backend/hardware.py` | Free RAM below which the lightweight tier is chosen |
| `HISTORY_LIMIT` | 12 | `backend/agent/agent.py` | Messages kept for the model's context |
| `MAX_TOOL_ROUNDS` | 3 | `backend/agent/agent.py` | Model replies that may request tools per investigation |
| `MAX_TOOL_CALLS` | 6 | `backend/agent/agent.py` | Model-requested tool calls per investigation |
| `MAX_CLARIFICATIONS` | 2 | `backend/agent/agent.py` | Unidentified problems tolerated before escalating |
| `MAX_FAILED_TOOL_CALLS` | 3 | `backend/agent/agent.py` | Failed checks before escalating |
| `STALLED_TURNS` | 4 | `backend/agent/agent.py` | Turns without progress before escalating |
| `VERIFY_ATTEMPTS` | 5 | `backend/agent/agent.py` | Re-reads after a write, one second apart |
| `ACTION_TTL` | 10 minutes | `backend/tools/gateway.py` | Lifetime of a proposed change |
| tool timeout | 10 s | `backend/tools/gateway.py` | Per tool call |
| `HIGH_CPU_PERCENT` | 80 | `backend/agent/taxonomy.py` | Average CPU above which high CPU is a supported cause |
| `HIGH_NETWORK_BYTES` | 1 GB | `backend/agent/taxonomy.py` | Network traffic above which high network is a supported cause |
| `MAX_TIMEOUT_SECONDS` | 900 | `backend/tools/lambda_.py` | Largest Lambda timeout the write tool accepts |
| `MAX_ROLES_COMPARED` | 25 | `backend/tools/iam.py` | Other roles checked when looking for a role that has the permission |
| `MAX_POLICIES_READ` | 10 | `backend/tools/iam.py` | Policies returned by `iam_get_policy` |
| `DEFAULT_SEED` | 7 | `backend/evaluation/generator.py` | Seed of the committed scenario files |
| `MAX_TURNS` | 12 | `backend/evaluation/simulator.py` | Longest conversation the simulated customer will have |
| `SCENARIO_TIMEOUT_S` | 600 | `backend/evaluation/runner.py` | Time limit for one scenario |
| `MAX_MESSAGE_CHARS` | 2000 | `backend/api/chat.py` | Longest accepted chat message |
| `RATE_LIMIT_TURNS` / `RATE_LIMIT_WINDOW_S` | 20 / 60 | `backend/api/chat.py` | Turns allowed per connection per window |
| `MAX_CHUNK_CHARS` / `MIN_CHUNK_CHARS` | 1200 / 60 | `backend/rag/chunking.py` | Target largest chunk (a code block may exceed it) and smallest chunk kept; re-ingest after changing |
| `EXCERPT_CHARS` | 900 | `backend/agent/prompts.py` | Characters of each retrieved chunk shown to the model |
| embedding batch size | 8 | `backend/providers/embeddings/local.py` | Chunks embedded per batch during ingestion |
| Ollama `keep_alive` | 30 m | `backend/providers/llm/ollama.py` | How long the model stays loaded after a request |
| Ollama `think` | false | `backend/providers/llm/ollama.py` | Disables Qwen3 reasoning output |
| `VAD_START_SECS` | 0.1 | `backend/voice/pipeline.py` | Speech needed before the customer counts as speaking; barge-in reacts after this (Pipecat's default is 0.2) |
| `VAD_STOP_SECS` | 0.8 | `backend/voice/pipeline.py` | Silence that ends the customer's turn (Pipecat's default is 0.2; longer tolerates pauses mid-sentence) |
| `USER_SPEECH_TIMEOUT` | 0.5 | `backend/voice/pipeline.py` | Extra wait after the VAD stop before the turn is closed and the transcript used |
| `TURN_STOP_TIMEOUT` | 20 s | `backend/voice/pipeline.py` | Longest wait for the transcript after the VAD stop; a slower one is lost as an empty turn (Pipecat's default is 5 s) |
| `PROTECTED_WAIT_S` | 15 s | `backend/voice/pipeline.py` | Longest a barge-in waits for a confirmed change to finish applying and verifying |
| `MAX_CONFIRMATION_REASKS` | 1 | `backend/agent/agent.py` | Unclear spoken replies to a proposed change re-asked before it is withdrawn |
| escalation triggers | `customer_request`, `unidentified`, `tool_failures`, `no_progress`, `no_fault_found`, `write_failed` | `backend/agent/agent.py` (`_escalate` calls) | The rule recorded on each `escalations` row and as the `escalations_total` label |
| Prometheus `scrape_interval` | 5 s | `observability/prometheus.yml` | How often `/metrics` is read |
| Prometheus retention | 7 d | `docker-compose.yml` | `--storage.tsdb.retention.time` |
| `STT_SAMPLE_RATE` | 16000 | `backend/voice/pipeline.py` | Pipeline input sample rate (what Whisper expects) |
| `no_speech_prob` | 0.4 | `backend/voice/pipeline.py` | Whisper segments with a higher no-speech probability are dropped |
| interim phrases | "Let me check {name}.", "Let me look that up.", "Applying that now." | `backend/agent/agent.py` (`_interim`) | Said at once on a call while checks, retrieval, or a confirmed change run; not sent to chat |
| `SPOKEN_ERROR` | "Sorry, I couldn't process that…" | `backend/voice/pipeline.py` | Said when the model is unavailable during a voice turn |
| `allow_interruptions` | True | `backend/voice/pipeline.py` (`build_worker`) | Whether the customer speaking stops the agent (barge-in) |
| `empty_user_turn` | None | `backend/voice/pipeline.py` | Pipecat's recovery prompt for a turn with no transcript is off; the agent stays quiet |
| `cancel_on_idle_timeout` | False | `backend/voice/pipeline.py` | A quiet call is not hung up by Pipecat |
| `OFFER_URL` | `$NEXT_PUBLIC_BACKEND_HTTP_URL/api/voice/offer` | `frontend/lib/useVoiceClient.ts` | Signalling endpoint the browser posts its WebRTC offer to |
