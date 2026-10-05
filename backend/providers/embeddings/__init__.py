from backend.config import Settings
from backend.providers.embeddings.base import Embedder

__all__ = ["Embedder", "create_embedder"]


def create_embedder(settings: Settings) -> Embedder:
    # Imported here so that code which never embeds does not load the ONNX runtime.
    from backend.providers.embeddings.local import LocalEmbedder

    return LocalEmbedder(settings.embedding_model, settings.embedding_cache_dir)
