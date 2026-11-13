---
name: shorts-qa
description: 렌더링 완료된 랭킹 쇼츠 MP4 영상과 sfx_cue_sheet.json, credits.txt를 멀티모달로 검수하여 해상도, 길이, 피사체 크롭, 음성 덕킹 밸런스, 저작권 출처를 점검합니다.
---
# `/shorts-qa` — 완성 쇼츠 영상 & 효과음 멀티모달 검수

1. `ffprobe`로 완성 영상의 스트림 규격을 검증합니다 (`1080x1920`, `30fps`, 총 길이 60초 이하, 오디오 `48000Hz stereo`):
   ```bash
   ffprobe -v error -show_entries stream=codec_type,width,height,r_frame_rate,sample_rate,channels:format=duration -of json <완성영상.mp4>
   ```
2. `sfx_cue_sheet.json`을 확인하여 다음을 점검합니다:
   - 초반 0~3초 구간에 시선 집중 후킹 효과음이 배치되었는가?
   - 대사/나레이션 중첩(`overlaps_voice: true`) 구간에서 모든 효과음이 `-12dB ~ -18dB` 범위로 덕킹되었는가?
   - 수급 실패로 누락된 필수 타격 포인트가 없는가?
3. 각 순위의 결정적 순간 프레임을 `python rank_short/make_rank.py preview <plan.json> --item <번호> --at <초>`로 추출해 주인공이 좌측 순위표에 가리지 않았는지 확인합니다.
4. `python rank_short/find_cc.py credits <plan.json>`을 실행해 CC BY 출처(`credits.txt`)가 최신 상태인지 확인합니다.
