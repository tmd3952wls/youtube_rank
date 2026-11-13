"""Pydantic v1 / v2 양방향 호환 정형 데이터 스키마 (sfx_catalog.json 및 sfx_cue_sheet.json 겸 Gemini Structured Output 스키마)."""
from __future__ import annotations

import json
from typing import Any, Literal, Optional
from pydantic import BaseModel, Field


class _CompatBaseModel(BaseModel):
    """Pydantic v1 과 v2 환경 모두에서 동일 인터페이스로 동작하는 호환 베이스 모델."""

    def model_dump(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        if hasattr(super(), "model_dump"):
            return super().model_dump(*args, **kwargs)  # type: ignore[misc]
        return self.dict(*args, **kwargs)

    def model_dump_json(self, indent: Optional[int] = None, **kwargs: Any) -> str:
        return json.dumps(self.model_dump(**kwargs), ensure_ascii=False, indent=indent)

    def model_copy(self, *, update: Optional[dict[str, Any]] = None, deep: bool = False) -> Any:
        if hasattr(super(), "model_copy"):
            return super().model_copy(update=update, deep=deep)  # type: ignore[misc]
        return self.copy(update=update, deep=deep)

    @classmethod
    def model_validate(cls, obj: Any) -> Any:
        if hasattr(super(), "model_validate"):
            return super().model_validate(obj)  # type: ignore[misc]
        return cls.parse_obj(obj)

    @classmethod
    def model_validate_json(cls, json_data: str | bytes) -> Any:
        if hasattr(super(), "model_validate_json"):
            return super().model_validate_json(json_data)  # type: ignore[misc]
        return cls.parse_raw(json_data)


SfxCategory = Literal[
    "whoosh",          # 화면 전환·빠른 이동 (휙, 쉭, 스우시)
    "impact",          # 물리적 타격·충돌·저음 드롭 (쿵, 퍽, 두둥탁, 폭발)
    "pop_ui",          # 자막 등장·알림·가벼운 포인트 (팝, 띵, 뾰잉, 클릭)
    "comedy_cartoon",  # 코믹·당황·반전 (띠용, 미끄러짐, 레코드 스크래치, 삐용)
    "voice_reaction",  # 사람 리액션·감탄·비명·환호 (뜨헉, 와우, 군중환호, 웃음)
    "riser_tension",   # 긴장감 고조·슬로모션 빌드업 (심장박동, 라이저, 째깍째깍, 서스펜스)
    "water_liquid",    # 물·액체 효과음 (풍덩, 파도, 보글보글, 다이빙)
    "vehicle_horn",    # 탈것·기계·사이렌 (급브레이크, 경적, 엔진, 헬기)
    "animal_nature",   # 동물·자연 소리 (고양이, 강아지, 천둥, 바람)
    "ambience",        # 공간 배경음·무음 보강용 루프 (파도 앰비언스, 실내 소음, 자연음)
    "bgm_sting",       # 장시간 배경음악(BGM) 또는 하이라이트 스팅
    "other",           # 기타 분류
]

TriggerType = Literal[
    "hook_3s",           # 초반 0~3초 시선 고정 후킹
    "cut_transition",    # 순위 전환 / 컷 편집 지점
    "caption_emphasis",  # 핵심 자막 등장 (pop/shake/drop/zoom)
    "action_impact",     # 영상 내 물리적 충돌·점프·성공·실패 타격 순간
    "mood_twist",        # 분위기 반전·당황·슬로모션 진입
    "ambience_fill",     # 2초 이상 오디오 공백 보강용 환경음
]


class GeminiAudioListenResult(_CompatBaseModel):
    """Gemini 멀티모달 오디오 청취 단일 분석 응답 스키마."""

    category: SfxCategory = Field(description="효과음의 대분류 카테고리")
    subcategory: str = Field(description="구체적 소리 종류 (예: fast_whoosh, heavy_boom, cartoon_boing, water_splash)")
    moods: list[str] = Field(description="분위기 태그 2~5개 (예: ['긴박한', '코믹한', '타격감', '경쾌한'])")
    tags_ko: list[str] = Field(description="한국어 검색 키워드 3~7개 (예: ['휙', '화면전환', '빠른이동'])")
    tags_en: list[str] = Field(description="영어 검색 키워드 3~7개 (예: ['whoosh', 'fast transition', 'swipe'])")
    description_ko: str = Field(description="소리의 질감과 특징을 1문장으로 요약")
    recommended_moments: list[str] = Field(
        description="쇼츠 편집 시 추천 사용 상황 2~4개 (예: ['초반 3초 후킹 자막 등장', '빠른 화면 전환'])"
    )
    is_bgm_or_long_music: bool = Field(
        default=False,
        description="단발성 효과음이 아니라 10초 이상의 배경음악(BGM) 트랙인지 여부",
    )
    align_peak_recommended: bool = Field(
        default=True,
        description="앞부분 빌드업이 있어 타격 정점(peak)을 이벤트 타임스탬프에 맞춰야 자연스러운지 여부",
    )


class SfxCatalogItem(_CompatBaseModel):
    """sfx_catalog.json 개별 효과음 레코드."""

    id: str
    filename: str
    rel_path: str
    sha1: str
    size_bytes: int
    mtime_ns: int
    duration_sec: float
    peak_time_sec: float
    onset_time_sec: float
    peak_dbfs: float
    rms_dbfs: float
    attack_type: Literal["impulsive", "swell", "sustained"]
    category: SfxCategory
    subcategory: str
    moods: list[str]
    tags_ko: list[str]
    tags_en: list[str]
    description_ko: str
    recommended_moments: list[str]
    is_bgm_or_long_music: bool = False
    align_peak_recommended: bool = True
    source: Literal["local", "freesound", "elevenlabs", "dsp_heuristic"] = "local"
    external_url: Optional[str] = None
    license_info: Optional[str] = None
    indexed_at: str = ""


class SfxCatalog(_CompatBaseModel):
    """sfx_catalog.json 루트 문서."""

    version: str = "1.0"
    sfx_dir: str
    updated_at: str
    total_count: int
    items: list[SfxCatalogItem] = Field(default_factory=list)


class GeminiCueItemDraft(_CompatBaseModel):
    """Gemini 영상 시청 디렉터가 반환하는 개별 큐 항목."""

    timestamp_str: str = Field(description="목표 이벤트 시각 (MM:SS.cc 형식, 예: '00:01.35')")
    timestamp_sec: float = Field(description="목표 이벤트 시각 (초 단위 소수, 예: 1.35)")
    trigger_type: TriggerType = Field(description="연출 트리거 유형")
    category: SfxCategory = Field(description="필요한 효과음 대분류")
    sound_concept_ko: str = Field(description="원하는 효과음 설명 (한국어, 예: '묵직하고 짧은 저음 타격음 쿵')")
    search_keywords_ko: list[str] = Field(description="로컬 한국어 효과음 매칭용 핵심 단어 2~5개 (예: ['쿵', '타격', '임팩트'])")
    search_keywords_en: list[str] = Field(description="Freesound 검색용 영어 키워드 2~4개 (예: ['deep boom', 'impact hit'])")
    generation_prompt_en: str = Field(
        description="ElevenLabs 생성 폴백용 구체적 영어 프롬프트 (예: 'Short punchy cinematic sub bass impact hit for video transition')"
    )
    visual_reason_ko: str = Field(description="영상의 어떤 프레임·모션·자막 때문에 이 소리를 배치했는지 연출 근거")
    align_to_peak: bool = Field(default=True, description="효과음의 정점(peak)을 이 타임스탬프에 정확히 맞출지 여부")
    intensity: float = Field(
        default=0.8,
        ge=0.2,
        le=1.2,
        description="연출 강도 (0.2=은은한 배경 보조, 0.7=일반 강조, 1.0=결정적 펀치라인 타격)",
    )
    max_duration_sec: float = Field(
        default=2.0,
        ge=0.2,
        le=10.0,
        description="이 구간에서 허용되는 효과음 최대 재생 길이(초)",
    )
    rank_context: Optional[int] = Field(default=None, description="해당 시점의 랭킹 번호 (예: 7~1, 없으면 null)")


class GeminiVideoDirectionResponse(_CompatBaseModel):
    """Gemini 영상 시청 디렉터 전체 JSON 응답 스키마."""

    video_summary_ko: str = Field(description="전체 쇼츠 영상의 주제, 템포, 편집 리듬 요약")
    hook_analysis_ko: str = Field(description="초반 0~3초 후킹 구간의 시각·자막 분석 및 사운드 연출 전략")
    cues: list[GeminiCueItemDraft] = Field(description="시간순으로 정렬된 효과음 연출 큐 목록")


class SfxCueItem(_CompatBaseModel):
    """최종 정형화된 큐시트 항목 (매칭 결과 및 볼륨/덕킹 계산값 포함)."""

    cue_id: str
    timestamp_str: str
    timestamp_sec: float
    frame_index: int
    trigger_type: TriggerType
    category: SfxCategory
    sound_concept_ko: str
    search_keywords_ko: list[str]
    search_keywords_en: list[str]
    generation_prompt_en: str
    visual_reason_ko: str
    align_to_peak: bool = True
    intensity: float = 0.8
    max_duration_sec: float = 2.0
    rank_context: Optional[int] = None

    # 매칭·수급 단계에서 채워지는 필드
    matched_sfx_id: Optional[str] = None
    matched_file: Optional[str] = None
    matched_source: Optional[str] = None
    match_score: Optional[float] = None
    sfx_peak_time_sec: float = 0.0
    playback_start_sec: float = 0.0
    overlaps_voice: bool = False
    target_db_relative_to_voice: Optional[float] = None
    computed_linear_gain: Optional[float] = None


class SfxCueSheet(_CompatBaseModel):
    """sfx_cue_sheet.json 루트 문서."""

    version: str = "1.0"
    video_path: str
    video_duration_sec: float
    fps: float = 30.0
    created_at: str
    director_model: str
    video_summary_ko: str
    hook_analysis_ko: str
    cues: list[SfxCueItem] = Field(default_factory=list)
