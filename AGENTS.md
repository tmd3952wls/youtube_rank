# AGENTS.md — 멀티에이전트 역할 분담 및 쇼츠 사운드 연출 규약

[Google Antigravity (`agy`)](https://antigravity.google/docs/cli/best-practices/)에서 작업이나 서브에이전트(`/swarm`, `/agents`, 백그라운드 태스크)를 실행할 때 아래 역할 분담과 레퍼런스 쇼츠 연출 규칙을 준수합니다.

## 1. 에이전트 역할 아키텍처 (Multi-Agent Workflow)

### Agent 1: Sourcing & Script Planner (`find_cc.py` + `make_rank.py draft`)
- **역할**: 주제에 맞는 Creative Commons(`CC BY`) 원본 영상을 탐색하고 자극도 곡선(**7등=두 번째로 센 후킹 클립 → 6~4등 완급 조절 → 3~2등 재상승 → 1등=가장 강력한 레전드 클립**)에 따라 `plan.json`을 구성합니다.
- **절대 규칙**:
  - `Compilation`, `Best of`, `모음` 채널 영상은 CC 표기가 있어도 재업로드 위험이 높으므로 배제합니다.
  - 각 클립의 `end`는 **사건이 터지는 순간 + 0.2~0.4초**에 칼같이 자릅니다 (뒷수습·리액션 늘어짐 금지).
  - 자막(`subs`)은 **한 줄 2~3어절** 이내로 작성하며, 강조 단어만 `[y:노랑]`, `[r:빨강]` 태그와 애니메이션(`drop`, `pop`, `shake`, `zoom`)을 부여합니다.

### Agent 2: SFX Cataloger (`rank_short/sfx_director/catalog.py`)
- **역할**: `LOCAL_SFX_DIR`(`C:\Users\tmd39\Desktop\효과음\효과음2869개`)의 오디오 자산을 관리합니다.
- **절대 규칙**:
  - 전체 재분석(`--force`)은 사용자가 명시적으로 요청할 때만 수행하며, 평소에는 `(rel_path, size_bytes, mtime_ns)` 및 `SHA1` 해시 대조를 통한 **증분 분석**만 실행합니다.
  - 타격 시점(`peak_time_sec`)과 음량(`peak_dbfs`, `rms_dbfs`)은 LLM 추측이 아닌 FFmpeg+NumPy 실측치를 기록합니다.

### Agent 3: Multimodal SFX Director (`rank_short/sfx_director/cue_sheet.py`)
- **역할**: 초안 렌더링 영상(`joined.mp4`)을 시청하며 프레임·밀리초 단위 효과음 큐시트(`sfx_cue_sheet.json`)를 설계합니다.
- **레퍼런스 벤치마크('랭킹감자' 스타일 8편 분석 기준)**:
  1. **0~3초 초반 후킹(`hook_3s`)**: `00:00.10~00:00.20` 오프닝 스우시/팝 + 첫 사건 폭발 프레임 임팩트 타격.
  2. **순위 컷 전환(`cut_transition`)**: 0.25초 검은 화면(`gap`) 정중앙에 `whoosh` 정점 배치 → 다음 순위 첫 프레임에 저음 `hit`(`쿵`) 배치 (단, `make_rank.py` 기본 `transition`이 켜져 있으면 중복 생성 금지).
  3. **자막·감정선 매칭(`caption_emphasis` / `mood_twist`)**:
     - 상황 설명 `drop` 자막 → 짧은 `pop_ui` (`팝`, `띵`)
     - 의문/당황 `?`, `어..?` → `comedy_cartoon` (`띠용`, `뾰잉`)
     - 절정/비명 `[r:...]` `shake`/`zoom` → `impact`(`두둥탁`, `퍽`), `water_liquid`(`풍덩`), `voice_reaction`(`뜨헉!`, `와우`)
  4. **오디오 공백 제로(`ambience_fill`)**: 원본 소리가 없거나 2초 이상 정적이 흐르는 구간에는 장면 공간감에 맞는 환경음(`파도`, `바람`, `심장박동`)을 깔아 지루함을 방지합니다.
  5. **과밀 방지**: 클립 하나당 자동 효과음은 최대 4개 이내, 인접 단발 효과음 간 최소 0.30초 간격을 유지합니다.

### Agent 4: Hybrid Procurer & Audio Mixer (`matcher_fallback.py` + `mixer.py`)
- **역할**: 큐시트 사운드 수급 및 음성 보호 믹싱을 담당합니다.
- **절대 규칙**:
  - **수급 순서**: 로컬 `sfx_catalog.json` (`score >= 0.45`) → Freesound APIv2 (`FREESOUND_API_KEY` 존재 시) → ElevenLabs Sound Generation (`ELEVENLABS_API_KEY` 존재 시) → 로컬 소프트 폴백 (`score >= 0.22`). API 키가 없어도 파이프라인이 절대 중단되면 안 됩니다.
  - **자산화**: 외부 API로 다운로드·생성한 파일은 즉시 `<LOCAL_SFX_DIR>/_auto_acquired/`에 저장하고 `sfx_catalog.json`에 자동 편입합니다.
  - **음성 덕킹**: 대사/나레이션 활성 구간(`overlaps_voice=true`)에서는 효과음을 음성 기준 레벨 대비 **`-12dB ~ -18dB`** 로 감쇠합니다.

### Agent 5: Multimodal QA Reviewer (`.agents/skills/shorts-qa`)
- **역할**: 최종 렌더링된 `final.mp4`와 `sfx_cue_sheet.json`을 대조 검수합니다.
- **검수 체크리스트**:
  - [ ] 영상 총 길이가 60초 이내(권장 32~45초)이며 해상도가 `1080x1920` (`30fps`)인가?
  - [ ] 주요 피사체가 좌측 순위표에 가리지 않고 중앙/우측에 잘 잡혔는가 (`preview` 프레임 점검)?
  - [ ] 효과음 타격 정점이 화면 전환·자막 출현 시각과 어긋나지 않는가?
  - [ ] 대사/나레이션 구간에서 효과음이 목소리를 덮지 않는가 (`target_db_relative_to_voice`가 `-12.0 ~ -18.0 dB` 이내인가)?
  - [ ] CC BY 원본 클립 출처(`credits.txt` / `upload.txt`)가 누락 없이 생성되었는가?
