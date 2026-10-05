import logging

from backend.config import Settings
from backend.providers.llm.base import LLMError, LLMProvider, LLMUnavailable, Message
from backend.providers.llm.ollama import OllamaProvider
from backend.providers.llm.openai_compatible import OpenAICompatibleProvider

__all__ = ["LLMError", "LLMProvider", "LLMUnavailable", "Message", "create_provider", "resolve_model"]

log = logging.getLogger(__name__)


def create_provider(settings: Settings) -> LLMProvider:
    match settings.llm_provider:
        case "ollama":
            return OllamaProvider(settings.llm_base_url, num_ctx=settings.llm_num_ctx)
        case "openai-compatible" | "nvidia":
            return OpenAICompatibleProvider(settings.llm_base_url, api_key=settings.llm_api_key)
        case other:
            raise ValueError(f"Unknown LLM_PROVIDER {other!r}; use ollama, openai-compatible, or nvidia")


async def resolve_model(provider: LLMProvider, preferred: str, fallback: str) -> str:
    """Pick the preferred model if the provider has it, otherwise the fallback.

    Never raises: a missing model or unreachable provider must not stop startup.
    The chat endpoint reports the problem to the customer when a turn fails.
    """
    try:
        available = await provider.list_models()
    except LLMUnavailable as e:
        log.warning("LLM provider unreachable at startup, keeping %s: %s", preferred, e)
        return preferred
    if preferred in available:
        return preferred
    if fallback in available:
        log.warning("Model %s is not available, falling back to %s", preferred, fallback)
        return fallback
    log.warning("Neither %s nor %s is available (found: %s)", preferred, fallback, available)
    return preferred
