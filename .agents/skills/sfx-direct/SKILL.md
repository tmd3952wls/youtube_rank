---
name: sfx-direct
description: 에피소드 plan.json 또는 초안 렌더링 MP4 영상을 Gemini 멀티모달로 시청하여 SFX 큐시트를 생성하고, 로컬/외부 수급 및 -12~-18dB 음성 덕킹 FFmpeg 믹싱을 수행합니다.
---
# `/sfx-direct` — 자동 효과음 연출·수급·믹싱 실행

대상 입력이 `plan.json`인지 초안 `.mp4`인지 확인하고 아래 절차를 실행합니다.

## A. `plan.json`에서 전체 렌더링 + 자동 효과음 연출을 한 번에 돌릴 때
```bash
python rank_short/make_rank.py render <에피소드폴더>/plan.json --auto-sfx
```

## B. 이미 만들어진 초안 영상(`draft.mp4`)에 효과음만 연출·합성할 때
```bash
python -m rank_short.sfx_director run <초안영상.mp4> --out <최종영상.mp4>
```

## 실행 후 보고 항목
- 생성된 `sfx_cue_sheet.json`을 열어 초반 3초 후킹(`hook_3s`), 컷 전환, 자막 동기화 큐 개수와 매칭 출처(`local_catalog` / `freesound` / `elevenlabs`) 통계를 요약합니다.
- `overlaps_voice: true` 구간에서 음성 대비 덕킹 레벨(`target_db_relative_to_voice`: `-12.0 ~ -18.0 dB`)이 정상 반영되었는지 확인합니다.
