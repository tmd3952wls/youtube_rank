"""[Step 3] 영상 시청 기반 '자동 효과음 연출(SFX 큐시트)' 생성 모듈 (sfx_cue_sheet.json).

주요 특징:
- 초안 렌더링된 쇼츠 영상(예: `make_rank.py`의 `joined.mp4` 또는 임의의 초안 `.mp4`)을 Gemini 멀티모달 File API로 업로드하여 직접 시청·청취
- FFmpeg 기반 프레임·오디오 사전 계측(장면 전환 시점, 무음 2초+ 구간, 원본 오디오 급등 피크) 및 `plan.json`의 순위·자막(`drop`/`pop`/`shake`/`zoom`) 타임라인을 함께 컨텍스트로 주입
- 초반 3초 후킹(`hook_3s`), 순위 전환(`cut_transition`), 핵심 자막 등장(`caption_emphasis`), 물리 타격(`action_impact`), 분위기 반전(`mood_twist`), 무음 보강(`ambience_fill`)을 초·프레임 단위(`00:01.35` / `1.35s` / `41f`)로 판별해 정형화된 JSON 큐시트(`SfxCueSheet`)로 출력
- 수동 지정된 `fx` 및 기본 전환음과의 중복 충돌 방지 가드레일 내장
"""
from __future__ import annotations

import datetime as dt
import json
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Optional

import numpy as np

from .config import SfxDirectorConfig, create_genai_client, is_auth_route_mismatch_error, load_director_config
from .schemas import (
    GeminiCueItemDraft,
    GeminiVideoDirectionResponse,
    SfxCueItem,
    SfxCueSheet,
)


def format_timestamp(sec: float) -> str:
    sec = max(0.0, float(sec))
    mins = int(sec // 60)
    rem = sec - mins * 60
    return f"{mins:02d}:{rem:05.2f}"


def parse_timestamp_str(ts: str) -> Optional[float]:
    m = re.match(r"^\s*(?:(\d+):)?(\d+(?:\.\d+)?)\s*$", ts or "")
    if not m:
        return None
    mins = int(m.group(1) or 0)
    secs = float(m.group(2))
    return round(mins * 60.0 + secs, 3)


def probe_video_meta(video_path: Path) -> dict[str, Any]:
    r = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "stream=codec_type,r_frame_rate,width,height:format=duration",
            "-of", "json", str(video_path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if r.returncode != 0:
        raise RuntimeError(f"ffprobe 실행 실패 ({video_path}): {r.stderr[-300:]}")
    d = json.loads(r.stdout)
    dur = float(d.get("format", {}).get("duration", 0.0))
    fps = 30.0
    has_audio = False
    for s in d.get("streams", []):
        if s.get("codec_type") == "video" and s.get("r_frame_rate"):
            num, _, den = str(s["r_frame_rate"]).partition("/")
            try:
                fps = round(float(num) / max(float(den or 1.0), 1e-6), 3)
            except ValueError:
                fps = 30.0
        elif s.get("codec_type") == "audio":
            has_audio = True
    return {"duration": dur, "fps": fps, "has_audio": has_audio}


def detect_scene_cuts_and_silence(video_path: Path, has_audio: bool) -> dict[str, Any]:
    """FFmpeg 필터로 시각적 컷 전환 시점과 2초 이상 오디오 정적 구간을 사전 검출."""
    cuts: list[float] = []
    r_sc = subprocess.run(
        [
            "ffmpeg", "-v", "info", "-i", str(video_path),
            "-vf", "select='gt(scene,0.32)',showinfo",
            "-an", "-f", "null", "-",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    for m in re.finditer(r"pts_time:([0-9.]+)", r_sc.stderr):
        t = round(float(m.group(1)), 2)
        if not cuts or (t - cuts[-1]) >= 0.35:
            cuts.append(t)

    quiet_windows: list[tuple[float, float]] = []
    audio_peaks: list[float] = []
    if has_audio:
        r_au = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", str(video_path), "-ac", "1", "-ar", "16000", "-f", "f32le", "-"],
            capture_output=True,
        )
        arr = np.frombuffer(r_au.stdout, dtype=np.float32)
        if arr.size > 1600:
            hop = 800  # 50ms
            n = arr.size // hop
            rms = np.sqrt(np.mean(arr[: n * hop].reshape(n, hop) ** 2, axis=1) + 1e-12)
            db = 20.0 * np.log10(rms)
            # 2초 이상 연속 정적 구간 (-42dBFS 미만)
            silent = db < -42.0
            run_start: Optional[int] = None
            for idx, is_s in enumerate(silent):
                if is_s and run_start is None:
                    run_start = idx
                elif not is_s and run_start is not None:
                    if (idx - run_start) * 0.05 >= 2.0:
                        quiet_windows.append((round(run_start * 0.05, 2), round(idx * 0.05, 2)))
                    run_start = None
            if run_start is not None and (n - run_start) * 0.05 >= 2.0:
                quiet_windows.append((round(run_start * 0.05, 2), round(n * 0.05, 2)))

            # 급격한 원본 사운드 타격 시점 상위 8개 추출
            med = float(np.median(db))
            jump = np.maximum(db - np.concatenate([[db[0]], db[:-1]]), 0.0)
            for idx in np.argsort(-jump):
                t = round(int(idx) * 0.05, 2)
                if db[idx] < med + 7.0:
                    break
                if all(abs(t - p) >= 0.7 for p in audio_peaks):
                    audio_peaks.append(t)
                if len(audio_peaks) >= 8:
                    break
            audio_peaks.sort()

    return {
        "scene_cuts": cuts[:25],
        "quiet_windows": quiet_windows[:10],
        "audio_peaks": audio_peaks,
    }


def build_plan_timeline_context(
    items: Optional[list[dict]] = None,
    starts: Optional[list[float]] = None,
    ends: Optional[list[float]] = None,
    cfg_dict: Optional[dict] = None,
) -> tuple[str, list[tuple[float, str]]]:
    """plan.json 의 클립 시작/종료, 순위, 자막(anim 포함), 기존 수동 효과음 타임라인을 요약 문자열과 중복방지 목록으로 반환."""
    if not items or not starts or not ends:
        return "제공된 plan.json 구조 메타데이터 없음 (순수 영상·오디오 멀티모달 분석 모드)", []

    lines: list[str] = []
    existing_events: list[tuple[float, str]] = []
    cfg_dict = cfg_dict or {}
    tr = cfg_dict.get("transition") or {}
    has_auto_tr = bool(tr.get("whoosh") or tr.get("hit"))

    if has_auto_tr:
        lines.append(
            "※ 참고: 렌더러 기본 순위 전환음(검은 화면 whoosh + 다음 클립 시작 hit)이 이미 활성화되어 있으므로, "
            "동일한 클립 경계 시각에 중복 whoosh/hit 를 넣지 말고 클립 내부 액션·자막·반전 포인트에 집중하세요."
        )
        for i in range(len(items)):
            if tr.get("whoosh") and (i > 0 or tr.get("whoosh_first", True)):
                pk = (ends[i - 1] + starts[i]) / 2.0 if i > 0 else 0.12
                existing_events.append((round(pk, 2), "whoosh"))
            if tr.get("hit") and i > 0:
                existing_events.append((round(starts[i], 2), "impact"))

    for i, it in enumerate(items):
        s_t, e_t = starts[i], ends[i]
        rk = it.get("rank", len(items) - i)
        lbl = it.get("label", "")
        lines.append(f"- [{format_timestamp(s_t)} ~ {format_timestamp(e_t)}] {rk}위 '{lbl}'")
        for sub in it.get("subs", []):
            abs_at = round(s_t + float(sub.get("at", 0.0)), 2)
            anim = sub.get("anim", cfg_dict.get("sub_anim", "pop"))
            lines.append(f"    · 자막 {format_timestamp(abs_at)} ({anim}): {sub.get('text', '')}")
        if it.get("slow"):
            sl = it["slow"]
            lines.append(f"    · 슬로모션 구간: 클립 내 {sl[0]}~{sl[1]}초 ({sl[2]}배)")
        for fx in it.get("fx", []):
            abs_fx = round(s_t + float(fx.get("at", 0.0)), 2)
            existing_events.append((abs_fx, str(fx.get("sfx", ""))))
            lines.append(f"    · 기존 수동 효과음 {format_timestamp(abs_fx)}: {fx.get('sfx')}")

    return "\n".join(lines), existing_events


def _direct_video_with_gemini(
    video_path: Path,
    meta: dict[str, Any],
    pre_signals: dict[str, Any],
    timeline_text: str,
    cfg: SfxDirectorConfig,
) -> GeminiVideoDirectionResponse:
    """Gemini 멀티모달 모델이 초안 영상을 직접 시청하고 타임스탬프별 SFX 큐시트를 설계."""
    from google import genai
    from google.genai import types

    prompt = f"""당신은 조회수 수백만 회를 기록하는 한국 유튜브 랭킹 쇼츠('랭킹감자' 스타일) 수석 사운드 디렉터입니다.
첨부된 초안 렌더링 쇼츠 영상을 처음부터 끝까지 직접 시청·청취하고, 시청 지속시간과 몰입도를 극대화하는 정밀 효과음(SFX) 큐시트를 작성하세요.

[영상 기본 사양]
- 전체 길이: {meta['duration']:.2f}초 ({meta['fps']} fps)
- FFmpeg 감지 시각 컷 전환(초): {pre_signals['scene_cuts']}
- FFmpeg 감지 2초 이상 오디오 정적 구간(초): {pre_signals['quiet_windows']}
- FFmpeg 감지 원본 오디오 피크(초): {pre_signals['audio_peaks']}

[타임라인 및 자막/기존 효과음 컨텍스트]
{timeline_text}

[필수 연출 원칙]
1. **초반 3초 후킹 (00:00.00 ~ 00:03.00, `hook_3s`)**: 첫 자막 등장·시선 집중 포인트와 첫 사건 타격 지점에 즉각적인 `pop_ui` / `whoosh` / `impact` 를 배치해 초반 이탈을 막으세요.
2. **화면 전환 및 순위 브릿지 (`cut_transition`)**: 기본 전환음이 비활성화된 경우 컷 경계 직전 `whoosh` + 새 클립 첫 프레임 `impact` 를 배치하세요. (이미 기본 전환음이 활성화된 경우 중복 배치를 피하세요.)
3. **핵심 자막 애니메이션 동기화 (`caption_emphasis`)**:
   - 상황 제시 `drop` 자막: 깔끔한 `pop_ui` (띵, 팝, 찰칵)
   - 당황·의문 `?`, `어..?` 자막: `comedy_cartoon` (띠용, 뾰잉, 삐용)
   - 절정·충격 `[r:...]` `shake`/`zoom` 자막: 묵직한 `impact`, `water_liquid`(물 장면), `vehicle_horn`(도로 장면), 또는 `voice_reaction`(뜨헉, 와우, 환호)
4. **분위기 반전 및 슬로모션 (`mood_twist` / `action_impact`)**: 슬로모션 진입 시 `riser_tension`(심장박동, 라이저), 대반전·실패 순간에 물리 타격음 또는 코믹 사운드를 정확히 사건 프레임에 맞추세요.
5. **무음 공백 방지 (`ambience_fill`)**: 2초 이상 오디오가 비는 구간에는 장면 배경에 맞는 `ambience`(파도, 바람, 실내 소음 등)를 은은하게(`intensity` 0.25~0.4) 깔아주세요.
6. **절제와 리듬**: 효과음이 너무 빽빽하면 산만해집니다. 클립당 핵심 포인트 {cfg.max_cues_per_clip}개 이내로 엄선하고, 최소 {cfg.min_cue_gap_sec}초 이상의 간격을 유지하세요. 타임스탬프는 반드시 0.00초 이상 {meta['duration']:.2f}초 이하로 지정하세요.
"""

    def _make_inline_video_part(vpath: Path):
        # 14MB 초과 초안 영상은 멀티모달 시청용 경량 480p 프록시로 자동 압축해 inline 전송 한도를 준수
        if vpath.stat().st_size <= 14 * 1024 * 1024:
            return types.Part.from_bytes(data=vpath.read_bytes(), mime_type="video/mp4")
        import tempfile as _tf
        with _tf.NamedTemporaryFile(suffix=".mp4", delete=False) as tf:
            proxy_p = Path(tf.name)
        try:
            subprocess.run(
                [
                    "ffmpeg", "-y", "-v", "error", "-i", str(vpath),
                    "-vf", "scale=-2:480,fps=15", "-c:v", "libx264", "-preset", "veryfast", "-crf", "30",
                    "-c:a", "aac", "-b:a", "64k", "-ac", "1", str(proxy_p),
                ],
                check=True,
            )
            return types.Part.from_bytes(data=proxy_p.read_bytes(), mime_type="video/mp4")
        finally:
            proxy_p.unlink(missing_ok=True)

    client, using_vertex = create_genai_client(cfg)
    uploaded_video = None
    try:
        print(f"[SFX Director] 초안 영상 멀티모달 시청 분석 시작 ({cfg.gemini_video_model}, vertex={using_vertex})...")
        for attempt in range(2):
            try:
                if using_vertex:
                    video_input = _make_inline_video_part(video_path)
                else:
                    uploaded_video = client.files.upload(
                        file=str(video_path),
                        config=types.UploadFileConfig(mime_type="video/mp4", display_name=video_path.name),
                    )
                    deadline = time.monotonic() + 240
                    while getattr(uploaded_video.state, "name", str(uploaded_video.state)) == "PROCESSING":
                        if time.monotonic() > deadline:
                            raise TimeoutError(f"Gemini 비디오 처리 대기 시간 초과: {video_path.name}")
                        time.sleep(2.5)
                        uploaded_video = client.files.get(name=uploaded_video.name)
                    state_name = getattr(uploaded_video.state, "name", str(uploaded_video.state))
                    if state_name != "ACTIVE":
                        raise RuntimeError(f"Gemini 비디오 파일 활성화 실패 (state={state_name})")
                    video_input = uploaded_video

                resp = client.models.generate_content(
                    model=cfg.gemini_video_model,
                    contents=[video_input, prompt],
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_schema=GeminiVideoDirectionResponse,
                        temperature=0.2,
                    ),
                )
                cfg.resolved_vertexai = using_vertex
                if resp.parsed is not None and isinstance(resp.parsed, GeminiVideoDirectionResponse):
                    return resp.parsed
                return GeminiVideoDirectionResponse.model_validate_json(resp.text)
            except Exception as exc:
                if (
                    attempt == 0
                    and cfg.gemini_vertexai_mode == "auto"
                    and cfg.resolved_vertexai is None
                    and not using_vertex
                    and is_auth_route_mismatch_error(exc)
                ):
                    try:
                        client.close()
                    except Exception:
                        pass
                    print("  ↻ AQ. 키 엔드포인트 자동 전환 → Vertex AI Express Mode(vertexai=True)로 영상을 시청합니다.")
                    client, using_vertex = create_genai_client(cfg, force_vertexai=True)
                    continue
                raise
    finally:
        if uploaded_video is not None and not using_vertex:
            try:
                client.files.delete(name=uploaded_video.name)
            except Exception:
                pass
        client.close()


def _direct_video_structural_fallback(
    meta: dict[str, Any],
    pre_signals: dict[str, Any],
    items: Optional[list[dict]],
    starts: Optional[list[float]],
    ends: Optional[list[float]],
    cfg_dict: Optional[dict],
) -> GeminiVideoDirectionResponse:
    """GEMINI_API_KEY 미설정 또는 네트워크 제한 환경에서 FFmpeg 장면/피크 분석 + plan.json 구조로 큐시트를 생성."""
    dur = float(meta["duration"])
    cues: list[GeminiCueItemDraft] = []
    cfg_dict = cfg_dict or {}

    if items and starts and ends:
        for i, it in enumerate(items):
            s_t, e_t = starts[i], ends[i]
            rk = int(it.get("rank", len(items) - i))
            # 이미 수동 fx가 충분히 채워진 클립은 부족한 포인트만 보완
            manual_ats = [s_t + float(fx.get("at", 0.0)) for fx in it.get("fx", [])]

            if i == 0 and all(abs(t - 0.15) > 0.3 for t in manual_ats):
                cues.append(
                    GeminiCueItemDraft(
                        timestamp_str=format_timestamp(min(dur, s_t + 0.15)),
                        timestamp_sec=round(min(dur, s_t + 0.15), 2),
                        trigger_type="hook_3s",
                        category="pop_ui",
                        sound_concept_ko="초반 3초 첫 순위 상황 자막 등장 팝 효과음",
                        search_keywords_ko=["팝", "띵", "알림"],
                        search_keywords_en=["pop", "ui ding", "short click"],
                        generation_prompt_en="Clean crisp short UI pop sound effect for video caption",
                        visual_reason_ko="0.15초 첫 클립 후킹 자막 진입 강조",
                        align_to_peak=True,
                        intensity=0.75,
                        max_duration_sec=0.8,
                        rank_context=rk,
                    )
                )

            if it.get("slow"):
                sl_start = round(s_t + float(it["slow"][0]), 2)
                if sl_start <= dur and all(abs(t - sl_start) > 0.35 for t in manual_ats):
                    cues.append(
                        GeminiCueItemDraft(
                            timestamp_str=format_timestamp(sl_start),
                            timestamp_sec=sl_start,
                            trigger_type="mood_twist",
                            category="riser_tension",
                            sound_concept_ko="슬로모션 진입 긴장감 빌드업 심장박동/라이저",
                            search_keywords_ko=["심장박동", "긴장", "라이저"],
                            search_keywords_en=["heartbeat", "tension riser", "suspense"],
                            generation_prompt_en="Dramatic short tension riser and heartbeat buildup for slow motion",
                            visual_reason_ko=f"{rk}위 슬로모션 구간 진입 서스펜스 강화",
                            align_to_peak=False,
                            intensity=0.7,
                            max_duration_sec=2.0,
                            rank_context=rk,
                        )
                    )

            for sub in it.get("subs", []):
                abs_t = round(min(dur, s_t + float(sub.get("at", 0.0))), 2)
                if any(abs(t - abs_t) <= 0.30 for t in manual_ats):
                    continue
                txt = str(sub.get("text", ""))
                anim = str(sub.get("anim", cfg_dict.get("sub_anim", "pop")))
                if anim == "zoom" or "[r:" in txt or "!!" in txt:
                    cues.append(
                        GeminiCueItemDraft(
                            timestamp_str=format_timestamp(abs_t),
                            timestamp_sec=abs_t,
                            trigger_type="action_impact" if abs_t > 3.0 else "hook_3s",
                            category="impact",
                            sound_concept_ko="클라이맥스 펀치라인 자막 타격 임팩트",
                            search_keywords_ko=["두둥탁", "쿵", "퍽", "타격"],
                            search_keywords_en=["punchy impact", "boom hit", "comic thud"],
                            generation_prompt_en="Punchy dramatic impact hit sound for viral short climax",
                            visual_reason_ko=f"{rk}위 클라이맥스 자막('{txt}', {anim}) 타격 동기화",
                            align_to_peak=True,
                            intensity=0.95,
                            max_duration_sec=1.5,
                            rank_context=rk,
                        )
                    )
                elif "?" in txt or "어.." in txt or anim == "shake":
                    cues.append(
                        GeminiCueItemDraft(
                            timestamp_str=format_timestamp(abs_t),
                            timestamp_sec=abs_t,
                            trigger_type="mood_twist",
                            category="comedy_cartoon",
                            sound_concept_ko="당황·의문 반전 포인트 코믹 띠용 효과음",
                            search_keywords_ko=["띠용", "뾰잉", "이상할때"],
                            search_keywords_en=["cartoon boing", "confused sound", "quirky pluck"],
                            generation_prompt_en="Short funny cartoon boing question sound effect",
                            visual_reason_ko=f"{rk}위 반전/당황 자막('{txt}') 강조",
                            align_to_peak=True,
                            intensity=0.8,
                            max_duration_sec=1.2,
                            rank_context=rk,
                        )
                    )
                elif e_t - s_t > 0 and abs_t == round(s_t + float(it["subs"][0].get("at", 0.0)), 2) and i > 0:
                    cues.append(
                        GeminiCueItemDraft(
                            timestamp_str=format_timestamp(abs_t),
                            timestamp_sec=abs_t,
                            trigger_type="caption_emphasis",
                            category="pop_ui",
                            sound_concept_ko="새 순위 첫 상황 자막 등장 팝 사운드",
                            search_keywords_ko=["팝", "띵", "뿅"],
                            search_keywords_en=["soft pop", "bubble pop", "caption ding"],
                            generation_prompt_en="Soft clean bubble pop sound effect for subtitle appearance",
                            visual_reason_ko=f"{rk}위 상황 설명 자막('{txt}') 등장 포인트",
                            align_to_peak=True,
                            intensity=0.6,
                            max_duration_sec=0.7,
                            rank_context=rk,
                        )
                    )
    else:
        # plan.json 없이 순수 MP4만 주어진 경우 FFmpeg 컷 전환 + 오디오 피크 기반 생성
        cues.append(
            GeminiCueItemDraft(
                timestamp_str="00:00.12",
                timestamp_sec=min(dur, 0.12),
                trigger_type="hook_3s",
                category="whoosh",
                sound_concept_ko="초반 0.1초 오프닝 후킹 스우시",
                search_keywords_ko=["휙", "훅", "스우시"],
                search_keywords_en=["fast whoosh", "opener swipe"],
                generation_prompt_en="Fast energetic whoosh sound for short video hook",
                visual_reason_ko="영상 시작 직후 시선 집중 오프닝 훅",
                align_to_peak=True,
                intensity=0.85,
                max_duration_sec=0.9,
            )
        )
        for cut_t in pre_signals["scene_cuts"]:
            if 0.4 <= cut_t <= dur - 0.2:
                cues.append(
                    GeminiCueItemDraft(
                        timestamp_str=format_timestamp(cut_t),
                        timestamp_sec=cut_t,
                        trigger_type="cut_transition",
                        category="whoosh",
                        sound_concept_ko="컷 전환 빠른 스우시 효과음",
                        search_keywords_ko=["휙", "쉭", "전환"],
                        search_keywords_en=["transition whoosh", "quick swoosh"],
                        generation_prompt_en="Crisp fast transition whoosh sound effect",
                        visual_reason_ko=f"{format_timestamp(cut_t)} 프레임 화면 전환 감지",
                        align_to_peak=True,
                        intensity=0.8,
                        max_duration_sec=1.0,
                    )
                )
        for pk_t in pre_signals["audio_peaks"][:4]:
            if 0.3 <= pk_t <= dur - 0.1:
                cues.append(
                    GeminiCueItemDraft(
                        timestamp_str=format_timestamp(pk_t),
                        timestamp_sec=pk_t,
                        trigger_type="action_impact",
                        category="impact",
                        sound_concept_ko="액션 피크 보조 타격음",
                        search_keywords_ko=["쿵", "퍽", "임팩트"],
                        search_keywords_en=["impact hit", "punch thud"],
                        generation_prompt_en="Short punchy impact thud sound effect",
                        visual_reason_ko=f"{format_timestamp(pk_t)} 오디오 에너지 급등 지점 타격 보강",
                        align_to_peak=True,
                        intensity=0.85,
                        max_duration_sec=1.2,
                    )
                )

    for q_start, q_end in pre_signals["quiet_windows"]:
        span = round(min(q_end - q_start, 5.0), 2)
        if span >= 2.0:
            cues.append(
                GeminiCueItemDraft(
                    timestamp_str=format_timestamp(q_start),
                    timestamp_sec=q_start,
                    trigger_type="ambience_fill",
                    category="ambience",
                    sound_concept_ko="2초 이상 오디오 공백 보강용 은은한 공간 앰비언스",
                    search_keywords_ko=["바람", "파도", "환경음", "보글보글"],
                    search_keywords_en=["subtle ambience", "room tone", "wind ambience"],
                    generation_prompt_en="Subtle atmospheric background ambience texture",
                    visual_reason_ko=f"{format_timestamp(q_start)}~{format_timestamp(q_end)} 무음 구간 오디오 공백 해소",
                    align_to_peak=False,
                    intensity=0.35,
                    max_duration_sec=span,
                )
            )

    return GeminiVideoDirectionResponse(
        video_summary_ko="FFmpeg 프레임·오디오 계측 및 자막 타임라인 기반 구조적 효과음 설계",
        hook_analysis_ko="0~3초 첫 클립 진입 자막과 사건 발생 구간에 즉각적 팝/타격 사운드를 배치",
        cues=sorted(cues, key=lambda c: c.timestamp_sec),
    )


def generate_sfx_cue_sheet(
    video_path: Path,
    cfg: Optional[SfxDirectorConfig] = None,
    output_json_path: Optional[Path] = None,
    items: Optional[list[dict]] = None,
    starts: Optional[list[float]] = None,
    ends: Optional[list[float]] = None,
    plan_cfg: Optional[dict] = None,
) -> SfxCueSheet:
    """초안 영상을 분석해 정형화된 SfxCueSheet 객체 생성 및 JSON 저장."""
    cfg = cfg or load_director_config()
    video_path = Path(video_path).resolve()
    if not video_path.is_file():
        raise FileNotFoundError(f"분석할 초안 영상이 없습니다: {video_path}")

    meta = probe_video_meta(video_path)
    pre_signals = detect_scene_cuts_and_silence(video_path, bool(meta["has_audio"]))
    timeline_text, existing_events = build_plan_timeline_context(items, starts, ends, plan_cfg)

    model_label = cfg.gemini_video_model if cfg.gemini_api_key else "structural_dsp_fallback"
    if cfg.gemini_api_key:
        try:
            raw_resp = _direct_video_with_gemini(video_path, meta, pre_signals, timeline_text, cfg)
        except Exception as exc:
            print(f"  ⚠ Gemini 영상 시청 API 호출 실패 → 구조적 큐시트 폴백으로 전환합니다: {exc}")
            model_label = "structural_dsp_fallback"
            raw_resp = _direct_video_structural_fallback(meta, pre_signals, items, starts, ends, plan_cfg)
    else:
        print("[SFX Director] GEMINI_API_KEY 미설정 상태 → FFmpeg 장면·피크 + plan.json 구조 분석 모드로 큐시트를 생성합니다.")
        raw_resp = _direct_video_structural_fallback(meta, pre_signals, items, starts, ends, plan_cfg)

    dur = max(0.05, float(meta["duration"]))
    fps = float(meta["fps"])

    # 타임스탬프 정규화, 수동 효과음 충돌 제거, 최소 간격 필터링
    filtered_cues: list[SfxCueItem] = []
    sorted_drafts = sorted(raw_resp.cues, key=lambda c: c.timestamp_sec)

    for draft in sorted_drafts:
        ts = draft.timestamp_sec
        parsed_from_str = parse_timestamp_str(draft.timestamp_str)
        if (ts < 0 or ts > dur) and parsed_from_str is not None:
            ts = parsed_from_str
        ts = round(min(max(float(ts), 0.0), max(0.0, dur - 0.02)), 3)

        # 기존 수동 fx / 기본 전환음과 동일 시점(±0.18초) 충돌 방지
        if cfg.preserve_manual_fx:
            collision = False
            for ex_t, ex_kind in existing_events:
                if abs(ts - ex_t) <= 0.18 and (
                    ex_kind == draft.category
                    or (ex_kind == "whoosh" and draft.category == "whoosh")
                    or (ex_kind == "impact" and draft.category == "impact")
                ):
                    collision = True
                    break
            if collision:
                continue

        # 직전 채택된 큐와 너무 가까운 동일 계열 중복 제거 (ambience 제외)
        if draft.category != "ambience" and filtered_cues:
            prev = filtered_cues[-1]
            if prev.category != "ambience" and (ts - prev.timestamp_sec) < cfg.min_cue_gap_sec:
                # 더 강도가 높은 큐를 유지
                if draft.intensity > prev.intensity:
                    filtered_cues.pop()
                else:
                    continue

        cue_idx = len(filtered_cues) + 1
        filtered_cues.append(
            SfxCueItem(
                cue_id=f"cue_{cue_idx:03d}",
                timestamp_str=format_timestamp(ts),
                timestamp_sec=ts,
                frame_index=int(round(ts * fps)),
                trigger_type=draft.trigger_type,
                category=draft.category,
                sound_concept_ko=draft.sound_concept_ko,
                search_keywords_ko=draft.search_keywords_ko,
                search_keywords_en=draft.search_keywords_en,
                generation_prompt_en=draft.generation_prompt_en,
                visual_reason_ko=draft.visual_reason_ko,
                align_to_peak=draft.align_to_peak,
                intensity=round(float(draft.intensity), 2),
                max_duration_sec=round(float(draft.max_duration_sec), 2),
                rank_context=draft.rank_context,
            )
        )

    # cue_id 번호 재정렬
    for i, c in enumerate(filtered_cues, start=1):
        c.cue_id = f"cue_{i:03d}"

    sheet = SfxCueSheet(
        video_path=str(video_path),
        video_duration_sec=round(dur, 3),
        fps=fps,
        created_at=dt.datetime.now(dt.timezone.utc).isoformat(),
        director_model=model_label,
        video_summary_ko=raw_resp.video_summary_ko,
        hook_analysis_ko=raw_resp.hook_analysis_ko,
        cues=filtered_cues,
    )

    if output_json_path:
        output_json_path = Path(output_json_path)
        output_json_path.parent.mkdir(parents=True, exist_ok=True)
        output_json_path.write_text(
            json.dumps(sheet.model_dump(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"[SFX Director] 큐시트 저장 완료 ({len(sheet.cues)}개 포인트) → {output_json_path}")

    return sheet
