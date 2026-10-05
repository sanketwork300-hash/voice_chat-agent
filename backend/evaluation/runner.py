"""Run scenarios against the agent and score them.

For each scenario: reset FakeCloud, build the scenario's fixture, let the
simulated customer talk to the agent, read FakeCloud back to check the
post-condition, score, and move on.
"""

import asyncio
import logging
from collections.abc import Callable

from langgraph.checkpoint.memory import InMemorySaver

from backend.agent.agent import SupportAgent, checkpoint_serde
from backend.cloud.base import CloudError, CloudProvider
from backend.evaluation import fixtures
from backend.evaluation.scenarios import Scenario
from backend.evaluation.scoring import Checks, score
from backend.evaluation.simulator import Outcome, RecordingCloud, converse
from backend.memory.store import MemoryStore
from backend.providers.llm import LLMError, LLMProvider
from backend.rag.knowledge import KnowledgeBase
from backend.tools.gateway import ToolGateway

log = logging.getLogger(__name__)

SCENARIO_TIMEOUT_S = 600

Result = tuple[Scenario, Outcome, Checks]
Prepare = Callable[[Scenario], Scenario]


def build_agent(llm: LLMProvider, model: str, cloud: CloudProvider, knowledge: KnowledgeBase, *,
                rag_top_k: int, rag_min_score: float) -> tuple[SupportAgent, RecordingCloud]:
    """An agent whose cloud writes are recorded. Conversations are kept in memory, not in Postgres."""
    recording = RecordingCloud(cloud)
    store = MemoryStore()
    agent = SupportAgent(
        llm, model, ToolGateway(recording, store), store, InMemorySaver(serde=checkpoint_serde()), knowledge,
        rag_top_k=rag_top_k, rag_min_score=rag_min_score)
    return agent, recording


def fakecloud_prepare(cloud, endpoint_url: str) -> Prepare:
    """Reset FakeCloud, build the scenario's fixture, and fill the scenario's placeholders with what was created."""
    def prepare(scenario: Scenario) -> Scenario:
        fixtures.reset(endpoint_url)
        handles = fixtures.apply(cloud.client, scenario.fixture)
        return Scenario.model_validate(fixtures.fill(scenario.model_dump(), handles))
    return prepare


async def run_scenarios(scenarios: list[Scenario], agent: SupportAgent, recording: RecordingCloud, *,
                        prepare: Prepare, admin: CloudProvider, run_id: str,
                        on_result: Callable[[int, Result], None] | None = None) -> list[Result]:
    results: list[Result] = []
    for index, scenario in enumerate(scenarios):
        outcome = Outcome(scenario_id=scenario.scenario_id)
        try:
            # Fixtures use blocking AWS calls; keep them off the event loop.
            scenario = await asyncio.to_thread(prepare, scenario)
            outcome = await asyncio.wait_for(
                converse(agent, scenario, f"{run_id}:{scenario.scenario_id}", recording), SCENARIO_TIMEOUT_S)
            if scenario.post_condition is not None:
                outcome.post_condition = await asyncio.to_thread(
                    fixtures.post_condition_holds, admin, scenario.post_condition)
        except TimeoutError:
            outcome.error = f"no result within {SCENARIO_TIMEOUT_S} seconds"
        except (LLMError, CloudError, OSError) as e:
            log.warning("scenario %s failed: %s", scenario.scenario_id, e)
            outcome.error = f"{type(e).__name__}: {e}"

        result = (scenario, outcome, score(scenario, outcome))
        results.append(result)
        if on_result:
            on_result(index, result)
    return results
