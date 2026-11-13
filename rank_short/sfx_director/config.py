"""환경변수(.env / secrets.env / OS 환경변수) 로더 및 파이프라인 설정."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_WINDOWS_SFX_DIR = r"C:\Users\tmd39\Desktop\효과음\효과음2869개"


def _clean_secret(val: Optional[str]) -> str:
    if not val:
        return ""
    s = val.strip()
    while len(s) >= 2 and ((s[0] == s[-1] == '"') or (s[0] == s[-1] == "'")):
        s = s[1:-1].strip()
    return s


def _parse_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    # UTF-8 BOM(\ufeff)으로 저장된 윈도우 메모장 .env 파일까지 안전하게 처리
    for raw in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip().lstrip("\ufeff")
        out[k] = _clean_secret(v)
    return out


def _as_bool(val: Optional[str], default: bool = False) -> bool:
    if val is None or val == "":
        return default
    return val.strip().lower() in ("1", "true", "yes", "on", "y")


def _as_float(val: Optional[str], default: float) -> float:
    if val is None or val == "":
        return default
    try:
        return float(val.strip())
    except ValueError:
        return default


def _as_int(val: Optional[str], default: int) -> int:
    if val is None or val == "":
        return default
    try:
        return int(val.strip())
    except ValueError:
        return default


@dataclass
class SfxDirectorConfig:
    """자동 효과음 디렉터 전체 설정 객체."""

    auto_sfx_enabled: bool = False

    # 경로 설정
    local_sfx_dir: Path = field(default_factory=lambda: Path(DEFAULT_WINDOWS_SFX_DIR))
    catalog_path: Path = field(default_factory=lambda: Path(DEFAULT_WINDOWS_SFX_DIR) / "sfx_catalog.json")
    auto_acquired_subdir: str = "_auto_acquired"

    # Gemini 멀티모달 API 설정 (AIza 표준 키, AQ. 신형 AI Studio Auth 키, AQ. Vertex AI Express 키 모두 호환)
    gemini_api_key: str = ""
    # "auto" (기본: 일반 엔드포인트 시도 후 AQ. Vertex Express 키면 자동 전환) | "true" | "false"
    gemini_vertexai_mode: str = "auto"
    gemini_audio_model: str = "gemini-3.5-flash-lite"
    gemini_video_model: str = "gemini-3.8-flash"
    catalog_batch_size: int = 1
    catalog_checkpoint_every: int = 20
    catalog_max_inline_mb: float = 15.0

    # 런타임 중 자동 판별된 백엔드 캐시 (None = 아직 미판별, True = Vertex Express, False = AI Studio)
    resolved_vertexai: Optional[bool] = None

    # 하이브리드 매칭 & 부족분 자동 수급(Fallback) 설정
    fallback_mode: str = "auto"
    local_match_threshold: float = 0.45
    local_soft_floor: float = 0.22

    freesound_api_key: str = ""
    freesound_oauth_token: str = ""

    elevenlabs_api_key: str = ""
    elevenlabs_sfx_model: str = "eleven_text_to_sound_v2"
    elevenlabs_output_format: str = "mp3_44100_128"
    elevenlabs_prompt_influence: float = 0.35

    # 오디오 볼륨 밸런싱(덕킹) 및 믹싱 설정 (-12dB ~ -18dB)
    sfx_duck_db_min: float = -12.0
    sfx_duck_db_max: float = -18.0
    sfx_idle_peak_db: float = -8.5
    voice_vad_margin_db: float = 8.0
    use_sidechain_ducking: bool = False
    target_loudness_lufs: float = -14.0

    # 큐시트 밀도 가드레일
    max_cues_per_clip: int = 4
    min_cue_gap_sec: float = 0.30
    preserve_manual_fx: bool = True

    @property
    def effective_fallback_chain(self) -> list[str]:
        mode = self.fallback_mode.lower().strip()
        if mode == "local_only":
            return ["local"]
        chain = ["local"]
        if mode in ("auto", "freesound") and self.freesound_api_key:
            chain.append("freesound")
        if mode in ("auto", "elevenlabs") and self.elevenlabs_api_key:
            chain.append("elevenlabs")
        return chain

    @property
    def acquired_dir(self) -> Path:
        return self.local_sfx_dir / self.auto_acquired_subdir


def create_genai_client(cfg: SfxDirectorConfig, force_vertexai: Optional[bool] = None):
    """AI Studio 키(`AIza...`, `AQ....`)와 Vertex AI Express Mode 키(`AQ....`)를 모두 지원하는 클라이언트 팩토리."""
    from google import genai

    use_vertex = force_vertexai
    if use_vertex is None:
        if cfg.resolved_vertexai is not None:
            use_vertex = cfg.resolved_vertexai
        elif cfg.gemini_vertexai_mode == "true":
            use_vertex = True
        elif cfg.gemini_vertexai_mode == "false":
            use_vertex = False
        else:
            use_vertex = False

    if use_vertex:
        return genai.Client(vertexai=True, api_key=cfg.gemini_api_key), True
    return genai.Client(api_key=cfg.gemini_api_key), False


def is_auth_route_mismatch_error(exc: Exception) -> bool:
    msg = str(exc)
    return any(
        token in msg
        for token in (
            "API_KEY_INVALID",
            "API key not valid",
            "ACCESS_TOKEN_TYPE_UNSUPPORTED",
            "UNAUTHENTICATED",
            "401",
        )
    )


def load_director_config(
    repo_root: Optional[Path] = None,
    plan_overrides: Optional[dict] = None,
    cli_auto_sfx: Optional[bool] = None,
) -> SfxDirectorConfig:
    """우선순위: CLI 인자 > plan.json > 프로젝트 .env(명시된 비어있지 않은 값 우선) > OS 환경변수 > 기본값."""
    root = Path(repo_root).resolve() if repo_root else REPO_ROOT
    file_vals: dict[str, str] = {}
    for env_file in (root / "secrets.env", root / ".env"):
        file_vals.update(_parse_env_file(env_file))

    merged: dict[str, str] = {}
    for k, v in file_vals.items():
        merged[k] = v
    # 명시적으로 전달된 비어있지 않은 OS/프로세스 환경변수는 .env 기본값보다 우선 적용
    for k, v in os.environ.items():
        cleaned = _clean_secret(v)
        if cleaned != "" or k not in merged:
            merged[k] = cleaned

    def get(key: str, default: Optional[str] = None) -> Optional[str]:
        return merged.get(key, default)

    plan_overrides = plan_overrides or {}

    sfx_dir_str = (
        plan_overrides.get("local_sfx_dir")
        or plan_overrides.get("sfx_dir")
        or get("LOCAL_SFX_DIR")
        or DEFAULT_WINDOWS_SFX_DIR
    )
    local_sfx_dir = Path(sfx_dir_str)

    catalog_str = plan_overrides.get("sfx_catalog_path") or get("SFX_CATALOG_PATH")
    if catalog_str:
        catalog_path = Path(catalog_str)
        if not catalog_path.is_absolute():
            catalog_path = root / catalog_path
    else:
        catalog_path = local_sfx_dir / "sfx_catalog.json"

    auto_enabled = _as_bool(get("AUTO_SFX_ENABLED"), default=False)
    if "auto_sfx" in plan_overrides and plan_overrides["auto_sfx"] is not None:
        auto_enabled = bool(plan_overrides["auto_sfx"])
    if cli_auto_sfx is not None:
        auto_enabled = bool(cli_auto_sfx)

    gemini_key = (
        get("GEMINI_API_KEY")
        or get("VERTEX_AI_KEY")
        or get("GOOGLE_API_KEY")
        or ""
    )
    vertex_mode = (get("GEMINI_USE_VERTEXAI") or get("GOOGLE_GENAI_USE_VERTEXAI") or "auto").strip().lower()
    if vertex_mode not in ("auto", "true", "false"):
        vertex_mode = "true" if _as_bool(vertex_mode) else "auto"

    # VERTEX_AI_KEY 로 넣었으면 기본 Vertex Express 모드 우선
    if not get("GEMINI_API_KEY") and get("VERTEX_AI_KEY") and vertex_mode == "auto":
        vertex_mode = "true"

    duck_min = _as_float(get("SFX_DUCK_DB_MIN"), -12.0)
    duck_max = _as_float(get("SFX_DUCK_DB_MAX"), -18.0)
    hi_db, lo_db = max(duck_min, duck_max), min(duck_min, duck_max)

    return SfxDirectorConfig(
        auto_sfx_enabled=auto_enabled,
        local_sfx_dir=local_sfx_dir,
        catalog_path=catalog_path,
        auto_acquired_subdir=get("SFX_AUTO_ACQUIRED_SUBDIR", "_auto_acquired") or "_auto_acquired",
        gemini_api_key=gemini_key,
        gemini_vertexai_mode=vertex_mode,
        gemini_audio_model=get("GEMINI_AUDIO_MODEL", "gemini-3.5-flash-lite") or "gemini-3.5-flash-lite",
        gemini_video_model=get("GEMINI_VIDEO_MODEL", "gemini-3.8-flash") or "gemini-3.8-flash",
        catalog_batch_size=max(1, _as_int(get("SFX_CATALOG_BATCH_SIZE"), 1)),
        catalog_checkpoint_every=max(1, _as_int(get("SFX_CATALOG_CHECKPOINT_EVERY"), 20)),
        fallback_mode=(get("SFX_FALLBACK_MODE", "auto") or "auto").lower(),
        local_match_threshold=_as_float(get("SFX_MATCH_THRESHOLD"), 0.45),
        local_soft_floor=_as_float(get("SFX_LOCAL_SOFT_FLOOR"), 0.22),
        freesound_api_key=get("FREESOUND_API_KEY", "") or "",
        freesound_oauth_token=get("FREESOUND_OAUTH_TOKEN", "") or "",
        elevenlabs_api_key=get("ELEVENLABS_API_KEY", "") or "",
        elevenlabs_sfx_model=get("ELEVENLABS_SFX_MODEL", "eleven_text_to_sound_v2") or "eleven_text_to_sound_v2",
        elevenlabs_output_format=get("ELEVENLABS_OUTPUT_FORMAT", "mp3_44100_128") or "mp3_44100_128",
        elevenlabs_prompt_influence=_as_float(get("ELEVENLABS_PROMPT_INFLUENCE"), 0.35),
        sfx_duck_db_min=hi_db,
        sfx_duck_db_max=lo_db,
        sfx_idle_peak_db=_as_float(get("SFX_IDLE_PEAK_DB"), -8.5),
        voice_vad_margin_db=_as_float(get("SFX_VOICE_VAD_MARGIN_DB"), 8.0),
        use_sidechain_ducking=_as_bool(get("SFX_USE_SIDECHAIN"), default=False),
        target_loudness_lufs=_as_float(get("SFX_TARGET_LUFS"), -14.0),
        max_cues_per_clip=max(1, _as_int(get("SFX_MAX_CUES_PER_CLIP"), 4)),
        min_cue_gap_sec=max(0.05, _as_float(get("SFX_MIN_CUE_GAP_SEC"), 0.30)),
        preserve_manual_fx=_as_bool(get("SFX_PRESERVE_MANUAL_FX"), default=True),
    )
