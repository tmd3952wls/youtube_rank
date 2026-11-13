# GEMINI.md — `youtube_rank` Antigravity(`agy`) & Gemini 멀티모달 워크스페이스 지침서

본 저장소(`youtube_rank`)는 1080×1920 랭킹형 유튜브 쇼츠(`TOP 3~7`) 자동 제작 파이프라인에 **Gemini 멀티모달 기반 완전 자동 효과음(SFX) 디렉터(`rank_short/sfx_director/`)**를 결합한 프로젝트입니다. [Google Antigravity CLI (`agy`)](https://antigravity.google/docs/cli/best-practices/)는 시작 시 본 문서와 루트의 `AGENTS.md`를 자동으로 로드합니다.

## 1. 아키텍처 및 디렉토리 표준

- `rank_short/find_cc.py` — `yt-dlp` 기반 유튜브 CC BY 원본 클립 검색(`search`), 최고 화질 다운로드(`get`), 출처 생성(`credits`). **모음집(Compilation) 채널 영상은 저작권 위험으로 사용 금지.**
- `rank_short/make_rank.py` — Pillow + NumPy + FFmpeg 기반 랭킹 쇼츠 렌더러 (`init`, `draft`, `preview`, `render`).
  - 렌더링 내부에서 모든 클립·암전(`gap`)·자막(`subs`)이 무손실 결합된 `joined.mp4` 직후 지점(`cmd_render`)에 `sfx_director` 훅이 비침습적으로 연결되어 있습니다.
  - `AUTO_SFX_ENABLED=false` (기본값) 상태에서는 기존 수동 파이프라인과 100% 동일하게 동작합니다.
- `rank_short/sfx_director/` — **독립형 완전 자동 효과음 디렉터 패키지**:
  - `config.py`: `.env` / `secrets.env` / OS 환경변수 로더 및 설정 관리 (`AUTO_SFX_ENABLED`, `LOCAL_SFX_DIR` 등).
  - `catalog.py`: 로컬 효과음 폴더(`C:\Users\tmd39\Desktop\효과음\효과음2869개`)의 오디오를 FFmpeg/NumPy DSP(길이·5ms 피크 시각·dBFS) + [Google Gen AI SDK](https://googleapis.github.io/python-genai/) 멀티모달 청취로 분석해 `sfx_catalog.json`에 **증분(Incremental) 인덱싱**.
  - `cue_sheet.py`: 초안 렌더링 영상(`joined.mp4` 또는 `draft.mp4`)을 Gemini 멀티모달로 직접 시청해 **초반 3초 후킹(`hook_3s`)**, **컷 전환(`cut_transition`)**, **핵심 자막(`caption_emphasis`)**, **반전/액션(`mood_twist` / `action_impact`)**, **2초+ 무음 보강(`ambience_fill`)** 포인트를 초·프레임 단위 `sfx_cue_sheet.json`으로 생성.
  - `matcher_fallback.py`: 1순위 로컬 `sfx_catalog.json` 매칭 → 미달 시 2순위 [Freesound APIv2](https://freesound.org/docs/api/resources_apiv2.html) 검색/자동 다운로드 → 3순위 [ElevenLabs Sound Effects API](https://elevenlabs.io/docs/api-reference/text-to-sound-effects/convert) AI 생성 후 `_auto_acquired/` 저장 및 카탈로그 자동 등록.
  - `mixer.py`: 음성 대역(280~3600Hz) VAD로 대사/나레이션 구간 감지 후 **음성 대비 `-12dB ~ -18dB` 자동 볼륨 밸런싱(덕킹)** + **타격 정점(`peak_time_sec`) 밀리초 정렬** + **EBU R128 2-pass `loudnorm`(-14 LUFS)** 합성.
- `rank_short/send_mail.py` — 9.5MB 이하 2-pass 자동 압축 및 네이버 SMTP(`secrets.env`) 메일 발송.
- `.agents/skills/` — Antigravity CLI 전용 커스텀 슬래시 스킬 (`/sfx-catalog`, `/sfx-direct`, `/shorts-qa`).

## 2. 로컬 실행 및 Antigravity (`agy`) 핵심 명령어

### 초기 환경 준비
```powershell
copy .env.example .env
pip install -r requirements.txt
python -m rank_short.sfx_director status
```

### 단계별 CLI 실행
```powershell
# [Step 1] 로컬 2,869개 효과음 증분 청취·카탈로그화 (신규 추가 파일만 분석, 중간 중단 시 자동 이어하기)
python -m rank_short.sfx_director catalog

# 처음 2,869개를 나눠서 인덱싱하고 싶을 때:
python -m rank_short.sfx_director catalog --limit 300

# [Step 2] 기존 plan.json 렌더링 + 자동 효과음 디렉터 통합 실행 (.env 에 AUTO_SFX_ENABLED=true 또는 --auto-sfx)
python rank_short/make_rank.py render ep_water/plan.json --auto-sfx

# [Step 3] 이미 렌더링된 초안 MP4에 대해 단독으로 큐시트 생성 → 수급 → 덕킹 믹싱 실행
python -m rank_short.sfx_director run ep_water/draft.mp4 --out ep_water/final_sfx.mp4
```

### Antigravity (`agy`) 원샷/대화형 실행 예시
```powershell
# 파이프라인 상태 점검 및 카탈로그 증분 동기화
agy -p "로컬 효과음 카탈로그 상태를 확인하고 새로 추가된 오디오 파일만 증분 인덱싱해 줘."

# 특정 에피소드 자동 효과음 연출 + 시각/청각 검수
agy -p "@ep_water/plan.json 을 AUTO_SFX_ENABLED 모드로 렌더링하고 생성된 sfx_cue_sheet.json 의 초반 3초 후킹과 덕킹 수치를 검수해 줘."
```

## 3. 코딩 및 수정 원칙 (Guardrails)

1. **비침습성 유지**: `make_rank.py`의 기존 레이아웃·자막 애니메이션(`pop`/`shake`/`drop`/`zoom`)·모션 트래킹·슬로모션·`sting` 로직을 훼손하지 마세요. 모든 AI 효과음 로직은 `rank_short/sfx_director/` 내부에서 관리합니다.
2. **오디오 덕킹 & 피크 정렬 엄수**: 나레이션이나 원본 영상 대사가 존재하는 구간(`overlaps_voice=true`)에서는 효과음 목표 음량이 음성 기준 대비 반드시 `-12dB ~ -18dB` 범위에 위치해야 하며, 최종 출력은 2-pass `loudnorm`(`-14 LUFS`, `-1.5 dBTP`)을 통과해야 합니다.
3. **인코딩 및 경로 호환성**: Windows 한글 경로(`C:\Users\tmd39\Desktop\효과음\효과음2869개`)가 깨지지 않도록 모든 파일 입출력에 `pathlib.Path`와 `encoding="utf-8"`을 명시하세요.
4. **테스트 검증 루프**: 코드를 수정한 뒤에는 반드시 `python -m unittest discover -s tests -v`를 실행해 증분 인덱싱, 피크 타이밍 계산, 음성 덕킹, FFmpeg 렌더링 통합 테스트가 모두 통과하는지 확인하세요.
