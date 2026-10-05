"""Detect local hardware and pick the model tier (SPEC section 5)."""

import json
import os
import platform
import shutil
import subprocess
from dataclasses import asdict, dataclass
from enum import StrEnum

import psutil

from backend.config import Settings, get_settings

# The 4B model holds about 3.2 GB when loaded and the speech models about 1 GB.
LIGHTWEIGHT_FREE_RAM_GB = 5.0


class Tier(StrEnum):
    LIGHTWEIGHT = "lightweight"
    STANDARD = "standard"
    CUDA = "cuda"


@dataclass(frozen=True)
class Hardware:
    cpu: str
    cpu_threads: int
    ram_total_gb: float
    ram_available_gb: float
    gpu: str | None
    vram_gb: float | None
    cuda: bool


@dataclass(frozen=True)
class ModelSelection:
    tier: Tier
    llm_model: str
    stt_model: str
    tts_provider: str
    reason: str


def _cpu_name() -> str:
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.startswith("model name"):
                    return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or platform.machine()


def _nvidia() -> tuple[str, float] | None:
    """Name and VRAM (GB) of the first NVIDIA GPU, or None when there is no usable one."""
    if not shutil.which("nvidia-smi"):
        return None
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5, check=True,
        ).stdout.strip()
    except (subprocess.SubprocessError, OSError):
        return None
    if not out:
        return None
    name, mem_mib = out.splitlines()[0].rsplit(",", 1)
    return name.strip(), round(float(mem_mib) / 1024, 1)


def _other_gpu() -> str | None:
    if not shutil.which("lspci"):
        return None
    try:
        out = subprocess.run(["lspci"], capture_output=True, text=True, timeout=5).stdout
    except (subprocess.SubprocessError, OSError):
        return None
    for line in out.splitlines():
        if any(k in line for k in ("VGA compatible", "3D controller", "Display controller")):
            return line.split(": ", 1)[-1].strip()
    return None


def detect() -> Hardware:
    mem = psutil.virtual_memory()
    nvidia = _nvidia()
    return Hardware(
        cpu=_cpu_name(),
        cpu_threads=os.cpu_count() or 1,
        ram_total_gb=round(mem.total / 2**30, 1),
        ram_available_gb=round(mem.available / 2**30, 1),
        gpu=nvidia[0] if nvidia else _other_gpu(),
        vram_gb=nvidia[1] if nvidia else None,
        cuda=nvidia is not None,
    )


def select_models(hw: Hardware, settings: Settings, reclaimable_gb: float = 0.0) -> ModelSelection:
    """Pick models for the hardware.

    `reclaimable_gb` is RAM already held by loaded LLMs. It counts as free:
    otherwise a model that is still loaded from the last run makes the machine
    look too small for that same model.
    """
    free_gb = round(hw.ram_available_gb + reclaimable_gb, 1)
    if settings.lightweight:
        tier, reason = Tier.LIGHTWEIGHT, "LIGHTWEIGHT=1"
    elif free_gb < LIGHTWEIGHT_FREE_RAM_GB:
        tier = Tier.LIGHTWEIGHT
        reason = f"{free_gb} GB RAM free for models, below {LIGHTWEIGHT_FREE_RAM_GB} GB"
    elif hw.cuda:
        tier, reason = Tier.CUDA, f"CUDA GPU with {hw.vram_gb} GB VRAM"
    else:
        tier, reason = Tier.STANDARD, "CPU only, enough free RAM"

    if tier is Tier.LIGHTWEIGHT:
        return ModelSelection(tier, settings.llm_fallback_model, "base", "piper", reason)
    if tier is Tier.CUDA:
        return ModelSelection(tier, settings.llm_model, "large-v3-turbo", settings.tts_provider, reason)
    return ModelSelection(tier, settings.llm_model, settings.stt_model, "piper", reason)


def report() -> dict:
    hw = detect()
    return {"hardware": asdict(hw), "selection": asdict(select_models(hw, get_settings()))}


if __name__ == "__main__":
    print(json.dumps(report(), indent=2))
