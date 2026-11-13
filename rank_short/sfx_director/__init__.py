"""완전 자동 효과음 디렉터 파이프라인 (Gemini Multimodal + Hybrid Fallback + FFmpeg Ducking Mixer).

기존 `rank_short/make_rank.py` 파이프라인을 깨뜨리지 않고 독립적으로 동작하며,
`AUTO_SFX_ENABLED=true/false` 환경변수 또는 CLI 플래그로 제어됩니다.
"""

from .config import SfxDirectorConfig, load_director_config
from .pipeline import run_auto_sfx_for_render, run_full_sfx_pipeline

__all__ = [
    "SfxDirectorConfig",
    "load_director_config",
    "run_auto_sfx_for_render",
    "run_full_sfx_pipeline",
]
