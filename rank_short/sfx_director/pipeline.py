"""자동 효과음 디렉터 통합 오케스트레이터 및 CLI 엔트리포인트.

사용 예시 (로컬 PC / Antigravity `agy` 터미널):
  # 1) 로컬 효과음 폴더(2,869개) 증분 청취·카탈로그화
  python -m rank_short.sfx_director catalog

  # 2) 초안 렌더링 영상 시청 → JSON 큐시트 생성
  python -m rank_short.sfx_director cue ep_water/draft.mp4 --out ep_water/sfx_cue_sheet.json

  # 3) 큐시트 매칭 + 부족분 자동 수급(Freesound/ElevenLabs)
  python -m rank_short.sfx_director match ep_water/sfx_cue_sheet.json

  # 4) 음성 덕킹(-12~-18dB) + 피크 정렬 FFmpeg 최종 믹싱
  python -m rank_short.sfx_director mix ep_water/draft.mp4 ep_water/sfx_cue_sheet.json --out ep_water/final.mp4

  # 5) 원스톱 파이프라인 실행 (초안 MP4 → 카탈로그 증분 동기화 → 큐시트 → 수급 → 최종 MP4)
  python -m rank_short.sfx_director run ep_water/draft.mp4 --out ep_water/final_sfx.mp4
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

from .catalog import build_or_update_catalog, load_catalog
from .config import SfxDirectorConfig, load_director_config
from .cue_sheet import generate_sfx_cue_sheet
from .matcher_fallback import match_and_procure_cues
from .mixer import (
    analyze_voice_activity,
    compute_ducked_sfx_schedule,
    mix_sfx_into_video,
    to_make_rank_events,
)
from .schemas import SfxCueSheet

sys.stdout.reconfigure(encoding="utf-8")


def _safe_is_dir(p: Path) -> bool:
    try:
        return p.is_dir()
    except OSError:
        return False


def _safe_is_file(p: Path) -> bool:
    try:
        return p.is_file()
    except OSError:
        return False


def ensure_catalog_ready(cfg: SfxDirectorConfig, auto_index_limit: Optional[int] = None) -> None:
    """카탈로그 파일이 없거나 로컬 효과음 폴더에 새 파일이 있으면 증분 동기화."""
    if _safe_is_dir(cfg.local_sfx_dir):
        build_or_update_catalog(cfg=cfg, limit=auto_index_limit)
    elif _safe_is_file(cfg.catalog_path):
        print(f"[SFX Pipeline] 로컬 폴더 미접근 상태 → 기존 캐시 카탈로그 사용: {cfg.catalog_path}")
    else:
        print(
            f"[SFX Pipeline] ⚠ 로컬 효과음 폴더({cfg.local_sfx_dir})가 아직 없습니다. "
            f"활성 수급 체인({cfg.effective_fallback_chain})으로 진행합니다."
        )


def run_auto_sfx_for_render(
    joined_video_path: Path,
    plan_path: Path,
    plan_cfg: dict,
    items: list[dict],
    starts: list[float],
    ends: list[float],
    cli_auto_sfx: Optional[bool] = None,
    cue_sheet_override: Optional[Path] = None,
) -> list[tuple]:
    """`make_rank.py` 의 `cmd_render()` 내부(`joined.mp4` 직후)에서 호출되는 비침습 통합 훅.

    `AUTO_SFX_ENABLED=false` 이고 `--auto-sfx` 플래그도 없으면 빈 리스트 `[]` 를 즉시 반환하여
    기존 렌더링 동작을 100% 그대로 유지합니다.
    """
    cfg = load_director_config(plan_overrides=plan_cfg, cli_auto_sfx=cli_auto_sfx)
    if not cfg.auto_sfx_enabled and cue_sheet_override is None:
        return []

    print("\n━━━ [Auto SFX Director] 자동 효과음 연출 파이프라인 가동 ━━━")
    ep_dir = Path(plan_path).resolve().parent
    cue_out_path = Path(cue_sheet_override) if cue_sheet_override else (ep_dir / "sfx_cue_sheet.json")

    # 1. 로컬 카탈로그 증분 동기화
    ensure_catalog_ready(cfg)
    catalog = load_catalog(cfg.catalog_path, cfg.local_sfx_dir)

    # 2. 큐시트 로드 또는 초안 영상 시청 생성
    if cue_sheet_override and Path(cue_sheet_override).is_file():
        print(f"[Auto SFX Director] 지정된 기존 큐시트 사용: {cue_sheet_override}")
        sheet = SfxCueSheet.model_validate_json(Path(cue_sheet_override).read_text(encoding="utf-8"))
    else:
        sheet = generate_sfx_cue_sheet(
            video_path=joined_video_path,
            cfg=cfg,
            output_json_path=cue_out_path,
            items=items,
            starts=starts,
            ends=ends,
            plan_cfg=plan_cfg,
        )

    # 3. 하이브리드 매칭 및 부족분 외부 수급
    sheet, _, match_stats = match_and_procure_cues(
        sheet,
        cfg=cfg,
        catalog=catalog,
        save_updated_cue_sheet=cue_out_path,
    )
    print(f"[Auto SFX Director] 매칭 통계: {match_stats}")

    # 4. 음성(원본 대사 또는 plan.json 의 선택적 narration 트랙) VAD 기반 -12~-18dB 덕킹 및 피크 정렬
    narration_opt = plan_cfg.get("narration")
    narration_path: Optional[Path] = None
    if isinstance(narration_opt, dict) and narration_opt.get("path"):
        np_cand = Path(narration_opt["path"])
        narration_path = np_cand if np_cand.is_absolute() else (ep_dir / np_cand)
    elif isinstance(narration_opt, str) and narration_opt.strip():
        np_cand = Path(narration_opt.strip())
        narration_path = np_cand if np_cand.is_absolute() else (ep_dir / np_cand)

    voice_prof = analyze_voice_activity(joined_video_path, cfg, narration_override_path=narration_path)
    scheduled = compute_ducked_sfx_schedule(sheet, voice_prof, cfg)

    # 덕킹·게인이 기록된 최종 큐시트 재저장 (검수용)
    cue_out_path.write_text(
        json.dumps(sheet.model_dump(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    ducked_n = sum(1 for s in scheduled if s["overlaps_voice"])
    print(
        f"[Auto SFX Director] 큐시트 확정: 합성 대상 {len(scheduled)}개 "
        f"(음성 감지={voice_prof.has_voice}, 음성 구간 덕킹={ducked_n}개) → {cue_out_path.name}"
    )
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n")
    return to_make_rank_events(scheduled)


def run_full_sfx_pipeline(
    draft_video: Path,
    output_video: Optional[Path] = None,
    cue_sheet_path: Optional[Path] = None,
    narration_path: Optional[Path] = None,
    cfg: Optional[SfxDirectorConfig] = None,
    catalog_limit: Optional[int] = None,
) -> dict:
    """단독 실행용 End-to-End 파이프라인 (초안 MP4 → 카탈로그 → 큐시트 → 수급 → 최종 믹싱 MP4)."""
    cfg = cfg or load_director_config(cli_auto_sfx=True)
    draft_video = Path(draft_video).resolve()
    out_video = Path(output_video).resolve() if output_video else draft_video.with_name(draft_video.stem + "_sfx.mp4")
    cue_path = Path(cue_sheet_path).resolve() if cue_sheet_path else draft_video.with_name("sfx_cue_sheet.json")

    ensure_catalog_ready(cfg, auto_index_limit=catalog_limit)
    catalog = load_catalog(cfg.catalog_path, cfg.local_sfx_dir)

    sheet = generate_sfx_cue_sheet(video_path=draft_video, cfg=cfg, output_json_path=cue_path)
    sheet, catalog, match_stats = match_and_procure_cues(
        sheet, cfg=cfg, catalog=catalog, save_updated_cue_sheet=cue_path
    )
    mix_res = mix_sfx_into_video(
        draft_video_path=draft_video,
        cue_sheet=sheet,
        output_video_path=out_video,
        cfg=cfg,
        narration_audio_path=narration_path,
    )
    # 최종 덕킹 수치가 반영된 큐시트 갱신
    cue_path.write_text(json.dumps(sheet.model_dump(), ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "cue_sheet": str(cue_path),
        "output_video": str(out_video),
        "match_stats": match_stats,
        "mix": mix_res,
    }


def cli_main(argv: Optional[list[str]] = None) -> None:
    ap = argparse.ArgumentParser(
        prog="python -m rank_short.sfx_director",
        description="Gemini 멀티모달 기반 완전 자동 효과음(SFX) 디렉터 파이프라인",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    # status
    sub.add_parser("status", help="현재 환경변수, 로컬 효과음 폴더, 카탈로그 및 폴백 체인 상태 확인")

    # catalog
    p_cat = sub.add_parser("catalog", help="로컬 효과음 폴더 증분 청취 및 sfx_catalog.json 인덱싱")
    p_cat.add_argument("--sfx-dir", help="효과음 폴더 경로 (미지정 시 LOCAL_SFX_DIR 사용)")
    p_cat.add_argument("--out", help="카탈로그 JSON 저장 경로")
    p_cat.add_argument("--limit", type=int, help="이번 실행에서 신규 분석할 최대 파일 수 (대량 폴더 분할 처리용)")
    p_cat.add_argument("--force", action="store_true", help="기존 캐시를 무시하고 전체 재분석")
    p_cat.add_argument("--upgrade-heuristics", action="store_true", help="휴리스틱 분석 항목을 Gemini 청취로 업그레이드")

    # cue
    p_cue = sub.add_parser("cue", help="초안 영상을 시청하여 타임스탬프별 SFX 큐시트(JSON) 생성")
    p_cue.add_argument("video", help="초안 렌더링된 쇼츠 MP4 파일")
    p_cue.add_argument("--plan", help="선택: 에피소드 plan.json 경로 (자막·순위 컨텍스트 결합)")
    p_cue.add_argument("--out", help="큐시트 JSON 저장 경로 (기본: <영상폴더>/sfx_cue_sheet.json)")

    # match
    p_mat = sub.add_parser("match", help="큐시트에 필요한 효과음을 로컬 매칭 및 외부 API(Freesound/ElevenLabs)로 자동 수급")
    p_mat.add_argument("cue_sheet", help="sfx_cue_sheet.json 경로")
    p_mat.add_argument("--mode", choices=["auto", "local_only", "freesound", "elevenlabs"], help="수급 모드 강제 지정")

    # mix
    p_mix = sub.add_parser("mix", help="큐시트 기반으로 음성 덕킹(-12~-18dB) 및 피크 정렬하여 최종 MP4 합성")
    p_mix.add_argument("video", help="초안 MP4 파일")
    p_mix.add_argument("cue_sheet", help="매칭 완료된 sfx_cue_sheet.json 경로")
    p_mix.add_argument("--narration", help="선택: 별도 TTS 나레이션 오디오 파일 경로")
    p_mix.add_argument("--out", help="출력 최종 MP4 경로 (기본: *_sfx.mp4)")

    # run
    p_run = sub.add_parser("run", help="원스톱 실행: 카탈로그 증분 동기화 → 영상 시청 큐시트 → 수급 → 덕킹 믹싱")
    p_run.add_argument("video", help="초안 MP4 파일")
    p_run.add_argument("--narration", help="선택: 별도 TTS 나레이션 오디오 파일 경로")
    p_run.add_argument("--cue-sheet", help="큐시트 저장 경로")
    p_run.add_argument("--out", help="출력 최종 MP4 경로")
    p_run.add_argument("--catalog-limit", type=int, help="사전 카탈로그 인덱싱 최대 신규 파일 수")

    args = ap.parse_args(argv)
    cfg = load_director_config()

    if args.cmd == "status":
        sfx_exists = _safe_is_dir(cfg.local_sfx_dir)
        audio_cnt = (
            sum(1 for p in cfg.local_sfx_dir.rglob("*") if _safe_is_file(p) and p.suffix.lower() in {".mp3", ".wav", ".ogg", ".m4a", ".flac", ".aac"})
            if sfx_exists else 0
        )
        cat_cnt = load_catalog(cfg.catalog_path).total_count if _safe_is_file(cfg.catalog_path) else 0
        info = {
            "AUTO_SFX_ENABLED": cfg.auto_sfx_enabled,
            "LOCAL_SFX_DIR": str(cfg.local_sfx_dir),
            "local_sfx_dir_exists": sfx_exists,
            "local_audio_files_detected": audio_cnt,
            "catalog_path": str(cfg.catalog_path),
            "catalog_indexed_count": cat_cnt,
            "gemini_api_configured": bool(cfg.gemini_api_key),
            "gemini_audio_model": cfg.gemini_audio_model,
            "gemini_video_model": cfg.gemini_video_model,
            "freesound_api_configured": bool(cfg.freesound_api_key),
            "elevenlabs_api_configured": bool(cfg.elevenlabs_api_key),
            "effective_fallback_chain": cfg.effective_fallback_chain,
            "ducking_range_db_below_voice": [cfg.sfx_duck_db_max, cfg.sfx_duck_db_min],
        }
        print(json.dumps(info, ensure_ascii=False, indent=2))

    elif args.cmd == "catalog":
        _, stats = build_or_update_catalog(
            cfg=cfg,
            sfx_dir=Path(args.sfx_dir) if args.sfx_dir else None,
            catalog_path=Path(args.out) if args.out else None,
            limit=args.limit,
            force_rebuild=args.force,
            upgrade_heuristics=args.upgrade_heuristics,
        )
        print(json.dumps(stats, ensure_ascii=False, indent=2))

    elif args.cmd == "cue":
        vpath = Path(args.video).resolve()
        out_p = Path(args.out).resolve() if args.out else (vpath.parent / "sfx_cue_sheet.json")
        items = None
        if args.plan and Path(args.plan).is_file():
            plan_data = json.loads(Path(args.plan).read_text(encoding="utf-8"))
            items = plan_data.get("items")
        generate_sfx_cue_sheet(video_path=vpath, cfg=cfg, output_json_path=out_p, items=items)

    elif args.cmd == "match":
        if args.mode:
            cfg.fallback_mode = args.mode
        cpath = Path(args.cue_sheet).resolve()
        sheet = SfxCueSheet.model_validate_json(cpath.read_text(encoding="utf-8"))
        _, _, stats = match_and_procure_cues(sheet, cfg=cfg, save_updated_cue_sheet=cpath)
        print(json.dumps(stats, ensure_ascii=False, indent=2))

    elif args.cmd == "mix":
        vpath = Path(args.video).resolve()
        cpath = Path(args.cue_sheet).resolve()
        out_p = Path(args.out).resolve() if args.out else vpath.with_name(vpath.stem + "_sfx.mp4")
        npath = Path(args.narration).resolve() if args.narration else None
        sheet = SfxCueSheet.model_validate_json(cpath.read_text(encoding="utf-8"))
        res = mix_sfx_into_video(vpath, sheet, out_p, cfg=cfg, narration_audio_path=npath)
        cpath.write_text(json.dumps(sheet.model_dump(), ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({k: v for k, v in res.items() if k != "schedule"}, ensure_ascii=False, indent=2))

    elif args.cmd == "run":
        res = run_full_sfx_pipeline(
            draft_video=Path(args.video),
            output_video=Path(args.out) if args.out else None,
            cue_sheet_path=Path(args.cue_sheet) if args.cue_sheet else None,
            narration_path=Path(args.narration) if args.narration else None,
            cfg=cfg,
            catalog_limit=args.catalog_limit,
        )
        print(
            json.dumps(
                {
                    "cue_sheet": res["cue_sheet"],
                    "output_video": res["output_video"],
                    "match_stats": res["match_stats"],
                    "mixed_sfx_count": res["mix"]["mixed_sfx_count"],
                    "voice_ducked_count": res["mix"]["voice_ducked_count"],
                },
                ensure_ascii=False,
                indent=2,
            )
        )


if __name__ == "__main__":
    cli_main()
