"""[Step 4] 하이브리드 효과음 자동 매칭 및 부족분 자동 수급(Fallback) 모듈.

수급 우선순위:
1순위: 로컬 카탈로그(`sfx_catalog.json`) 다중 가중치 매칭 (카테고리 일치 + 한/영 태그·파일명 키워드 + 길이·어택 적합성 + 반복 사용 패널티)
2순위: Freesound APIv2 (`GET https://freesound.org/apiv2/search/`) 검색 및 고음질 프리뷰(`preview-hq-mp3`) 또는 원본 자동 다운로드
3순위: ElevenLabs Sound Effects API (`POST https://api.elevenlabs.io/v1/sound-generation`) 텍스트-투-사운드 생성
- 외부에서 수급한 효과음은 즉시 `<LOCAL_SFX_DIR>/_auto_acquired/` 폴더에 저장하고 DSP 파형 분석 후 `sfx_catalog.json`에 영구 등록하여 다음 실행부터 로컬 1순위로 재사용
- API 키 유무 및 `SFX_FALLBACK_MODE`(`auto` / `local_only` / `freesound` / `elevenlabs`)에 따라 무중단 전환
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Optional

import requests

from .catalog import analyze_single_audio_file, load_catalog, save_catalog_atomic
from .config import SfxDirectorConfig, load_director_config
from .schemas import SfxCatalog, SfxCatalogItem, SfxCueItem, SfxCueSheet

RELATED_CATEGORIES: dict[str, set[str]] = {
    "whoosh": {"riser_tension"},
    "impact": {"pop_ui", "comedy_cartoon"},
    "pop_ui": {"comedy_cartoon", "impact"},
    "comedy_cartoon": {"pop_ui", "voice_reaction"},
    "voice_reaction": {"comedy_cartoon"},
    "riser_tension": {"ambience", "whoosh"},
    "water_liquid": {"ambience", "impact"},
    "vehicle_horn": {"impact", "whoosh"},
    "animal_nature": {"ambience", "voice_reaction"},
    "ambience": {"animal_nature", "water_liquid", "riser_tension"},
}


def _sanitize_slug(text: str, max_len: int = 36) -> str:
    s = re.sub(r"[^0-9A-Za-z가-힣_\-]+", "_", text.strip())
    s = re.sub(r"_+", "_", s).strip("_")
    return (s[:max_len] or "sfx").lower()


def _tokenize(texts: list[str]) -> set[str]:
    out: set[str] = set()
    for t in texts:
        for tok in re.split(r"[\s_\-()\[\],./!?]+", t.lower()):
            if len(tok) >= 1:
                out.add(tok)
    return out


def score_catalog_candidate(
    cue: SfxCueItem,
    item: SfxCatalogItem,
    usage_counts: dict[str, int],
) -> float:
    """단일 큐 항목 대비 로컬 카탈로그 아이템의 적합도 점수(0.0 ~ 1.0) 산출."""
    # 단발성 효과음 큐에 장시간 BGM 트랙이 매칭되는 현상 차단
    if item.is_bgm_or_long_music and cue.category != "bgm_sting":
        return 0.0

    score = 0.0

    # 1. 카테고리 일치도 (최대 0.42점)
    if item.category == cue.category:
        score += 0.42
    elif item.category in RELATED_CATEGORIES.get(cue.category, set()):
        score += 0.18

    # 2. 한국어/영어 키워드 및 파일명 부분 일치도 (최대 0.38점)
    cue_ko = [k.lower() for k in cue.search_keywords_ko if k.strip()]
    cue_en = [k.lower() for k in cue.search_keywords_en if k.strip()]
    haystack_str = " ".join(
        [
            item.filename.lower(),
            item.subcategory.lower(),
            item.description_ko.lower(),
            *(t.lower() for t in item.tags_ko),
            *(t.lower() for t in item.tags_en),
            *(m.lower() for m in item.moods),
            *(rm.lower() for rm in item.recommended_moments),
        ]
    )
    item_tokens = _tokenize([item.filename, item.subcategory, *item.tags_ko, *item.tags_en])

    kw_hits = 0.0
    total_kw = max(1, len(cue_ko) + len(cue_en))
    for kw in cue_ko + cue_en:
        if kw in item_tokens or re.sub(r"^\d+_", "", item.filename.lower()).startswith(kw):
            kw_hits += 1.0
        elif kw in haystack_str:
            kw_hits += 0.65
    score += min(0.38, 0.38 * (kw_hits / min(total_kw, 3)))

    # 3. 재생 길이 및 어택 적합성 (최대 0.20점)
    if cue.category == "ambience":
        if item.duration_sec >= 1.8:
            score += 0.20
        else:
            score += 0.05
    else:
        if item.duration_sec <= cue.max_duration_sec * 1.4:
            score += 0.14
        elif item.duration_sec <= cue.max_duration_sec * 2.5:
            score += 0.07
        if cue.align_to_peak and item.attack_type in ("impulsive", "swell"):
            score += 0.06

    # 4. 다양성 보정 (한 영상에서 같은 효과음만 반복되는 현상 완화)
    used = usage_counts.get(item.id, 0)
    if used > 0:
        score -= min(0.18, 0.06 * used)

    return round(max(0.0, min(1.0, score)), 4)


def resolve_catalog_item_path(item: SfxCatalogItem, sfx_dir: Path) -> Optional[Path]:
    cand = sfx_dir / item.rel_path
    if cand.is_file():
        return cand
    cand2 = sfx_dir / item.filename
    if cand2.is_file():
        return cand2
    return None


def fetch_from_freesound(
    cue: SfxCueItem,
    cfg: SfxDirectorConfig,
    timeout: int = 25,
) -> Optional[tuple[Path, str, str]]:
    """Freesound APIv2 로 적합한 효과음을 검색하여 로컬 `_auto_acquired` 폴더에 자동 다운로드."""
    if not cfg.freesound_api_key:
        return None

    headers = {"Authorization": f"Token {cfg.freesound_api_key}"}
    queries = []
    if cue.search_keywords_en:
        queries.append(" ".join(cue.search_keywords_en[:2]))
        if len(cue.search_keywords_en) > 1:
            queries.append(cue.search_keywords_en[0])
    queries.append(cue.category.replace("_", " "))

    min_dur = 1.5 if cue.category == "ambience" else 0.08
    max_dur = max(2.5, min(15.0, cue.max_duration_sec * 2.2))
    fields = "id,name,tags,previews,download,duration,license,username,avg_rating,num_downloads,url"

    for q in dict.fromkeys(queries):
        params = {
            "query": q,
            "filter": f"duration:[{min_dur:.2f} TO {max_dur:.2f}]",
            "sort": "rating_desc",
            "page_size": "10",
            "fields": fields,
        }
        try:
            resp = requests.get(
                "https://freesound.org/apiv2/search/",
                headers=headers,
                params=params,
                timeout=timeout,
            )
            if resp.status_code != 200:
                continue
            results = resp.json().get("results") or []
            if not results:
                continue

            # 다운로드 수와 평점이 고른 상위 사운드 선택
            best = max(
                results,
                key=lambda r: float(r.get("avg_rating") or 0.0) + min(2.0, (r.get("num_downloads") or 0) / 500.0),
            )
            sound_id = best["id"]
            slug = _sanitize_slug(best.get("name") or q)
            cfg.acquired_dir.mkdir(parents=True, exist_ok=True)

            # OAuth2 토큰이 있으면 원본 다운로드 시도, 없으면 API Key 만으로 즉시 동작하는 고음질 MP3/OGG 프리뷰 사용
            dl_bytes: Optional[bytes] = None
            ext = ".mp3"
            if cfg.freesound_oauth_token and best.get("download"):
                r_orig = requests.get(
                    best["download"],
                    headers={"Authorization": f"Bearer {cfg.freesound_oauth_token}"},
                    timeout=timeout,
                )
                if r_orig.status_code == 200 and r_orig.content:
                    dl_bytes = r_orig.content
                    orig_ext = Path(str(best.get("name", ""))).suffix.lower()
                    ext = orig_ext if orig_ext in (".wav", ".mp3", ".ogg", ".flac", ".m4a") else ".wav"

            if dl_bytes is None:
                previews = best.get("previews") or {}
                preview_url = (
                    previews.get("preview-hq-mp3")
                    or previews.get("preview-hq-ogg")
                    or previews.get("preview-lq-mp3")
                )
                if not preview_url:
                    continue
                ext = ".ogg" if preview_url.endswith(".ogg") else ".mp3"
                r_prev = requests.get(preview_url, timeout=timeout)
                if r_prev.status_code == 200 and r_prev.content:
                    dl_bytes = r_prev.content

            if not dl_bytes:
                continue

            out_file = cfg.acquired_dir / f"fs_{sound_id}_{slug}{ext}"
            out_file.write_bytes(dl_bytes)
            ext_url = best.get("url") or f"https://freesound.org/s/{sound_id}/"
            lic = f"Freesound ({best.get('license', 'CC')}) by {best.get('username', 'unknown')}"
            print(f"  [Fallback:Freesound] ✓ 다운로드 완료: {out_file.name} ({lic})")
            return out_file, ext_url, lic
        except Exception as exc:
            print(f"  ⚠ Freesound 검색/다운로드 오류 (query='{q}'): {exc}")
            continue

    return None


def generate_with_elevenlabs(
    cue: SfxCueItem,
    cfg: SfxDirectorConfig,
    timeout: int = 45,
) -> Optional[tuple[Path, str, str]]:
    """ElevenLabs Sound Effects API (`POST /v1/sound-generation`)를 호출해 부족한 효과음을 생성 후 저장."""
    if not cfg.elevenlabs_api_key:
        return None

    prompt_en = (cue.generation_prompt_en or "").strip()
    if not prompt_en:
        kws = ", ".join(cue.search_keywords_en or [cue.category])
        prompt_en = f"High quality crisp {kws} sound effect for short-form video editing"

    # ElevenLabs API 제약: duration_seconds 는 0.5 ~ 30.0 사이 실수
    dur_sec = round(min(30.0, max(0.5, float(cue.max_duration_sec))), 2)
    url = "https://api.elevenlabs.io/v1/sound-generation"
    headers = {
        "xi-api-key": cfg.elevenlabs_api_key,
        "Content-Type": "application/json",
    }
    params = {"output_format": cfg.elevenlabs_output_format}
    payload = {
        "text": prompt_en,
        "duration_seconds": dur_sec,
        "prompt_influence": cfg.elevenlabs_prompt_influence,
        "loop": cue.category == "ambience",
        "model_id": cfg.elevenlabs_sfx_model,
    }

    try:
        resp = requests.post(url, headers=headers, params=params, json=payload, timeout=timeout)
        if resp.status_code != 200 or not resp.content:
            print(f"  ⚠ ElevenLabs 효과음 생성 실패 (HTTP {resp.status_code}): {resp.text[:200]}")
            return None

        cfg.acquired_dir.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha1(f"{prompt_en}:{dur_sec}".encode("utf-8")).hexdigest()[:8]
        slug = _sanitize_slug("_".join(cue.search_keywords_en[:2] or [cue.category]))
        ext = ".wav" if cfg.elevenlabs_output_format.startswith(("pcm_", "wav_")) else ".mp3"
        out_file = cfg.acquired_dir / f"el_{digest}_{slug}{ext}"
        out_file.write_bytes(resp.content)
        print(f"  [Fallback:ElevenLabs] ✓ AI 효과음 생성 완료: {out_file.name} ('{prompt_en[:45]}...')")
        return (
            out_file,
            "https://api.elevenlabs.io/v1/sound-generation",
            f"ElevenLabs Sound Generation ({cfg.elevenlabs_sfx_model})",
        )
    except Exception as exc:
        print(f"  ⚠ ElevenLabs API 호출 예외: {exc}")
        return None


def match_and_procure_cues(
    cue_sheet: SfxCueSheet,
    cfg: Optional[SfxDirectorConfig] = None,
    catalog: Optional[SfxCatalog] = None,
    save_updated_cue_sheet: Optional[Path] = None,
) -> tuple[SfxCueSheet, SfxCatalog, dict[str, int]]:
    """큐시트의 모든 효과음을 1순위 로컬 카탈로그 매칭 → 2순위 Freesound → 3순위 ElevenLabs 순으로 해결."""
    cfg = cfg or load_director_config()
    if catalog is None:
        catalog = load_catalog(cfg.catalog_path, cfg.local_sfx_dir)

    usage_counts: dict[str, int] = {}
    catalog_dirty = False
    chain = cfg.effective_fallback_chain
    stats = {
        "local_matched": 0,
        "freesound_acquired": 0,
        "elevenlabs_generated": 0,
        "local_soft_fallback": 0,
        "unmatched_skipped": 0,
    }

    print(
        f"[SFX Matcher] 큐 {len(cue_sheet.cues)}개 매칭 시작 | "
        f"카탈로그 보유 {len(catalog.items)}개 | 활성 수급 체인: {' → '.join(chain)}"
    )

    for cue in cue_sheet.cues:
        best_item: Optional[SfxCatalogItem] = None
        best_path: Optional[Path] = None
        best_score = -1.0

        for item in catalog.items:
            sc = score_catalog_candidate(cue, item, usage_counts)
            if sc > best_score:
                p = resolve_catalog_item_path(item, cfg.local_sfx_dir)
                if p is not None:
                    best_score = sc
                    best_item = item
                    best_path = p

        # 1순위: 임계치 이상 로컬 효과음 즉시 채택
        if best_item is not None and best_path is not None and best_score >= cfg.local_match_threshold:
            cue.matched_sfx_id = best_item.id
            cue.matched_file = str(best_path)
            cue.matched_source = "local_catalog"
            cue.match_score = best_score
            cue.sfx_peak_time_sec = best_item.peak_time_sec if cue.align_to_peak else 0.0
            usage_counts[best_item.id] = usage_counts.get(best_item.id, 0) + 1
            stats["local_matched"] += 1
            continue

        # 2순위 & 3순위: 외부 자동 수급 (Freesound -> ElevenLabs)
        acquired: Optional[tuple[Path, str, str]] = None
        acquired_src = ""
        if "freesound" in chain:
            acquired = fetch_from_freesound(cue, cfg)
            if acquired:
                acquired_src = "freesound"
        if acquired is None and "elevenlabs" in chain:
            acquired = generate_with_elevenlabs(cue, cfg)
            if acquired:
                acquired_src = "elevenlabs"

        if acquired is not None:
            fpath, ext_url, lic_info = acquired
            try:
                # 새로 구해온 사운드도 즉시 DSP + 메타데이터 분석 후 카탈로그에 영구 추가하여 다음 큐부터 재사용!
                new_item = analyze_single_audio_file(
                    fpath,
                    cfg.local_sfx_dir,
                    cfg,
                    source=acquired_src,
                    external_url=ext_url,
                    license_info=lic_info,
                )
                # 큐 의도 키워드를 태그에 보강하여 동일 유형 재요청 시 로컬 1순위로 바로 적중되도록 처리
                new_item.category = cue.category
                new_item.tags_ko = list(dict.fromkeys([*cue.search_keywords_ko, *new_item.tags_ko]))[:10]
                new_item.tags_en = list(dict.fromkeys([*cue.search_keywords_en, *new_item.tags_en]))[:10]
                catalog.items.append(new_item)
                catalog_dirty = True

                cue.matched_sfx_id = new_item.id
                cue.matched_file = str(fpath)
                cue.matched_source = acquired_src
                cue.match_score = 1.0
                cue.sfx_peak_time_sec = new_item.peak_time_sec if cue.align_to_peak else 0.0
                usage_counts[new_item.id] = usage_counts.get(new_item.id, 0) + 1
                if acquired_src == "freesound":
                    stats["freesound_acquired"] += 1
                else:
                    stats["elevenlabs_generated"] += 1
                continue
            except Exception as exc:
                print(f"  ⚠ 수급된 오디오 카탈로그 등록 실패 ({fpath.name}): {exc}")

        # 4순위 (무중단 보장): 로컬 전용 모드이거나 외부 API 실패 시 소프트 하한 이상의 차선책 로컬 사운드 사용
        if best_item is not None and best_path is not None and best_score >= cfg.local_soft_floor:
            cue.matched_sfx_id = best_item.id
            cue.matched_file = str(best_path)
            cue.matched_source = "local_soft_fallback"
            cue.match_score = best_score
            cue.sfx_peak_time_sec = best_item.peak_time_sec if cue.align_to_peak else 0.0
            usage_counts[best_item.id] = usage_counts.get(best_item.id, 0) + 1
            stats["local_soft_fallback"] += 1
        else:
            stats["unmatched_skipped"] += 1
            print(
                f"  ⚠ [{cue.timestamp_str}] '{cue.sound_concept_ko}' 에 적합한 효과음이 없어 해당 포인트만 건너뜁니다 "
                f"(최고 로컬 점수={max(best_score, 0.0):.2f})."
            )

    if catalog_dirty:
        try:
            save_catalog_atomic(catalog, cfg.catalog_path)
            print(f"[SFX Matcher] 새로 수급된 효과음을 카탈로그에 자동 저장했습니다 → {cfg.catalog_path}")
        except Exception as exc:
            print(f"  ⚠ 카탈로그 저장 경고: {exc}")

    if save_updated_cue_sheet:
        Path(save_updated_cue_sheet).write_text(
            cue_sheet.model_dump_json(indent=2),
            encoding="utf-8",
        )

    return cue_sheet, catalog, stats
