"""완전 자동 효과음 디렉터 파이프라인 단위·통합 검증 테스트.

실행:
  python -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from rank_short.sfx_director.catalog import build_or_update_catalog, load_catalog
from rank_short.sfx_director.config import SfxDirectorConfig
from rank_short.sfx_director.cue_sheet import generate_sfx_cue_sheet
from rank_short.sfx_director.matcher_fallback import match_and_procure_cues
from rank_short.sfx_director.mixer import (
    analyze_voice_activity,
    compute_ducked_sfx_schedule,
    mix_sfx_into_video,
)
from rank_short.sfx_director.schemas import SfxCueItem, SfxCueSheet


def _write_wav(path: Path, samples: np.ndarray, sr: int = 24000) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    clipped = np.clip(samples, -0.98, 0.98)
    pcm16 = (clipped * 32767.0).astype(np.int16)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(pcm16.tobytes())
    return path.read_bytes()


def _synth_whoosh(sr: int = 24000, dur: float = 0.50, peak_at: float = 0.24) -> np.ndarray:
    t = np.linspace(0, dur, int(sr * dur), endpoint=False)
    rng = np.random.default_rng(42)
    noise = rng.normal(0, 0.35, size=t.size)
    env = np.exp(-((t - peak_at) ** 2) / (2 * 0.045 ** 2))
    sweep = 0.45 * np.sin(2 * np.pi * (220 + 900 * (t / dur)) * t)
    return (noise + sweep) * env * 0.85


def _synth_hit(sr: int = 24000, dur: float = 0.45, freq: float = 95.0) -> np.ndarray:
    t = np.linspace(0, dur, int(sr * dur), endpoint=False)
    env = np.exp(-t * 11.0)
    env[:48] *= np.linspace(0, 1, 48)
    return 0.85 * np.sin(2 * np.pi * freq * t) * env


def _synth_ambience(sr: int = 24000, dur: float = 3.2) -> np.ndarray:
    t = np.linspace(0, dur, int(sr * dur), endpoint=False)
    return 0.18 * (np.sin(2 * np.pi * 180 * t) + 0.5 * np.sin(2 * np.pi * 310 * t))


def _make_test_clip(path: Path, dur: float = 3.6, with_speech: bool = True, color: str = "0x1e3a8a") -> None:
    """FFmpeg lavfi로 테스트용 720p 클립 생성 (with_speech=True 면 0.6~2.4초 구간에 650Hz 음성 대역 신호 삽입)."""
    if with_speech:
        af = "volume='if(between(t,0.6,2.4),0.45,0.002)':eval=frame"
        audio_src = f"sine=frequency=650:sample_rate=48000:duration={dur}"
        cmd = [
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i", f"testsrc2=size=1280x720:rate=30:duration={dur}",
            "-f", "lavfi", "-i", audio_src,
            "-af", af,
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-ar", "48000", "-ac", "2",
            str(path),
        ]
    else:
        cmd = [
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i", f"color=c={color}:s=1280x720:r=30:d={dur}",
            "-f", "lavfi", "-i", f"anullsrc=r=48000:cl=stereo:d={dur}",
            "-shortest",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-ar", "48000", "-ac", "2",
            str(path),
        ]
    subprocess.run(cmd, check=True)


class TestSfxDirectorPipeline(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_obj = tempfile.TemporaryDirectory(prefix="sfx_test_")
        self.tmp = Path(self.tmp_obj.name)
        self.sfx_dir = self.tmp / "local_sfx"
        self.sfx_dir.mkdir(parents=True, exist_ok=True)

        # 기본 로컬 효과음 세트 생성 (make_rank.py 기본 전환음 이름 포함)
        _write_wav(self.sfx_dir / "190_훅.wav", _synth_whoosh(peak_at=0.24))
        _write_wav(self.sfx_dir / "009_쉭.wav", _synth_whoosh(peak_at=0.18))
        _write_wav(self.sfx_dir / "007_쿵.wav", _synth_hit(freq=85.0))
        _write_wav(self.sfx_dir / "008_팝.wav", _synth_hit(dur=0.22, freq=520.0))
        _write_wav(self.sfx_dir / "144_띠용.wav", _synth_hit(dur=0.40, freq=380.0))
        _write_wav(self.sfx_dir / "350_파도 소리.wav", _synth_ambience(dur=3.2))

        self.cfg = SfxDirectorConfig(
            auto_sfx_enabled=True,
            local_sfx_dir=self.sfx_dir,
            catalog_path=self.sfx_dir / "sfx_catalog.json",
            gemini_api_key="",  # 오프라인 결정론적 테스트
            fallback_mode="auto",
        )

    def tearDown(self) -> None:
        self.tmp_obj.cleanup()

    def test_01_incremental_catalog_and_peak_detection(self) -> None:
        # 1차 인덱싱: 6개 전부 신규 분석
        cat1, st1 = build_or_update_catalog(cfg=self.cfg)
        self.assertEqual(st1["newly_analyzed"], 6)
        self.assertEqual(st1["reused"], 0)
        self.assertEqual(cat1.total_count, 6)

        # 스우시(190_훅.wav)의 실측 정점이 설정한 ~0.24초(±0.03초) 근처인지 검증
        whoosh_item = next(it for it in cat1.items if it.filename == "190_훅.wav")
        self.assertEqual(whoosh_item.category, "whoosh")
        self.assertAlmostEqual(whoosh_item.peak_time_sec, 0.24, delta=0.04)

        # 2차 인덱싱(변경 없음): 6개 전부 캐시 재사용, 신규 분석 0개
        _, st2 = build_or_update_catalog(cfg=self.cfg)
        self.assertEqual(st2["newly_analyzed"], 0)
        self.assertEqual(st2["reused"], 6)

        # 3차 인덱싱(새 파일 1개 추가): 추가된 1개만 증분 분석
        _write_wav(self.sfx_dir / "026_뜨헉!.wav", _synth_hit(dur=0.35, freq=440.0))
        cat3, st3 = build_or_update_catalog(cfg=self.cfg)
        self.assertEqual(st3["newly_analyzed"], 1)
        self.assertEqual(st3["reused"], 6)
        self.assertEqual(cat3.total_count, 7)

    def test_02_hybrid_matching_and_freesound_elevenlabs_fallback(self) -> None:
        cat, _ = build_or_update_catalog(cfg=self.cfg)

        # 로컬 매칭 성공 큐 1개 + 로컬에 전혀 없는 특수 사운드 큐 1개 구성
        sheet = SfxCueSheet(
            video_path=str(self.tmp / "dummy.mp4"),
            video_duration_sec=10.0,
            created_at="2026-11-13T00:00:00Z",
            director_model="test",
            video_summary_ko="테스트",
            hook_analysis_ko="테스트",
            cues=[
                SfxCueItem(
                    cue_id="cue_001",
                    timestamp_str="00:01.35",
                    timestamp_sec=1.35,
                    frame_index=40,
                    trigger_type="hook_3s",
                    category="whoosh",
                    sound_concept_ko="빠른 화면 전환 스우시",
                    search_keywords_ko=["훅", "휙"],
                    search_keywords_en=["whoosh"],
                    generation_prompt_en="Fast whoosh transition",
                    visual_reason_ko="전환 테스트",
                    align_to_peak=True,
                ),
                SfxCueItem(
                    cue_id="cue_002",
                    timestamp_str="00:04.20",
                    timestamp_sec=4.20,
                    frame_index=126,
                    trigger_type="action_impact",
                    category="vehicle_horn",
                    sound_concept_ko="급브레이크 경적 소리",
                    search_keywords_ko=["급브레이크", "사이렌"],
                    search_keywords_en=["car horn screech"],
                    generation_prompt_en="Loud car horn and tire screech",
                    visual_reason_ko="돌발 차량 회피",
                    align_to_peak=True,
                ),
            ],
        )

        # A. API 키 없는 local_only 상태 검증 -> 멈추지 않고 안전 처리
        self.cfg.freesound_api_key = ""
        self.cfg.elevenlabs_api_key = ""
        sheet_local, _, stats_local = match_and_procure_cues(sheet.model_copy(deep=True), cfg=self.cfg, catalog=cat.model_copy(deep=True))
        self.assertEqual(stats_local["local_matched"], 1)
        self.assertEqual(sheet_local.cues[0].matched_source, "local_catalog")

        # B. Freesound API 키 활성화(Mock) 검증 -> 자동 다운로드 후 _auto_acquired 저장 및 카탈로그 영구 편입
        synth_horn_bytes = _write_wav(self.tmp / "mock_horn.wav", _synth_hit(dur=0.6, freq=700.0))
        self.cfg.freesound_api_key = "test_fs_token"

        mock_search_resp = MagicMock(status_code=200)
        mock_search_resp.json.return_value = {
            "results": [
                {
                    "id": 99887,
                    "name": "car_horn_screech",
                    "avg_rating": 4.8,
                    "num_downloads": 1200,
                    "license": "CC0",
                    "username": "soundmaker",
                    "url": "https://freesound.org/s/99887/",
                    "previews": {"preview-hq-mp3": "https://cdn.freesound.org/previews/99887.mp3"},
                }
            ]
        }
        mock_dl_resp = MagicMock(status_code=200, content=synth_horn_bytes)

        with patch("requests.get", side_effect=[mock_search_resp, mock_dl_resp]):
            sheet_fs, updated_cat, stats_fs = match_and_procure_cues(sheet.model_copy(deep=True), cfg=self.cfg, catalog=cat.model_copy(deep=True))

        self.assertEqual(stats_fs["local_matched"], 1)
        self.assertEqual(stats_fs["freesound_acquired"], 1)
        self.assertEqual(sheet_fs.cues[1].matched_source, "freesound")
        self.assertTrue(Path(sheet_fs.cues[1].matched_file).is_file())
        self.assertEqual(len(updated_cat.items), len(cat.items) + 1)

        # C. 이제 카탈로그에 등록되었으므로 다음 실행에서는 외부 API 호출 없이 로컬 1순위로 즉시 매칭!
        self.cfg.freesound_api_key = ""
        _, _, stats_reused = match_and_procure_cues(sheet.model_copy(deep=True), cfg=self.cfg, catalog=updated_cat)
        self.assertEqual(stats_reused["local_matched"], 2)

    def test_03_voice_ducking_peak_alignment_and_end_to_end_make_rank_render(self) -> None:
        ep_dir = self.tmp / "ep_test"
        raw_dir = ep_dir / "raw"
        raw_dir.mkdir(parents=True, exist_ok=True)

        # TOP 3 클립 생성: 3위(음성 포함), 2위(무음 3.6초 -> ambience 유도), 1위(음성 포함)
        _make_test_clip(raw_dir / "c01.mp4", dur=3.8, with_speech=True)
        _make_test_clip(raw_dir / "c02.mp4", dur=3.8, with_speech=False, color="0x0f172a")
        _make_test_clip(raw_dir / "c03.mp4", dur=4.0, with_speech=True, color="0x3b0764")

        plan = {
            "title": "테스트 [랭킹] 모음",
            "sfx_dir": str(self.sfx_dir),
            "items": [
                {
                    "rank": 3,
                    "label": "초반후킹",
                    "src": "raw/c01.mp4",
                    "start": 0.0,
                    "end": 3.6,
                    "subs": [
                        {"at": 0.15, "dur": 1.0, "text": "(시작 상황)", "anim": "drop"},
                        {"at": 1.20, "dur": 1.2, "text": "[r:대참사!!]", "anim": "zoom"},
                    ],
                },
                {
                    "rank": 2,
                    "label": "정적반전",
                    "src": "raw/c02.mp4",
                    "start": 0.0,
                    "end": 3.6,
                    "volume": 0.0,
                    "subs": [
                        {"at": 1.50, "dur": 1.2, "text": "어..?", "anim": "shake"},
                    ],
                },
                {
                    "rank": 1,
                    "label": "레전드",
                    "src": "raw/c03.mp4",
                    "start": 0.0,
                    "end": 3.8,
                    "slow": [1.0, 2.0, 1.5],
                    "subs": [
                        {"at": 2.80, "dur": 1.2, "text": "([y:천운] ON)", "anim": "zoom"},
                    ],
                },
            ],
        }
        plan_path = ep_dir / "plan.json"
        plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")

        env = {
            **os.environ,
            "LOCAL_SFX_DIR": str(self.sfx_dir),
            "AUTO_SFX_ENABLED": "false",
        }

        # 1) 기존 모드(--no-auto-sfx / AUTO_SFX_ENABLED=false) 렌더링 호환성 검증
        out_legacy = ep_dir / "final_legacy.mp4"
        r1 = subprocess.run(
            [sys.executable, str(REPO_ROOT / "rank_short/make_rank.py"), "render", str(plan_path), "--out", str(out_legacy)],
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(r1.returncode, 0, msg=r1.stderr + "\n" + r1.stdout)
        self.assertTrue(out_legacy.is_file())
        self.assertFalse((ep_dir / "sfx_cue_sheet.json").exists())

        # 2) 자동 효과음 디렉터 활성화(--auto-sfx) 통합 렌더링 검증
        out_auto = ep_dir / "final_auto.mp4"
        r2 = subprocess.run(
            [
                sys.executable,
                str(REPO_ROOT / "rank_short/make_rank.py"),
                "render",
                str(plan_path),
                "--auto-sfx",
                "--out",
                str(out_auto),
            ],
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(r2.returncode, 0, msg=r2.stderr + "\n" + r2.stdout)
        self.assertTrue(out_auto.is_file())
        cue_file = ep_dir / "sfx_cue_sheet.json"
        self.assertTrue(cue_file.is_file())

        saved_sheet = SfxCueSheet.model_validate_json(cue_file.read_text(encoding="utf-8"))
        self.assertGreaterEqual(len(saved_sheet.cues), 3)

        # 음성 구간 중첩 효과음의 덕킹 범위(-18.0dB ~ -12.0dB) 및 피크 정렬 검증
        ducked_cues = [c for c in saved_sheet.cues if c.overlaps_voice and c.target_db_relative_to_voice is not None]
        self.assertGreaterEqual(len(ducked_cues), 1)
        for c in ducked_cues:
            self.assertGreaterEqual(c.target_db_relative_to_voice, -18.0)
            self.assertLessEqual(c.target_db_relative_to_voice, -12.0)

        for c in saved_sheet.cues:
            if c.matched_file and c.align_to_peak:
                expected_start = round(max(0.0, c.timestamp_sec - c.sfx_peak_time_sec), 4)
                self.assertAlmostEqual(c.playback_start_sec, expected_start, places=3)

        # 3) 단독 믹서(mix_sfx_into_video) 출력 해상도/스트림 검증
        standalone_out = ep_dir / "standalone_mixed.mp4"
        mix_info = mix_sfx_into_video(out_legacy, saved_sheet, standalone_out, cfg=self.cfg)
        self.assertTrue(standalone_out.is_file())
        self.assertGreater(mix_info["mixed_sfx_count"], 0)


if __name__ == "__main__":
    unittest.main()
