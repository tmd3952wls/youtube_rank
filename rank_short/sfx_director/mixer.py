"""[Step 5] 음성 볼륨 밸런싱(덕킹 -12dB ~ -18dB), 피크 타이밍 정밀 정렬 및 FFmpeg 자동 믹싱 모듈.

주요 특징:
- 초안 영상의 음성 트랙(원본 대사 `[0:a]` 또는 별도 TTS 나레이션 파일)에서 300Hz~3400Hz 음성 대역 에너지 기반 VAD(음성 활성 구간) 및 기준 음성 레벨(dBFS) 자동 측정
- 나레이션/대사가 나오는 구간의 효과음은 음성 기준 레벨 대비 `-12dB ~ -18dB`(강도 `intensity` 비례 보간)로 자동 감쇠시켜 목소리 마스킹 원천 차단
- 대사가 없는 구간(암전 전환·펀치라인 정적)에서는 선명한 목표 피크(`sfx_idle_peak_db`, 기본 -8.5dBFS)로 재생
- 각 효과음의 실측 타격 정점(`sfx_peak_time_sec`)을 큐시트 타임스탬프에 맞춰 재생 시작 시각(`timestamp_sec - peak_time_sec`) 및 선행 트림(`atrim`)을 밀리초 단위로 정렬
- 두 가지 통합 경로 모두 지원:
  1) `make_rank.py` 내부 렌더링 파이프라인에 `sfx_events` 튜플로 직접 주입
  2) 임의의 초안 `.mp4` + `sfx_cue_sheet.json`을 받아 EBU R128 2-pass `loudnorm`으로 최종 `.mp4`를 독립 합성
"""
from __future__ import annotations

import json
import math
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np

from .catalog import analyze_waveform_dsp
from .config import SfxDirectorConfig, load_director_config
from .cue_sheet import probe_video_meta
from .schemas import SfxCueItem, SfxCueSheet


@dataclass
class VoiceProfile:
    """초안 영상(또는 나레이션 트랙)의 음성 활성 구간 및 기준 음량 정보."""

    has_voice: bool
    speech_ref_dbfs: float
    noise_floor_dbfs: float
    frame_sec: float
    voice_mask: np.ndarray  # bool array per frame_sec
    frame_dbfs: np.ndarray  # float array per frame_sec

    def is_voice_active(self, start_sec: float, end_sec: float) -> tuple[bool, float]:
        """지정 시간 구간 [start_sec, end_sec] 내 음성 활성 여부와 해당 구간 상위 음성 dBFS 반환."""
        if not self.has_voice or self.voice_mask.size == 0:
            return False, self.speech_ref_dbfs
        i0 = max(0, int(math.floor(start_sec / self.frame_sec)))
        i1 = min(self.voice_mask.size, max(i0 + 1, int(math.ceil(end_sec / self.frame_sec))))
        if i0 >= self.voice_mask.size:
            return False, self.speech_ref_dbfs
        seg_mask = self.voice_mask[i0:i1]
        if not np.any(seg_mask):
            return False, self.speech_ref_dbfs
        active_dbs = self.frame_dbfs[i0:i1][seg_mask]
        local_voice_db = float(np.percentile(active_dbs, 80)) if active_dbs.size > 0 else self.speech_ref_dbfs
        return True, local_voice_db


def analyze_voice_activity(
    media_path: Path,
    cfg: Optional[SfxDirectorConfig] = None,
    narration_override_path: Optional[Path] = None,
    frame_sec: float = 0.025,
) -> VoiceProfile:
    """300Hz~3400Hz 음성 주파수 대역 필터링 + 단구간 RMS로 사람 목소리(TTS/대사) 구간과 음량을 검출."""
    cfg = cfg or load_director_config()
    target = Path(narration_override_path) if narration_override_path else Path(media_path)

    sr = 16000
    cmd = [
        "ffmpeg", "-v", "error", "-i", str(target),
        "-vn", "-ac", "1", "-ar", str(sr),
        "-af", "highpass=f=280,lowpass=f=3600",
        "-f", "f32le", "-",
    ]
    r = subprocess.run(cmd, capture_output=True)
    samples = np.frombuffer(r.stdout, dtype=np.float32) if r.returncode == 0 and r.stdout else np.array([], dtype=np.float32)

    hop = max(1, int(sr * frame_sec))
    if samples.size < hop * 2:
        return VoiceProfile(
            has_voice=False,
            speech_ref_dbfs=-18.0,
            noise_floor_dbfs=-60.0,
            frame_sec=frame_sec,
            voice_mask=np.zeros(0, dtype=bool),
            frame_dbfs=np.zeros(0, dtype=np.float32),
        )

    n_frames = samples.size // hop
    frames = samples[: n_frames * hop].reshape(n_frames, hop)
    rms = np.sqrt(np.mean(frames ** 2, axis=1) + 1e-12)
    db = 20.0 * np.log10(rms)

    # 배경 노이즈 바닥(하위 25 퍼센타일, 최소 -52dBFS 캡) 추정
    noise_floor = max(-52.0, float(np.percentile(db, 25)))
    threshold = max(-36.0, noise_floor + cfg.voice_vad_margin_db)
    raw_mask = db >= threshold

    # 짧은 발음 끊김(150ms 이내) 메우기 (행오버 스무딩)
    pad_frames = max(1, int(round(0.12 / frame_sec)))
    kernel = np.ones(pad_frames * 2 + 1, dtype=np.int32)
    smoothed_mask = np.convolve(raw_mask.astype(np.int32), kernel, mode="same") > 0

    if not np.any(smoothed_mask):
        return VoiceProfile(
            has_voice=False,
            speech_ref_dbfs=-18.0,
            noise_floor_dbfs=round(noise_floor, 2),
            frame_sec=frame_sec,
            voice_mask=smoothed_mask,
            frame_dbfs=db.astype(np.float32),
        )

    speech_ref = float(np.percentile(db[smoothed_mask], 75))
    speech_ref = min(-6.0, max(-30.0, speech_ref))
    return VoiceProfile(
        has_voice=True,
        speech_ref_dbfs=round(speech_ref, 2),
        noise_floor_dbfs=round(noise_floor, 2),
        frame_sec=frame_sec,
        voice_mask=smoothed_mask,
        frame_dbfs=db.astype(np.float32),
    )


def compute_ducked_sfx_schedule(
    cue_sheet: SfxCueSheet,
    voice_profile: VoiceProfile,
    cfg: Optional[SfxDirectorConfig] = None,
) -> list[dict]:
    """각 매칭된 큐에 대해 피크 정렬 시각과 음성 덕킹(-12dB ~ -18dB)이 반영된 선형 볼륨 게인을 계산."""
    cfg = cfg or load_director_config()
    scheduled: list[dict] = []

    for cue in cue_sheet.cues:
        if not cue.matched_file:
            continue
        sfx_path = Path(cue.matched_file)
        if not sfx_path.is_file():
            continue

        dsp = analyze_waveform_dsp(sfx_path)
        sfx_dur = float(dsp["duration_sec"])
        sfx_peak_db = float(dsp["peak_dbfs"])
        sfx_rms_db = float(dsp["rms_dbfs"])
        peak_t = float(dsp["peak_time_sec"]) if cue.align_to_peak else 0.0
        cue.sfx_peak_time_sec = round(peak_t, 4)

        # 피크 타이밍 정렬: 목표 타임스탬프에 정확히 타격 정점이 꽂히도록 시작 시각 역산
        raw_start = cue.timestamp_sec - peak_t
        src_trim_start = max(0.0, -raw_start)
        play_start = max(0.0, raw_start)
        effective_dur = min(max(0.15, sfx_dur - src_trim_start), cue.max_duration_sec)
        play_end = min(cue_sheet.video_duration_sec, play_start + effective_dur)

        # 음성/나레이션 중첩 검사 (±80ms 마진 포함)
        overlaps_voice, local_voice_db = voice_profile.is_voice_active(
            max(0.0, play_start - 0.08),
            play_end + 0.08,
        )
        cue.overlaps_voice = overlaps_voice
        cue.playback_start_sec = round(play_start, 4)

        # intensity (0.2 ~ 1.2) 를 [0, 1] 로 정규화하여 -18dB ~ -12dB 사이로 선형 매핑
        norm_int = min(1.0, max(0.0, (cue.intensity - 0.2) / 0.8))
        if overlaps_voice:
            # 예: sfx_duck_db_max=-18.0, sfx_duck_db_min=-12.0
            rel_db = cfg.sfx_duck_db_max + norm_int * (cfg.sfx_duck_db_min - cfg.sfx_duck_db_max)
            # 환경음(ambience)은 항상 가장 낮은 덕킹 하한(-18dB) 유지
            if cue.category == "ambience":
                rel_db = cfg.sfx_duck_db_max
            cue.target_db_relative_to_voice = round(rel_db, 2)
            target_level_db = local_voice_db + rel_db
        else:
            cue.target_db_relative_to_voice = None
            # 비음성 구간: 기본 피크(-8.5dBFS)에서 강도에 따라 -6dB ~ +1.5dB 범위 조절
            target_level_db = cfg.sfx_idle_peak_db - (1.0 - norm_int) * 6.0
            if cue.category == "ambience":
                target_level_db = -24.0

        # 단발성 효과음은 정점(peak_dbfs) 기준, 지속형 환경음은 RMS(rms_dbfs) 기준으로 게인 산출
        measured_ref_db = sfx_rms_db if cue.category == "ambience" else sfx_peak_db
        gain_db = target_level_db - measured_ref_db
        # 과증폭 클리핑 방지 (+14dB 상한)
        gain_db = min(14.0, max(-42.0, gain_db))
        linear_gain = round(10.0 ** (gain_db / 20.0), 5)
        cue.computed_linear_gain = linear_gain

        scheduled.append(
            {
                "cue_id": cue.cue_id,
                "path": sfx_path,
                "raw_start_sec": raw_start,
                "play_start_sec": play_start,
                "src_trim_start_sec": src_trim_start,
                "duration_sec": round(effective_dur, 3),
                "linear_gain": linear_gain,
                "gain_db": round(gain_db, 2),
                "target_level_db": round(target_level_db, 2),
                "overlaps_voice": overlaps_voice,
                "rel_to_voice_db": cue.target_db_relative_to_voice,
                "category": cue.category,
                "name": sfx_path.stem,
            }
        )

    return scheduled


def to_make_rank_events(scheduled: list[dict]) -> list[tuple]:
    """`make_rank.py` 의 `sfx_events` 포맷 `(path, at, gain, name, dur, src_off, fade_in, kind)` 으로 변환."""
    out = []
    for ev in scheduled:
        fade_in = 0.08 if ev["category"] == "ambience" else 0.01
        out.append(
            (
                ev["path"],
                float(ev["raw_start_sec"]),
                float(ev["linear_gain"]),
                f"[AI]{ev['name']}",
                float(ev["duration_sec"]),
                0.0,
                fade_in,
                "auto_sfx",
            )
        )
    return out


def mix_sfx_into_video(
    draft_video_path: Path,
    cue_sheet: SfxCueSheet,
    output_video_path: Path,
    cfg: Optional[SfxDirectorConfig] = None,
    narration_audio_path: Optional[Path] = None,
) -> dict:
    """독립 실행 모드: 초안 MP4 영상(+선택적 TTS 나레이션)에 큐시트 효과음을 덕킹·피크정렬·2-pass loudnorm 합성."""
    cfg = cfg or load_director_config()
    draft_video_path = Path(draft_video_path).resolve()
    output_video_path = Path(output_video_path).resolve()
    output_video_path.parent.mkdir(parents=True, exist_ok=True)

    meta = probe_video_meta(draft_video_path)
    total_dur = float(meta["duration"])
    has_orig_audio = bool(meta["has_audio"])

    voice_prof = analyze_voice_activity(draft_video_path, cfg, narration_override_path=narration_audio_path)
    scheduled = compute_ducked_sfx_schedule(cue_sheet, voice_prof, cfg)

    cmd_base = ["ffmpeg", "-y", "-v", "error", "-i", str(draft_video_path)]
    fc: list[str] = []
    voice_bus_inputs: list[str] = []
    input_idx = 1

    if has_orig_audio:
        fc.append("[0:a]aresample=48000,aformat=channel_layouts=stereo[orig_a]")
        voice_bus_inputs.append("[orig_a]")
    else:
        fc.append(f"anullsrc=r=48000:cl=stereo,atrim=0:{total_dur:.3f}[orig_a]")
        voice_bus_inputs.append("[orig_a]")

    if narration_audio_path and Path(narration_audio_path).is_file():
        cmd_base += ["-i", str(narration_audio_path)]
        fc.append(
            f"[{input_idx}:a]aresample=48000,aformat=channel_layouts=stereo,"
            f"apad,atrim=0:{total_dur:.3f}[tts_a]"
        )
        voice_bus_inputs.append("[tts_a]")
        input_idx += 1

    if len(voice_bus_inputs) == 1:
        fc.append(f"{voice_bus_inputs[0]}anull[voice_bus]")
    else:
        fc.append(
            f"{''.join(voice_bus_inputs)}amix=inputs={len(voice_bus_inputs)}:"
            f"duration=first:normalize=0[voice_bus]"
        )

    sfx_labels: list[str] = []
    for j, ev in enumerate(scheduled):
        cmd_base += ["-i", str(ev["path"])]
        s0 = float(ev["src_trim_start_sec"])
        dur = float(ev["duration_sec"])
        delay_ms = int(round(float(ev["play_start_sec"]) * 1000))
        fade_in = 0.06 if ev["category"] == "ambience" else 0.01
        fade_out_st = max(0.0, dur - min(0.25, dur * 0.3))
        fade_out_d = min(0.25, dur * 0.3)

        fc.append(
            f"[{input_idx}:a]atrim=start={s0:.3f}:end={s0 + dur:.3f},asetpts=PTS-STARTPTS,"
            f"afade=t=in:d={fade_in:.3f},afade=t=out:st={fade_out_st:.3f}:d={fade_out_d:.3f},"
            f"aresample=48000,aformat=channel_layouts=stereo,"
            f"volume={ev['linear_gain']:.5f},adelay={delay_ms}|{delay_ms}[sfx_{j}]"
        )
        sfx_labels.append(f"[sfx_{j}]")
        input_idx += 1

    if not sfx_labels:
        # 매칭된 효과음이 하나도 없더라도 무중단으로 원본/나레이션 오디오 정규화 출력
        final_mix_expr = "[voice_bus]anull[pre_ln]"
        fc.append(final_mix_expr)
    else:
        fc.append(
            f"{''.join(sfx_labels)}amix=inputs={len(sfx_labels)}:"
            f"duration=longest:normalize=0,apad,atrim=0:{total_dur:.3f}[sfx_bus_raw]"
        )
        if cfg.use_sidechain_ducking and voice_prof.has_voice:
            # 실시간 사이드체인 압축 추가 안전망 (음성이 갑자기 치고 들어올 때 지속형 SFX 추가 4~6dB 억제)
            fc.append("[voice_bus]asplit=2[vb_main][vb_sc]")
            fc.append(
                "[sfx_bus_raw][vb_sc]sidechaincompress="
                "threshold=0.04:ratio=4:attack=15:release=220[sfx_bus]"
            )
            fc.append("[vb_main][sfx_bus]amix=inputs=2:duration=first:normalize=0[pre_ln]")
        else:
            fc.append("[voice_bus][sfx_bus_raw]amix=inputs=2:duration=first:normalize=0[pre_ln]")

    # 2-pass EBU R128 loudnorm (-14 LUFS, -1.5 dBTP)
    ln_base = f"loudnorm=I={cfg.target_loudness_lufs}:TP=-1.5:LRA=11"
    measure_cmd = [("info" if x == "error" else x) for x in cmd_base]
    r_m = subprocess.run(
        [
            *measure_cmd,
            "-filter_complex", ";".join([*fc, f"[pre_ln]{ln_base}:print_format=json[m]"]),
            "-map", "[m]", "-f", "null", "-",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if r_m.returncode == 0 and "{" in r_m.stderr and "}" in r_m.stderr:
        m_json = json.loads(r_m.stderr[r_m.stderr.rindex("{"): r_m.stderr.rindex("}") + 1])
        ln_full = (
            f"{ln_base}:measured_I={m_json['input_i']}:measured_TP={m_json['input_tp']}:"
            f"measured_LRA={m_json['input_lra']}:measured_thresh={m_json['input_thresh']}:"
            f"offset={m_json['target_offset']}:linear=true"
        )
    else:
        ln_full = ln_base

    render_cmd = [
        *cmd_base,
        "-filter_complex", ";".join([*fc, f"[pre_ln]{ln_full},aresample=48000,aformat=sample_rates=48000:channel_layouts=stereo[aout]"]),
        "-map", "0:v", "-map", "[aout]",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
        "-movflags", "+faststart",
        str(output_video_path),
    ]
    r_out = subprocess.run(render_cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r_out.returncode != 0:
        raise RuntimeError(f"FFmpeg 자동 효과음 믹싱 실패: {r_out.stderr[-1500:]}")

    ducked_count = sum(1 for s in scheduled if s["overlaps_voice"])
    print(
        f"[SFX Mixer] 믹싱 완료: 총 {len(scheduled)}개 효과음 합성 "
        f"(음성 구간 -12~-18dB 덕킹 적용 {ducked_count}개) → {output_video_path}"
    )
    return {
        "output_video": str(output_video_path),
        "mixed_sfx_count": len(scheduled),
        "voice_ducked_count": ducked_count,
        "voice_detected": voice_prof.has_voice,
        "speech_ref_dbfs": voice_prof.speech_ref_dbfs,
        "schedule": scheduled,
    }
