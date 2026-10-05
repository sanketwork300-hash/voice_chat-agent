from pathlib import Path

from fastembed import TextEmbedding


class LocalEmbedder:
    """A small ONNX embedding model run on the CPU through fastembed.

    The model is downloaded once into `cache_dir`; after that no network is needed.
    """

    def __init__(self, model: str, cache_dir: str):
        cache = Path(cache_dir).expanduser()
        cache.mkdir(parents=True, exist_ok=True)
        self.name = model
        self._model = TextEmbedding(model, cache_dir=str(cache))
        self.dimensions = len(self.embed_query("dimension probe"))

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        # Each batch is padded to its longest text, so small batches of similar
        # length are several times faster on CPU than large mixed ones.
        order = sorted(range(len(texts)), key=lambda i: len(texts[i]))
        vectors = self._model.embed([texts[i] for i in order], batch_size=8)
        result: list[list[float]] = [[]] * len(texts)
        for index, vector in zip(order, vectors):
            result[index] = vector.tolist()
        return result

    def embed_query(self, text: str) -> list[float]:
        # Query embedding adds the retrieval instruction the model was trained with.
        return next(iter(self._model.query_embed(text))).tolist()
