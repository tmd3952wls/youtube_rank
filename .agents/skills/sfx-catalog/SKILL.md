---
name: sfx-catalog
description: LOCAL_SFX_DIR 로컬 효과음 폴더를 스캔하여 새로 추가되거나 변경된 오디오 파일만 Gemini 멀티모달 + FFmpeg DSP로 증분 청취해 sfx_catalog.json을 갱신합니다.
---
# `/sfx-catalog` — 로컬 효과음 증분 카탈로그화

1. 먼저 현재 환경 설정과 폴더 연결 상태를 확인합니다:
   ```bash
   python -m rank_short.sfx_director status
   ```
2. 신규/변경된 오디오 파일만 증분 인덱싱합니다 (중단되어도 20개 단위 체크포인트로 이어하기 가능):
   ```bash
   python -m rank_short.sfx_director catalog
   ```
   - 초기 2,869개 대량 인덱싱을 배치로 나누어 실행하려면 `--limit 200` 옵션을 추가합니다.
   - API 키 없이 먼저 파일명+DSP로 인덱싱된 항목을 Gemini 청취로 업그레이드하려면 `--upgrade-heuristics`를 사용합니다.
3. 완료 후 생성된 `sfx_catalog.json`의 카테고리별 분포(`whoosh`, `impact`, `pop_ui`, `comedy_cartoon`, `voice_reaction`, `ambience` 등)를 요약해 보고합니다.
