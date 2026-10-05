from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    llm_provider: str = "ollama"
    llm_model: str = "qwen3:4b-instruct"
    llm_fallback_model: str = "qwen3:1.7b"
    llm_base_url: str = "http://localhost:11434"
    llm_api_key: str | None = None
    llm_num_ctx: int = 4096

    stt_provider: str = "faster-whisper"
    stt_model: str = "small"
    stt_compute_type: str = "int8"
    # CPU threads for transcription; 0 means half the cores, so a transcription cannot starve the audio loop.
    stt_threads: int = 0

    tts_provider: str = "piper"
    tts_voice: str = "en_US-lessac-medium"
    piper_cache_dir: str = "~/.cache/pipecat/piper"
    # Voice loads Whisper at startup (about 1 GB of RAM); 0 leaves it out.
    voice_enabled: bool = True

    embedding_model: str = "BAAI/bge-small-en-v1.5"
    embedding_cache_dir: str = "~/.cache/fastembed"
    rag_top_k: int = 3
    # Cosine similarity below which a chunk is not considered relevant to the question.
    rag_min_score: float = 0.74

    database_url: str = "postgresql+psycopg://support:support@localhost:5432/support"

    aws_endpoint_url: str | None = "http://localhost:4566"
    aws_region: str = "us-east-1"
    aws_access_key_id: str = "test"
    aws_secret_access_key: str = "test"

    backend_host: str = "127.0.0.1"
    backend_port: int = 8010
    frontend_origin: str = "http://localhost:3000"

    lightweight: bool = False
    debug_ui: bool = True


@lru_cache
def get_settings() -> Settings:
    return Settings()
