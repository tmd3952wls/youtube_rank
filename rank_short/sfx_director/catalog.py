"""[Step 2] 로컬 효과음 폴더 자동 청취 및 증분 카탈로그화 모듈 (sfx_catalog.json).

주요 특징:
- `LOCAL_SFX_DIR` 내 모든 오디오 파일(.mp3, .wav, .ogg, .m4a, .flac, .aac) 재귀 탐색
- (상대경로, 파일크기, mtime_ns) + SHA1 해시 기반 초고속 증분 감지 (2,869개 파일에서도 신규/변경분만 처리)
- FFmpeg + NumPy DSP 파형 정밀 측정: 길이(duration), 타격 정점 시점(peak_time_sec, 5ms 해상도), 시작점(onset), 정점 dBFS, 평균 RMS dBFS, 어택 타입 분석
- Gemini 멀티모달 오디오 직접 청취(`google-genai` SDK `Part.from_bytes` / `client.files.upload`)로 사운드 종류, 분위기, 추천 사용 상황 JSON 구조화 추출
- 주기적 원자적 체크포인트 저장(`.tmp` 교체)으로 대량 인덱싱 중 중단되어도 즉시 이어하기 가능
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import mimetypes
import re
import subprocess
import time
from pathlib import Path
from typing import Optional

import numpy as np

from .config import SfxDirectorConfig, create_genai_client, is_auth_route_mismatch_error, load_director_config
from .schemas import GeminiAudioListenResult, SfxCatalog, SfxCatalogItem, SfxCategory

AUDIO_EXTENSIONS = {".mp3", ".wav", ".ogg", ".m4a", ".flac", ".aac", ".opus"}

MIME_BY_EXT = {
    ".mp3": "audio/mp3",
    ".wav": "audio/wav",
    ".ogg": "audio/ogg",
    ".m4a": "audio/mp4",
    ".flac": "audio/flac",
    ".aac": "audio/aac",
    ".opus": "audio/ogg",
}

# 파일명 기반 폴백/보조 분류 사전 (한·영 효과음 명명 패턴 반영)
FILENAME_CATEGORY_RULES: list[tuple[SfxCategory, re.Pattern[str], str]] = [
    ("bgm_sting", re.compile(r"bgm|브금|배경음|vlog|로파이|lofi|테마|합창|피아노|노래|music|카페|여행|행복|슬픔|히어로|긴장감", re.I), "bgm_track"),
    ("whoosh", re.compile(r"whoosh|swoosh|swipe|휙|쉭|훅|스우시|바람\s*가르는|전환|지나가는\s*휙|로켓\s*쉭", re.I), "whoosh_transition"),
    ("water_liquid", re.compile(r"water|splash|bubble|풍덩|첨벙|물소리|파도|보글보글|꼬로록|다이빙|수영", re.I), "water_splash"),
    ("vehicle_horn", re.compile(r"brake|horn|siren|car|engine|급브레이크|경적|빵빵|자동차|오토바이|비행기|헬기|사이렌", re.I), "vehicle_sfx"),
    ("voice_reaction", re.compile(r"wow|scream|cheer|laugh|gasp|뜨헉|헉|와우|환호|군중|웃음|비명|박수|예에|오마이갓", re.I), "voice_reaction"),
    ("comedy_cartoon", re.compile(r"boing|cartoon|slip|scratch|띠용|띠동|뾰잉|삐용|카툰|만화|미끄러|쫄래쫄래|뽀욕|띠유", re.I), "cartoon_comedy"),
    ("riser_tension", re.compile(r"riser|heartbeat|tick|suspense|horror|심장|째깍|긴장|공포|쿠우웅|쿠-우|빌드업|드롭전", re.I), "tension_riser"),
    ("animal_nature", re.compile(r"cat|dog|bird|frog|thunder|wind|고양이|강아지|개구리|새소리|천둥|번개|바람", re.I), "animal_nature"),
    ("pop_ui", re.compile(r"pop|click|ding|bell|chime|팝|띵|클릭|알림|체크|버튼|뿅|찰칵", re.I), "ui_pop"),
    ("impact", re.compile(r"boom|hit|punch|crash|thud|explosion|쿵|퍽|탁|두둥|폭발|충돌|타격|펀치|텅|둥|쾅", re.I), "impact_hit"),
]


def compute_sha1(path: Path) -> str:
    h = hashlib.sha1()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def analyze_waveform_dsp(path: Path, sample_rate: int = 24000) -> dict[str, float | str]:
    """FFmpeg + NumPy로 오디오의 물리적 특성(길이, 5ms 단위 정점 시각, 시작점, 피크/RMS dBFS, 어택 형태)을 측정."""
    cmd = [
        "ffmpeg", "-v", "error", "-i", str(path),
        "-ac", "1", "-ar", str(sample_rate), "-f", "f32le", "-",
    ]
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode != 0 or not r.stdout:
        raise RuntimeError(f"FFmpeg 오디오 디코딩 실패: {path} ({r.stderr.decode('utf-8', 'replace')[-300:]})")

    samples = np.frombuffer(r.stdout, dtype=np.float32)
    if samples.size == 0:
        raise RuntimeError(f"오디오 샘플이 비어 있습니다: {path}")

    duration_sec = float(samples.size) / sample_rate
    # 5ms (24kHz 기준 120 샘플) 단위 엔벨로프로 타격 정점 시점 정밀 탐색
    hop = max(1, int(sample_rate * 0.005))
    n_frames = max(1, samples.size // hop)
    trimmed = samples[: n_frames * hop].reshape(n_frames, hop)
    frame_peaks = np.abs(trimmed).max(axis=1)

    peak_idx = int(frame_peaks.argmax())
    peak_amp = max(float(frame_peaks[peak_idx]), 1e-6)
    peak_dbfs = 20.0 * math.log10(peak_amp)
    peak_time_sec = round(peak_idx * 0.005, 4)

    # 소리 시작 시점(onset): 정점 진폭의 10%(-20dB) 이상 처음 도달하는 프레임
    onset_indices = np.where(frame_peaks >= peak_amp * 0.10)[0]
    onset_time_sec = round(int(onset_indices[0]) * 0.005, 4) if onset_indices.size > 0 else 0.0

    # 유성 구간 평균 RMS dBFS (무음 꼬리 제외)
    blk = max(1, int(sample_rate * 0.05))
    n_blk = max(1, samples.size // blk)
    blk_rms = np.sqrt(np.mean(samples[: n_blk * blk].reshape(n_blk, blk) ** 2, axis=1) + 1e-12)
    active = blk_rms[blk_rms > blk_rms.max() * 0.05]
    rms_val = float(np.sqrt(np.mean(active ** 2))) if active.size > 0 else float(np.sqrt(np.mean(blk_rms ** 2)))
    rms_dbfs = 20.0 * math.log10(max(rms_val, 1e-6))

    attack_delta = max(0.0, peak_time_sec - onset_time_sec)
    if duration_sec >= 8.0 and (peak_dbfs - rms_dbfs) < 12.0:
        attack_type = "sustained"
    elif attack_delta <= 0.12:
        attack_type = "impulsive"
    elif attack_delta >= 0.25:
        attack_type = "swell"
    else:
        attack_type = "sustained" if duration_sec > 3.5 else "impulsive"

    return {
        "duration_sec": round(duration_sec, 3),
        "peak_time_sec": peak_time_sec,
        "onset_time_sec": onset_time_sec,
        "peak_dbfs": round(peak_dbfs, 2),
        "rms_dbfs": round(rms_dbfs, 2),
        "attack_type": attack_type,
    }


def infer_metadata_heuristic(path: Path, dsp: dict) -> GeminiAudioListenResult:
    """GEMINI_API_KEY 미설정 또는 오프라인 테스트 모드 시 파일명 + DSP 파형 특성으로 기본 메타데이터를 추론."""
    stem = path.stem
    clean = re.sub(r"^\d+[_\-\s]*", "", stem).strip() or stem
    dur = float(dsp["duration_sec"])
    attack = str(dsp["attack_type"])

    matched_cat: SfxCategory = "other"
    subcat = "general_sfx"
    for cat, pat, sub in FILENAME_CATEGORY_RULES:
        if pat.search(clean) or pat.search(stem):
            matched_cat = cat
            subcat = sub
            break

    if matched_cat == "other":
        if dur >= 15.0:
            matched_cat, subcat = "bgm_sting", "bgm_loop"
        elif dur >= 5.0 and attack == "sustained":
            matched_cat, subcat = "ambience", "ambient_texture"
        elif attack == "swell":
            matched_cat, subcat = "whoosh", "swell_transition"
        elif dur <= 0.45:
            matched_cat, subcat = "pop_ui", "short_accent"
        else:
            matched_cat, subcat = "impact", "percussive_hit"

    is_bgm = matched_cat == "bgm_sting" or dur >= 18.0
    align_peak = (not is_bgm) and matched_cat in ("whoosh", "impact", "pop_ui", "comedy_cartoon", "water_liquid")

    tokens_ko = [t for t in re.split(r"[\s_\-()\[\],.]+", clean) if t]
    cat_en_map = {
        "whoosh": ["whoosh", "transition", "swipe"],
        "impact": ["impact", "hit", "boom"],
        "pop_ui": ["pop", "ding", "ui", "caption"],
        "comedy_cartoon": ["cartoon", "boing", "funny"],
        "voice_reaction": ["reaction", "voice", "crowd"],
        "riser_tension": ["riser", "tension", "suspense"],
        "water_liquid": ["water", "splash", "liquid"],
        "vehicle_horn": ["horn", "brake", "vehicle"],
        "animal_nature": ["animal", "nature"],
        "ambience": ["ambience", "background", "roomtone"],
        "bgm_sting": ["bgm", "music", "sting"],
        "other": ["sfx", "sound effect"],
    }
    mood_map = {
        "whoosh": ["속도감", "경쾌한", "전환"],
        "impact": ["타격감", "강렬한", "임팩트"],
        "pop_ui": ["깔끔한", "집중", "강조"],
        "comedy_cartoon": ["코믹한", "유쾌한", "반전"],
        "voice_reaction": ["생동감", "리액션", "과장된"],
        "riser_tension": ["긴장감", "서스펜스", "고조"],
        "water_liquid": ["시원한", "현장감", "액션"],
        "vehicle_horn": ["긴박한", "현실감", "경고"],
        "animal_nature": ["자연스러운", "현장감"],
        "ambience": ["공간감", "잔잔한", "배경"],
        "bgm_sting": ["배경음악", "무드"],
        "other": ["포인트", "효과음"],
    }
    moment_map = {
        "whoosh": ["순위 간 검은 화면 컷 전환", "빠른 동작·카메라 패닝 순간"],
        "impact": ["다음 순위 클립 첫 프레임 등장", "펀치라인 자막(zoom/shake) 타격 시점"],
        "pop_ui": ["상황 설명·포인트 자막(pop/drop) 등장", "초반 3초 후킹 텍스트 강조"],
        "comedy_cartoon": ["예상 밖 실수·당황스러운 반전 순간", "'?' 또는 '어..?' 자막 등장"],
        "voice_reaction": ["클라이맥스 대성공·대참사 리액션", "관객 환호·놀람 포인트"],
        "riser_tension": ["사건 직전 슬로모션 빌드업 구간", "1위 공개 직전 긴장감 조성"],
        "water_liquid": ["물에 빠지는 순간(풍덩)", "수중·바다·수영장 클립 배경 및 액션"],
        "vehicle_horn": ["도로·블랙박스 아찔한 회피 순간", "급정거·돌발 등장"],
        "animal_nature": ["반려동물·시골·야외 클립 현장감 강조"],
        "ambience": ["원본 오디오가 없는 클립의 2초 이상 무음 구간 보강"],
        "bgm_sting": ["클립별 분위기 배경음 또는 하이라이트 스팅"],
        "other": ["장면 내 동작 및 자막 보조 강조"],
    }
    return GeminiAudioListenResult(
        category=matched_cat,
        subcategory=subcat,
        moods=mood_map[matched_cat],
        tags_ko=list(dict.fromkeys([clean, matched_cat, *tokens_ko]))[:7],
        tags_en=cat_en_map[matched_cat],
        description_ko=f"'{clean}' 사운드 ({dur:.2f}초, 정점 {dsp['peak_time_sec']:.2f}초, {attack} 어택)",
        recommended_moments=moment_map[matched_cat],
        is_bgm_or_long_music=is_bgm,
        align_peak_recommended=align_peak,
    )


def listen_with_gemini(
    path: Path,
    dsp: dict,
    cfg: SfxDirectorConfig,
    max_retries: int = 3,
) -> GeminiAudioListenResult:
    """Gemini 멀티모달 API로 실제 오디오 파형을 청취하여 카테고리·분위기·추천 연출 상황을 분석."""
    from google import genai
    from google.genai import types

    mime_type = MIME_BY_EXT.get(path.suffix.lower()) or mimetypes.guess_type(str(path))[0] or "audio/mpeg"
    size_mb = path.stat().st_size / (1024 * 1024)

    prompt = (
        "당신은 유튜브 랭킹 쇼츠 전문 사운드 디렉터입니다. 첨부된 오디오 파일을 직접 끝까지 듣고 "
        "쇼츠 자동 편집 파이프라인에서 검색·매칭할 수 있도록 정확히 분석하세요.\n"
        f"- 참고 파일명: {path.name}\n"
        f"- DSP 실측 길이: {dsp['duration_sec']}초 / 정점(타격) 시각: {dsp['peak_time_sec']}초 / "
        f"어택 특성: {dsp['attack_type']}\n"
        "주의사항:\n"
        "1. 파일명과 실제 소리가 다르면 반드시 실제 들리는 소리를 기준으로 분류하세요.\n"
        "2. 10초 이상의 음악/루프 배경음이면 category='bgm_sting' 및 is_bgm_or_long_music=true 로 표시하세요.\n"
        "3. 스우시(whoosh)나 타격음처럼 정점을 컷/자막 순간에 맞춰야 하는 소리는 align_peak_recommended=true 로 설정하세요."
    )

    client, using_vertex = create_genai_client(cfg)
    uploaded_file = None
    try:
        for attempt in range(max_retries):
            try:
                if using_vertex or size_mb <= cfg.catalog_max_inline_mb:
                    # Vertex AI Express Mode 는 Files API 대신 inline bytes 를 사용 (대용량 BGM은 앞 25초 64kbps 샘플링)
                    if size_mb > cfg.catalog_max_inline_mb:
                        r_slice = subprocess.run(
                            ["ffmpeg", "-v", "error", "-t", "25", "-i", str(path), "-ac", "1", "-ar", "16000", "-b:a", "64k", "-f", "mp3", "-"],
                            capture_output=True,
                        )
                        audio_part = types.Part.from_bytes(data=r_slice.stdout or path.read_bytes()[:5_000_000], mime_type="audio/mp3")
                    else:
                        audio_part = types.Part.from_bytes(data=path.read_bytes(), mime_type=mime_type)
                    contents = [audio_part, prompt]
                else:
                    if uploaded_file is None:
                        uploaded_file = client.files.upload(
                            file=str(path),
                            config=types.UploadFileConfig(mime_type=mime_type, display_name=path.name),
                        )
                        deadline = time.monotonic() + 120
                        while getattr(uploaded_file.state, "name", str(uploaded_file.state)) == "PROCESSING":
                            if time.monotonic() > deadline:
                                raise TimeoutError(f"Gemini File API 오디오 처리 시간 초과: {path.name}")
                            time.sleep(2.0)
                            uploaded_file = client.files.get(name=uploaded_file.name)
                    contents = [uploaded_file, prompt]

                resp = client.models.generate_content(
                    model=cfg.gemini_audio_model,
                    contents=contents,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_schema=GeminiAudioListenResult,
                        temperature=0.1,
                    ),
                )
                cfg.resolved_vertexai = using_vertex
                if resp.parsed is not None and isinstance(resp.parsed, GeminiAudioListenResult):
                    return resp.parsed
                return GeminiAudioListenResult.model_validate_json(resp.text)
            except Exception as exc:
                # AQ. 접두사 Vertex AI Express 키가 일반 AI Studio 엔드포인트에서 거부된 경우 즉시 Vertex 모드로 전환 재시도
                if (
                    cfg.gemini_vertexai_mode == "auto"
                    and cfg.resolved_vertexai is None
                    and not using_vertex
                    and is_auth_route_mismatch_error(exc)
                ):
                    try:
                        client.close()
                    except Exception:
                        pass
                    print("  ↻ AQ. 키 엔드포인트 자동 감지 → Vertex AI Express Mode(vertexai=True)로 전환하여 재시도합니다.")
                    client, using_vertex = create_genai_client(cfg, force_vertexai=True)
                    continue
                if is_auth_route_mismatch_error(exc):
                    # 인증/키 오류는 불필요한 지수 백오프 대기 없이 즉시 반환
                    raise RuntimeError(f"Gemini 인증 오류 ({path.name}): {exc}") from exc
                if attempt + 1 >= max_retries:
                    raise RuntimeError(f"Gemini 오디오 청취 실패 ({path.name}): {exc}") from exc
                time.sleep(2.0 * (2 ** attempt))
    finally:
        if uploaded_file is not None and not using_vertex:
            try:
                client.files.delete(name=uploaded_file.name)
            except Exception:
                pass
        client.close()

    raise RuntimeError(f"Gemini 오디오 청취 응답 없음: {path.name}")


def analyze_single_audio_file(
    path: Path,
    sfx_root: Path,
    cfg: SfxDirectorConfig,
    source: str = "local",
    external_url: Optional[str] = None,
    license_info: Optional[str] = None,
    allow_heuristic_fallback: bool = True,
) -> SfxCatalogItem:
    """단일 오디오 파일을 DSP + Gemini 멀티모달(또는 휴리스틱 폴백)로 분석하여 SfxCatalogItem 생성."""
    st = path.stat()
    sha1 = compute_sha1(path)
    dsp = analyze_waveform_dsp(path)

    used_source = source
    if cfg.gemini_api_key:
        try:
            semantic = listen_with_gemini(path, dsp, cfg)
        except Exception as exc:
            if not allow_heuristic_fallback:
                raise
            print(f"  ⚠ Gemini 청취 실패 → DSP+파일명 휴리스틱 폴백 적용 ({path.name}): {exc}")
            semantic = infer_metadata_heuristic(path, dsp)
            if source == "local":
                used_source = "dsp_heuristic"
    else:
        semantic = infer_metadata_heuristic(path, dsp)
        if source == "local":
            used_source = "dsp_heuristic"

    try:
        rel_path = path.resolve().relative_to(sfx_root.resolve()).as_posix()
    except ValueError:
        rel_path = path.name

    return SfxCatalogItem(
        id=f"sfx_{sha1[:12]}",
        filename=path.name,
        rel_path=rel_path,
        sha1=sha1,
        size_bytes=st.st_size,
        mtime_ns=st.st_mtime_ns,
        duration_sec=float(dsp["duration_sec"]),
        peak_time_sec=float(dsp["peak_time_sec"]),
        onset_time_sec=float(dsp["onset_time_sec"]),
        peak_dbfs=float(dsp["peak_dbfs"]),
        rms_dbfs=float(dsp["rms_dbfs"]),
        attack_type=dsp["attack_type"],  # type: ignore[arg-type]
        category=semantic.category,
        subcategory=semantic.subcategory,
        moods=semantic.moods,
        tags_ko=semantic.tags_ko,
        tags_en=semantic.tags_en,
        description_ko=semantic.description_ko,
        recommended_moments=semantic.recommended_moments,
        is_bgm_or_long_music=semantic.is_bgm_or_long_music,
        align_peak_recommended=semantic.align_peak_recommended,
        source=used_source,  # type: ignore[arg-type]
        external_url=external_url,
        license_info=license_info,
        indexed_at=dt.datetime.now(dt.timezone.utc).isoformat(),
    )


def load_catalog(catalog_path: Path, default_sfx_dir: Optional[Path] = None) -> SfxCatalog:
    """기존 sfx_catalog.json 로드 (없으면 빈 카탈로그 반환)."""
    try:
        exists = catalog_path.is_file()
    except OSError:
        exists = False
    if exists:
        data = json.loads(catalog_path.read_text(encoding="utf-8"))
        return SfxCatalog.model_validate(data)
    return SfxCatalog(
        sfx_dir=str(default_sfx_dir or catalog_path.parent),
        updated_at=dt.datetime.now(dt.timezone.utc).isoformat(),
        total_count=0,
        items=[],
    )


def save_catalog_atomic(catalog: SfxCatalog, catalog_path: Path) -> None:
    """중단 시 파일 손상을 막기 위한 임시 파일 후 원자적 교체 저장."""
    catalog.total_count = len(catalog.items)
    catalog.updated_at = dt.datetime.now(dt.timezone.utc).isoformat()
    catalog_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = catalog_path.with_suffix(".json.tmp")
    tmp_path.write_text(
        json.dumps(catalog.model_dump(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp_path.replace(catalog_path)


def build_or_update_catalog(
    cfg: Optional[SfxDirectorConfig] = None,
    sfx_dir: Optional[Path] = None,
    catalog_path: Optional[Path] = None,
    limit: Optional[int] = None,
    force_rebuild: bool = False,
    prune_deleted: bool = True,
    upgrade_heuristics: bool = False,
) -> tuple[SfxCatalog, dict[str, int]]:
    """로컬 효과음 폴더를 스캔하여 신규/변경된 오디오 파일만 증분 분석 후 sfx_catalog.json 갱신."""
    cfg = cfg or load_director_config()
    target_dir = Path(sfx_dir) if sfx_dir else cfg.local_sfx_dir
    out_path = Path(catalog_path) if catalog_path else cfg.catalog_path

    if not target_dir.is_dir():
        raise FileNotFoundError(
            f"로컬 효과음 폴더를 찾을 수 없습니다: {target_dir} (.env 의 LOCAL_SFX_DIR 경로를 확인하세요)"
        )

    existing = (
        SfxCatalog(sfx_dir=str(target_dir), updated_at="", total_count=0, items=[])
        if force_rebuild
        else load_catalog(out_path, target_dir)
    )

    by_rel: dict[str, SfxCatalogItem] = {it.rel_path: it for it in existing.items}
    by_sha1: dict[str, SfxCatalogItem] = {it.sha1: it for it in existing.items}

    audio_files = sorted(
        p for p in target_dir.rglob("*")
        if p.is_file() and p.suffix.lower() in AUDIO_EXTENSIONS
    )

    kept_items: dict[str, SfxCatalogItem] = {}
    to_analyze: list[Path] = []
    reused_count = 0
    relocated_count = 0

    for fpath in audio_files:
        rel = fpath.resolve().relative_to(target_dir.resolve()).as_posix()
        st = fpath.stat()
        prev = by_rel.get(rel)
        if prev and prev.size_bytes == st.st_size and prev.mtime_ns == st.st_mtime_ns:
            if upgrade_heuristics and cfg.gemini_api_key and prev.source == "dsp_heuristic":
                to_analyze.append(fpath)
            else:
                kept_items[rel] = prev
                reused_count += 1
            continue

        # 파일명/위치가 바뀌었거나 mtime 만 바뀐 경우 SHA1 해시로 기존 분석 재사용
        sha1 = compute_sha1(fpath)
        same_hash = by_sha1.get(sha1)
        if same_hash and not (upgrade_heuristics and cfg.gemini_api_key and same_hash.source == "dsp_heuristic"):
            updated_item = same_hash.model_copy(
                update={
                    "filename": fpath.name,
                    "rel_path": rel,
                    "size_bytes": st.st_size,
                    "mtime_ns": st.st_mtime_ns,
                }
            )
            kept_items[rel] = updated_item
            relocated_count += 1
            continue

        to_analyze.append(fpath)

    if not prune_deleted:
        for rel, old_item in by_rel.items():
            kept_items.setdefault(rel, old_item)

    total_pending = len(to_analyze)
    if limit is not None and limit > 0:
        to_analyze = to_analyze[:limit]

    analyzed_count = 0
    failed_count = 0
    mode_label = f"Gemini 멀티모달({cfg.gemini_audio_model})" if cfg.gemini_api_key else "DSP+파일명 휴리스틱(GEMINI_API_KEY 미설정)"
    print(
        f"[SFX Catalog] 스캔 완료: 총 {len(audio_files)}개 오디오 | "
        f"기존 캐시 유지 {reused_count + relocated_count}개 | 신규 분석 대상 {len(to_analyze)}개"
        + (f" (전체 미분석 {total_pending}개 중 --limit {limit} 적용)" if limit and total_pending > len(to_analyze) else "")
        + f" | 분석 모드: {mode_label}"
    )

    for idx, fpath in enumerate(to_analyze, start=1):
        rel = fpath.resolve().relative_to(target_dir.resolve()).as_posix()
        try:
            item = analyze_single_audio_file(fpath, target_dir, cfg)
            kept_items[rel] = item
            analyzed_count += 1
            print(
                f"  [{idx}/{len(to_analyze)}] ✓ {rel} → {item.category}/{item.subcategory} "
                f"(dur={item.duration_sec:.2f}s, peak={item.peak_time_sec:.2f}s)"
            )
        except Exception as exc:
            failed_count += 1
            print(f"  [{idx}/{len(to_analyze)}] ✗ 분석 건너뜀 ({rel}): {exc}")

        if idx % cfg.catalog_checkpoint_every == 0:
            checkpoint = SfxCatalog(
                sfx_dir=str(target_dir),
                updated_at=dt.datetime.now(dt.timezone.utc).isoformat(),
                total_count=len(kept_items),
                items=sorted(kept_items.values(), key=lambda x: x.rel_path),
            )
            save_catalog_atomic(checkpoint, out_path)

    final_catalog = SfxCatalog(
        sfx_dir=str(target_dir),
        updated_at=dt.datetime.now(dt.timezone.utc).isoformat(),
        total_count=len(kept_items),
        items=sorted(kept_items.values(), key=lambda x: x.rel_path),
    )
    save_catalog_atomic(final_catalog, out_path)

    stats = {
        "total_scanned": len(audio_files),
        "reused": reused_count,
        "relocated": relocated_count,
        "newly_analyzed": analyzed_count,
        "failed": failed_count,
        "remaining_unanalyzed": max(0, total_pending - len(to_analyze)),
        "catalog_total": final_catalog.total_count,
    }
    return final_catalog, stats
