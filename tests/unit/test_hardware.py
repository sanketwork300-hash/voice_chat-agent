from backend.config import Settings
from backend.hardware import Hardware, Tier, select_models


def hardware(ram_available_gb: float = 8.0, cuda: bool = False) -> Hardware:
    return Hardware(
        cpu="test", cpu_threads=8, ram_total_gb=16.0, ram_available_gb=ram_available_gb,
        gpu="test gpu" if cuda else None, vram_gb=12.0 if cuda else None, cuda=cuda,
    )


def settings(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


def test_cpu_only_with_enough_ram_is_standard():
    selection = select_models(hardware(), settings())
    assert selection.tier is Tier.STANDARD
    assert (selection.llm_model, selection.stt_model, selection.tts_provider) == (
        "qwen3:4b-instruct", "small", "piper")


def test_low_free_ram_falls_back_to_lightweight():
    selection = select_models(hardware(ram_available_gb=4.0), settings())
    assert selection.tier is Tier.LIGHTWEIGHT
    assert (selection.llm_model, selection.stt_model) == ("qwen3:1.7b", "base")


def test_ram_held_by_loaded_models_counts_as_free():
    selection = select_models(hardware(ram_available_gb=3.0), settings(), reclaimable_gb=3.2)
    assert selection.tier is Tier.STANDARD


def test_lightweight_flag_wins_over_cuda():
    selection = select_models(hardware(cuda=True), settings(lightweight=True))
    assert selection.tier is Tier.LIGHTWEIGHT


def test_cuda_selects_turbo_stt():
    selection = select_models(hardware(cuda=True), settings())
    assert selection.tier is Tier.CUDA
    assert selection.stt_model == "large-v3-turbo"
