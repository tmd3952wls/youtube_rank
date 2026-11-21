"""랭킹형 쇼츠 (TOP N 모음집) 자동 렌더러.

  python make_rank.py init <클립폴더> [--title "아빠로 살아가는 삶"]
      → 클립폴더/plan.json 템플릿 생성 (파일 이름순 = 7등부터 재생 순서)
  python make_rank.py preview <plan.json> [--item 0] [--at 1.0]
      → 해당 순위 화면 한 장을 PNG 로 (레이아웃 확인용, 몇 초)
  python make_rank.py render <plan.json> [--out final.mp4]
      → 1080x1920 완성본

화면 구성 (레퍼런스 8편 분석 기준)
  - 상단: 노란 주제 한 줄 + 회색 "랭킹 TOP7" (기울임, 검은 외곽선)
  - 영상: y=365 부터 가로 꽉 채움, 나머지 배경은 같은 영상을 흐리게
  - 왼쪽 순위표: 1등 빨강 / 2등 주황 / 3등 노랑 / 나머지 흰색. 재생 순서대로 라벨이 하나씩 채워짐
  - 리액션 자막: 검은 박스 + 흰 글씨
  - 순위가 바뀔 때 짧은 검은 화면 (순위표만 보임)
"""
import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageFilter

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
W, H = 1080, 1920
FONT_HEAVY = ROOT / "assets/fonts/BlackHanSans-Regular.ttf"
FONT_SUB_CANDIDATES = [
    ROOT / "assets/fonts/sub.ttf",
    Path("C:/Windows/Fonts/malgunbd.ttf"),
    Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"),
    FONT_HEAVY,
]


def _resolve_default_sfx_dir() -> str:
    """LOCAL_SFX_DIR 환경변수/.env 우선 → 효과음2869개 폴더 존재 시 사용 → 기존 효과음600개 폴백."""
    import os
    env_val = os.environ.get("LOCAL_SFX_DIR", "").strip()
    if not env_val:
        for env_p in (ROOT.parent / ".env", ROOT.parent / "secrets.env"):
            if env_p.is_file():
                for ln in env_p.read_text(encoding="utf-8", errors="replace").splitlines():
                    s = ln.strip()
                    if s and not s.startswith("#") and "=" in s:
                        k, v = s.split("=", 1)
                        if k.strip() == "LOCAL_SFX_DIR" and v.strip():
                            env_val = v.strip().strip('"').strip("'")
                            break
            if env_val:
                break
    if env_val:
        return env_val
    cand_2869 = Path("C:/Users/tmd39/Desktop/효과음/효과음2869개")
    try:
        if cand_2869.exists():
            return str(cand_2869)
    except OSError:
        pass
    return "C:/Users/tmd39/Desktop/효과음/효과음600개"


DEFAULTS = {
    "template": "black",       # "black" = 위아래 검정 띠 + 긴 영상 (기본) / "blur" = 흐린 배경
    "subtitle": None,          # None → "랭킹 TOP{N}"
    "hook_text": "(다들 몇 번이 제일 좋아?)",  # 제목 위 작은 질문 한 줄 (black 템플릿, "" = 없음)
    "fps": 30,
    "gap": 0.25,               # 순위 사이 검은 화면 (초). 0 이면 없음. 클립마다 "gap" 으로 바꿈 (그 클립 '앞' 암전)
    "end_gap": 0.9,            # 1위가 끝난 뒤 검은 화면 (완성 순위표 유지) → 0.0초 첫 장면으로 루프. 0 이면 없음
    "transition_mode": "default",  # "clean_drop" = 전환·오프닝·엔딩에 저음 드롭 1종만 정점 정렬
    "clean_drop": {"sfx": "007_쿵 효과음", "vol": 0.9},
    "end_sfx": None,           # 엔딩 암전 시작 소리 (None = clean_drop 이면 드롭, 아니면 transition 의 hit)
    "video_top": 348,          # 영상 칸 시작 y
    "video_area_h": 1348,      # 영상 칸 높이 (black: 348~1696, 아래는 검정 띠)
    "video_min_h": 1348,       # 가로 영상도 칸을 꽉 채움(좌우 잘림). 덜 자르려면 클립에 "box_h"
    "video_max_h": 1348,       # 세로 영상은 이 높이에서 잘림
    # 순위표: "1." "2." … 반투명, 얇은 외곽선 (사용자 레퍼런스 2026-11-12)
    "list_x": 70, "list_y": 430, "list_step": 75,
    "rank_size": 56, "label_size": 54, "list_weight": 800, "list_stroke": 3, "label_gap": 14,
    "list_alpha": 0.7,         # 순위표 전체 불투명도 (2026-11-12 사용자 요청으로 조금 더 흐리게)
    "rank_word_until": 0,      # N 이하 순위는 "N등" (0 = 모두 "N.")
    "rank_colors": {"1": "#ff4646", "2": "#d98a3e", "3": "#f2dc48"},
    "label_fade": 0.6,         # 새 라벨이 나타나는 시간(초)
    "label_blur": 12,          # 처음 흐림 정도
    "label_delay": 0.1,        # 클립 시작 후 몇 초 뒤 나타나기 시작
    "sub_y": 1280, "sub_size": 44,
    "sub_max_words": 3,        # 자막은 한 줄, 2~3 어절
    "sub_anim": "pop",         # 자막 기본 애니메이션 (pop/shake/drop/zoom/none), 자막마다 "anim" 으로 바꿈
    "title_size": 100, "subtitle_size": 96, "hook_size": 48,
    "title_color": "#ffe23a",     # 주제(눈길 가는 부분) 노랑. [ ] 를 쓰면 그 부분만 노랑, 나머지 흰색
    "subtitle_color": "#ff3b3b",  # "랭킹 TOP N" 빨강
    "loudness": -14,           # 최종 LUFS
    # 효과음 폴더 (.env 의 LOCAL_SFX_DIR 또는 효과음2869개/600개 자동 감지)
    "sfx_dir": _resolve_default_sfx_dir(),
    "sfx_peak_db": -9,         # 효과음 정점 크기 기준 (원본 소리를 덮지 않게)
    "bgm_rms_db": -20,         # 장면별 배경음 평균 크기 기준 (vol 1.0 일 때). 원본 소리 밑에 깔림
    # 순위 전환: 휙(검은 화면 한가운데 정점) + 쿵(다음 영상 시작). 레퍼런스 분석 기반
    "transition": {"whoosh": ["190_훅", "009_쉭"], "whoosh_vol": 0.9,
                   "hit": "007_쿵", "hit_vol": 0.5, "whoosh_first": True},
    "sfx": None,               # (예전 방식) {"path": "...wav", "volume": 0.6}  순위 시작마다
    "bgm": None,               # {"path": "...mp3", "volume": 0.12}
    "narration": None,         # 선택: {"path": "tts.mp3", "volume": 1.0} 별도 나레이션 트랙
    "auto_sfx": None,          # None 이면 환경변수 AUTO_SFX_ENABLED 따름, true/false 로 개별 지정 가능
}


# ───────────────────────── 공통 ─────────────────────────
def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        print(" ".join(map(str, cmd)))
        print(r.stderr[-3000:])
        raise SystemExit(f"ffmpeg 실패 (코드 {r.returncode})")
    return r


def probe(path):
    r = run(["ffprobe", "-v", "error", "-show_entries",
             "stream=codec_type,width,height:stream_side_data=rotation:format=duration",
             "-of", "json", str(path)])
    d = json.loads(r.stdout)
    v = next((s for s in d["streams"] if s["codec_type"] == "video"), None)
    if v is None:
        raise SystemExit(f"영상 스트림이 없습니다: {path}")
    w, h = v["width"], v["height"]
    rot = 0
    for sd in v.get("side_data_list", []) or []:
        rot = int(sd.get("rotation", 0) or 0)
    if abs(rot) in (90, 270):
        w, h = h, w
    return {"w": w, "h": h, "dur": float(d["format"]["duration"]),
            "audio": any(s["codec_type"] == "audio" for s in d["streams"])}


# 순위 수 → 클립 하나 길이 권장 범위(초). 전체 30~45초 안팎이 되게
CLIP_LEN = {3: (10, 15), 4: (7, 11), 5: (5.5, 9), 6: (4.5, 7), 7: (3.5, 6)}


def load_plan(path):
    path = Path(path).resolve()
    plan = json.loads(path.read_text(encoding="utf-8"))
    base = dict(DEFAULTS)
    if plan.get("template") == "blur":  # 예전 흐린 배경 템플릿 (랭킹감자 스타일)
        base.update({"video_top": 365, "video_area_h": 1280, "video_min_h": 1000, "video_max_h": 1280,
                     "title_size": 104, "subtitle_size": 100})
    elif plan.get("template") == "ref_black_header":
        # 레퍼런스 7편 1:1 정밀 복제 + 상단 제목 영역(0~365px)만 순수 검정 블락 처리
        base.update({
            "header_style": "ref_exact",
            "hook_text": "",
            "video_top": 365,
            "video_area_h": 1555,
            "video_min_h": 1120,
            "video_max_h": 1555,
            "title_size": 98,
            "subtitle_size": 102,
            "title_y": 200,
            "subtitle_y": 296,
            "x_scale": 0.86,
            "rank_word_until": 3,  # 1·2·3위만 "N등" (사용자 결정 2026-11-18)
            # 전 영상 공통 브금 QKThr (사용자 결정 2026-11-21, 원본 현장음 밑에 깔림). plan 에서 "bgm": null 로 끌 수 있음
            "bgm": {"path": str(ROOT / "assets" / "bgm" / "QKThr.mp3"), "volume": 0.25},
            "rank_colors": {"1": "#ff3531", "2": "#ff9e3e", "3": "#ffef60"},
            "label_color": "#f2f4f7",
            "list_center_x": 116,
            "label_x": 172,
            "list_y": 431,
            "list_step": 78,
            "rank_size": 56,
            "label_size": 46,
            "list_stroke": 5,
            "label_stroke": 4,
            "list_alpha": 1.0,
            "label_blur": 0,
            "label_fade": 0.65,
            "label_delay": 0.067,
            "sub_y": 1272,
            "sub_size": 48,
            "sub_anim": "none",
            "sub_box_alpha": 255,
            "sub_max_words": 8,
            "gap": round(13 / 30, 6),
            "end_gap": round(13 / 30, 6),
            # 전환음: 기존 에피소드와 같은 저음 드롭(007_쿵 효과음) 사용 (사용자 결정 2026-11-18)
            "transition_mode": "clean_drop",
            "clean_drop": {"sfx": "007_쿵 효과음", "vol": 0.9},
            "auto_sfx": False,  # 자동 효과음 연출 끔 (사용자 결정 2026-11-18)
            "sfx_peak_db": -2.5,
            "loudness": -13.5,
        })
    cfg = {**base, **{k: v for k, v in plan.items() if k != "items"}}
    cfg["rank_colors"] = {**base["rank_colors"], **plan.get("rank_colors", {})}
    items = plan["items"]
    if not items:
        raise SystemExit("items 가 비어 있습니다")
    n = len(items)
    if cfg["subtitle"] is None:
        cfg["subtitle"] = f"랭킹 TOP{n}"
    for it in items:
        src = Path(it["src"])
        it["src"] = src if src.is_absolute() else (path.parent / src)
        if not it["src"].exists():
            raise SystemExit(f"클립이 없습니다: {it['src']}")
    for it in items:
        for s in it.get("subs", []):
            # "|" 로 나눈 여러 줄(3줄 괴성 팝업 등)은 의도된 연출이라 경고하지 않음
            if "|" in s["text"]:
                continue
            words = s["text"].split()
            if len(words) > cfg["sub_max_words"]:
                print(f"  ⚠ {it['rank']}등 자막이 깁니다 (한 줄 {cfg['sub_max_words']}어절 이하 권장): {s['text']}")
    ranks = [it["rank"] for it in items]
    if sorted(ranks) != list(range(1, n + 1)):
        raise SystemExit(f"rank 는 1~{n} 이 한 번씩 있어야 합니다: {ranks}")
    # 순위 수 3~7. 순위가 적을수록 클립 하나가 길어야 함 (3개 ≈ 10~15초, 7개 ≈ 4~6초)
    if not 3 <= n <= 7:
        print(f"  ⚠ 순위가 {n}개입니다 (3~7개 권장)")
    lo, hi = CLIP_LEN.get(n, (3.5, 6.0))
    # TOP6~7: 에피소드당 장호흡 클립(9~12초) 1개와 초단타 클립(2.5~3.8초) 1개는 의도된 리듬 변주로 봄
    vary = {"long": (9.0, 12.0), "short": (2.5, 3.8)} if n >= 6 else {}
    used = set()
    for it in items:
        d = float(it.get("end", 0)) - float(it.get("start", 0))
        if it.get("slow"):
            a, b, f = it["slow"]
            d += (b - a) * (f - 1)
        if it.get("freeze"):
            d += float(it["freeze"][1])
        if it.get("replay"):
            d += float(it["replay"]["end"]) - float(it["replay"]["start"])
        if it.get("end_freeze"):
            d += float(it["end_freeze"].get("dur", 0.65))
        if d > 0 and not lo * 0.8 <= d <= hi * 1.25:
            kind = next((k for k, (a, b) in vary.items() if a <= d <= b and k not in used), None)
            if kind:
                used.add(kind)
                continue
            print(f"  ⚠ {it['rank']}등 클립 {d:.1f}초 — TOP{n} 이면 {lo:.0f}~{hi:.0f}초 정도가 맞습니다")
    return path, cfg, items


def font(size, path=FONT_HEAVY):
    return ImageFont.truetype(str(path), size)


def sub_font(size):
    for p in FONT_SUB_CANDIDATES:
        if p.exists():
            return font(size, p)
    return font(size)


def label_font(size, weight=800):
    """순위 옆 라벨: 레퍼런스처럼 숫자보다 한 단계 가벼운 굵은 고딕."""
    p = ROOT / "assets/fonts/label.ttf"
    if p.exists():
        return font(size, p)
    vf = Path("C:/Windows/Fonts/NotoSansKR-VF.ttf")
    if vf.exists():
        f = font(size, vf)
        try:
            f.set_variation_by_axes([weight])
        except Exception:
            pass
        return f
    return font(size)


def hex_rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


# ───────────────────────── 그래픽 ─────────────────────────
def gradient_text(text, fnt, top, bottom, stroke, shadow=(7, 7), skew=0.18,
                  stroke_rgb=(10, 10, 10), outer=0):
    """세로 그라데이션 글자 + 외곽선(+바깥 검은 외곽선) + 그림자, 기울임. RGBA 이미지 반환."""
    stroke_total = stroke + outer
    pad = stroke_total * 2 + 40
    l, t, r, b = fnt.getbbox(text, stroke_width=stroke_total)
    tw, th = r - l + pad * 2, b - t + pad * 2
    org = (pad - l, pad - t)

    base = Image.new("L", (tw, th), 0)
    ImageDraw.Draw(base).text(org, text, font=fnt, fill=255)

    def layer(fill_img=None, color=None, sw=0):
        # PIL stroke_width 는 겹치는 획(ㅇ+ㅏ) 사이에 구멍을 내서, 마스크 팽창으로 외곽선을 만든다
        mask = base.filter(ImageFilter.MaxFilter(sw * 2 + 1)) if sw else base
        img = fill_img if fill_img is not None else Image.new("RGBA", (tw, th), color)
        out = Image.new("RGBA", (tw, th), (0, 0, 0, 0))
        out.paste(img, (0, 0), mask)
        return out

    grad = Image.new("RGBA", (tw, th))
    gd = ImageDraw.Draw(grad)
    c0, c1 = hex_rgb(top), hex_rgb(bottom)
    y0, y1 = org[1] + t, org[1] + b
    for y in range(th):
        k = min(max((y - y0) / max(y1 - y0, 1), 0), 1)
        gd.line([(0, y), (tw, y)], fill=tuple(int(c0[i] + (c1[i] - c0[i]) * k) for i in range(3)) + (255,))

    canvas = Image.new("RGBA", (tw + shadow[0], th + shadow[1]), (0, 0, 0, 0))
    sh = layer(color=(0, 0, 0, 170), sw=stroke_total).filter(ImageFilter.GaussianBlur(3))
    canvas.alpha_composite(sh, shadow)
    if outer:
        canvas.alpha_composite(layer(color=(10, 10, 10, 255), sw=stroke_total))
    canvas.alpha_composite(layer(color=tuple(stroke_rgb) + (255,), sw=stroke))
    canvas.alpha_composite(layer(fill_img=grad))
    if skew:
        cw, ch = canvas.size
        extra = int(ch * skew)
        canvas = canvas.transform((cw + extra, ch), Image.AFFINE,
                                  (1, skew, -extra, 0, 1, 0), resample=Image.BICUBIC)
    return canvas.crop(canvas.getbbox())


def outlined(draw, xy, text, fnt, fill, stroke, anchor="lm"):
    draw.text(xy, text, font=fnt, fill=fill, stroke_width=stroke, stroke_fill=(0, 0, 0), anchor=anchor)


def title_font(size):
    p = ROOT / "assets/fonts/Jua-Regular.ttf"  # 주아체 (OFL)
    return font(size, p if p.exists() else FONT_HEAVY)


def color_text(text, fnt, hi, base, stroke, shadow=(5, 5)):
    """글자색을 부분별로: [ ] 안은 hi 색, 밖은 base 색. 대괄호가 없으면 전체가 hi 색.
    검은 외곽선(마스크 팽창) + 그림자. RGBA 이미지 반환."""
    import re
    if "[" in text:
        spans = [(m.group(1) or m.group(2), bool(m.group(1)))
                 for m in re.finditer(r"\[([^\]]+)\]|([^\[]+)", text)]
    else:
        spans = [(text, True)]
    plain = "".join(s for s, _ in spans)
    pad = stroke * 2 + 40
    l, t, r, b = fnt.getbbox(plain)
    tw, th = r - l + pad * 2, b - t + pad * 2
    org = (pad - l, pad - t)
    mask = Image.new("L", (tw, th), 0)
    ImageDraw.Draw(mask).text(org, plain, font=fnt, fill=255)
    fill = Image.new("RGBA", (tw, th), (0, 0, 0, 0))
    d = ImageDraw.Draw(fill)
    x = org[0]
    for s, is_hi in spans:
        d.text((x, org[1]), s, font=fnt, fill=hex_rgb(hi if is_hi else base) + (255,))
        x += fnt.getlength(s)
    outline = mask.filter(ImageFilter.MaxFilter(stroke * 2 + 1))
    canvas = Image.new("RGBA", (tw + shadow[0], th + shadow[1]), (0, 0, 0, 0))
    sh = Image.new("RGBA", (tw, th), (0, 0, 0, 0))
    sh.paste((0, 0, 0, 170), (0, 0), outline)
    canvas.alpha_composite(sh.filter(ImageFilter.GaussianBlur(3)), shadow)
    ol = Image.new("RGBA", (tw, th), (0, 0, 0, 0))
    ol.paste((12, 12, 12, 255), (0, 0), outline)
    canvas.alpha_composite(ol)
    canvas.alpha_composite(fill)
    return canvas.crop(canvas.getbbox())


def fit_w(img, maxw=W - 60):
    if img.width > maxw:
        img = img.resize((maxw, int(img.height * maxw / img.width)), Image.LANCZOS)
    return img


def header_layer(cfg):
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    hstyle = cfg.get("header_style") or ("flat" if cfg["template"] == "black" else "gradient")
    if hstyle == "ref_exact":
        p_aggro = ROOT / "assets/fonts/AggroB.ttf"
        f_path = p_aggro if p_aggro.exists() else FONT_HEAVY
        xs = float(cfg.get("x_scale", 0.86))
        t1 = gradient_text(cfg["title"], font(cfg["title_size"], f_path), "#f7cc2e", "#cf9a14", 8, shadow=(6, 6), skew=0.18)
        t1 = t1.resize((max(1, int(round(t1.width * xs))), t1.height), Image.LANCZOS)
        t1 = fit_w(t1, W - 56)
        t2 = gradient_text(cfg["subtitle"], font(cfg["subtitle_size"], f_path), "#dcdedf", "#9b9ea3", 8, shadow=(6, 6), skew=0.18)
        t2 = t2.resize((max(1, int(round(t2.width * xs))), t2.height), Image.LANCZOS)
        t2 = fit_w(t2, W - 56)
        hy1 = int(cfg.get("title_y", 200))
        hy2 = int(cfg.get("subtitle_y", 296))
        img.alpha_composite(t1, ((W - t1.width) // 2, hy1 - t1.height // 2))
        img.alpha_composite(t2, ((W - t2.width) // 2, hy2 - t2.height // 2))
        return img
    if hstyle == "flat":
        # 검정 띠 위: (질문 한 줄, 흰색) / 주제 (주아체 노랑, [ ] 로 일부만 노랑 가능) / "랭킹 TOP N" (빨강)
        tf = title_font
        if cfg.get("hook_text"):
            t0 = color_text(cfg["hook_text"], tf(cfg["hook_size"]), "#ffffff", "#ffffff", 5)
            img.alpha_composite(t0, ((W - t0.width) // 2, 128 - t0.height // 2))
        t1 = fit_w(color_text(cfg["title"], tf(cfg["title_size"]), cfg["title_color"], "#ffffff", 8))
        t2 = fit_w(color_text(cfg["subtitle"], tf(cfg["subtitle_size"]), cfg["subtitle_color"],
                              cfg["subtitle_color"], 8))
        img.alpha_composite(t1, ((W - t1.width) // 2, 208 - t1.height // 2))
        img.alpha_composite(t2, ((W - t2.width) // 2, 294 - t2.height // 2))
        return img
    t1 = gradient_text(cfg["title"], font(cfg["title_size"]), "#fff04d", "#f2b100", 9)
    if t1.width > W - 60:
        t1 = t1.resize((W - 60, int(t1.height * (W - 60) / t1.width)), Image.LANCZOS)
    t2 = gradient_text(cfg["subtitle"], font(cfg["subtitle_size"]), "#ffffff", "#8f8f8f", 9)
    hy1 = int(cfg.get("title_y", 205 if cfg["video_top"] >= 360 else 190))
    hy2 = int(cfg.get("subtitle_y", 300 if cfg["video_top"] >= 360 else 284))
    img.alpha_composite(t1, ((W - t1.width) // 2, hy1 - t1.height // 2))
    img.alpha_composite(t2, ((W - t2.width) // 2, hy2 - t2.height // 2))
    return img


def _scaled_glyph(text, fnt, fill, stroke, xs=1.0):
    pad = stroke * 2 + 24
    l, t, r, b = fnt.getbbox(text, stroke_width=stroke)
    tw, th = max(1, r - l + pad * 2), max(1, b - t + pad * 2)
    im = Image.new("RGBA", (tw, th), (0, 0, 0, 0))
    ImageDraw.Draw(im).text((pad - l, pad - t), text, font=fnt, fill=fill, stroke_width=stroke, stroke_fill=(0, 0, 0))
    bb = im.getbbox()
    im = im.crop(bb) if bb else im
    if abs(xs - 1.0) > 1e-3:
        im = im.resize((max(1, int(round(im.width * xs))), im.height), Image.LANCZOS)
    return im


def list_fonts(cfg):
    if cfg.get("template") == "ref_black_header":
        p_num = ROOT / "assets/fonts/AggroM.ttf"
        p_lab = ROOT / "assets/fonts/EsamanruMed.ttf"
        fn = font(cfg["rank_size"], p_num) if p_num.exists() else label_font(cfg["rank_size"], cfg["list_weight"])
        fl = font(cfg["label_size"], p_lab) if p_lab.exists() else label_font(cfg["label_size"], cfg["list_weight"])
        return fn, fl
    return label_font(cfg["rank_size"], cfg["list_weight"]), label_font(cfg["label_size"], cfg["list_weight"])


def list_rows(cfg, n):
    """각 순위 줄의 (rank, 숫자 글자, 숫자 x, y, 라벨 x)."""
    d = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    fr, _ = list_fonts(cfg)
    step = cfg["list_step"] if n <= 8 else int(cfg["list_step"] * 8 / n)
    word_until = cfg["rank_word_until"]
    num_w = max(d.textlength(f"{r}.", font=fr) for r in range(1, n + 1))
    rows = []
    for i, rank in enumerate(range(1, n + 1)):
        y = cfg["list_y"] + i * step
        rt = f"{rank}등" if rank <= word_until else f"{rank}."
        if cfg.get("list_center_x") is not None:
            x = int(cfg["list_center_x"])
            lx = int(cfg.get("label_x", x + 56))
        else:
            x = cfg["list_x"]
            lx = x + max(num_w, d.textlength(rt, font=fr)) + cfg["label_gap"]
        rows.append((rank, rt, x, y, lx))
    return rows


def fade_alpha(img, a):
    if a >= 1:
        return img
    r, g, b, al = img.split()
    return Image.merge("RGBA", (r, g, b, al.point(lambda v: int(v * a))))


def draw_list_text(d, xy, text, fnt, fill, cfg):
    # 레퍼런스: 얇은 어두운 외곽선 + 흐린 그림자
    d.text(xy, text, font=fnt, fill=fill, stroke_width=cfg["list_stroke"],
           stroke_fill=(20, 20, 20), anchor="lm")


def rank_rgb(cfg, rank):
    return hex_rgb(cfg["rank_colors"].get(str(rank), "#ffffff"))


def list_layer(cfg, n, revealed):
    """revealed: {rank: label} 지금 화면에 다 보여 있는 라벨들."""
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    fr, fl = list_fonts(cfg)
    if cfg.get("template") == "ref_black_header":
        xs_num = float(cfg.get("x_scale", 0.86)) * 0.99
        xs_lab = float(cfg.get("x_scale", 0.86))
        lab_rgb = hex_rgb(cfg.get("label_color", "#f2f4f7"))
        for rank, rt, cx, y, lx in list_rows(cfg, n):
            g_num = _scaled_glyph(rt, fr, rank_rgb(cfg, rank), cfg["list_stroke"], xs=xs_num)
            img.alpha_composite(g_num, (cx - g_num.width // 2, y - g_num.height // 2))
            if revealed.get(rank):
                g_lab = _scaled_glyph(revealed[rank], fl, lab_rgb, int(cfg.get("label_stroke", 4)), xs=xs_lab)
                img.alpha_composite(g_lab, (lx, y - g_lab.height // 2))
        sh = img.split()[3].filter(ImageFilter.GaussianBlur(3)).point(lambda v: int(v * 0.6))
        shadow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        shadow.paste((0, 0, 0, 255), (3, 4), sh)
        shadow.alpha_composite(img)
        return fade_alpha(shadow, cfg["list_alpha"])
    d = ImageDraw.Draw(img)
    for rank, rt, x, y, lx in list_rows(cfg, n):
        draw_list_text(d, (x, y), rt, fr, rank_rgb(cfg, rank), cfg)
        if revealed.get(rank):
            # 1·2·3위는 이름도 번호와 같은 색
            draw_list_text(d, (lx, y), revealed[rank], fl, rank_rgb(cfg, rank), cfg)
    sh = img.split()[3].filter(ImageFilter.GaussianBlur(4)).point(lambda v: int(v * 0.55))
    shadow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    shadow.paste((0, 0, 0, 255), (3, 4), sh)
    shadow.alpha_composite(img)
    return fade_alpha(shadow, cfg["list_alpha"])


def label_anim_frames(cfg, n, rank, text, tmp, tag):
    """새 라벨이 흐릿하게 → 선명하게, 투명 → 반투명으로 서서히 나타나는 PNG 시퀀스.
    반환: (패턴 경로, x, y) — 마지막 프레임은 그대로 유지되도록 overlay 에서 repeat."""
    row = next(r for r in list_rows(cfg, n) if r[0] == rank)
    _, _, _, y, lx = row
    _, fl = list_fonts(cfg)
    pad = 40
    if cfg.get("template") == "ref_black_header":
        xs_lab = float(cfg.get("x_scale", 0.86))
        lab_rgb = hex_rgb(cfg.get("label_color", "#f2f4f7"))
        g_lab = _scaled_glyph(text, fl, lab_rgb, int(cfg.get("label_stroke", 4)), xs=xs_lab)
        base = Image.new("RGBA", (g_lab.width + pad * 2, g_lab.height + pad * 2), (0, 0, 0, 0))
        base.alpha_composite(g_lab, (pad, pad))
        sh = base.split()[3].filter(ImageFilter.GaussianBlur(3)).point(lambda v: int(v * 0.6))
        full = Image.new("RGBA", base.size, (0, 0, 0, 0))
        full.paste((0, 0, 0, 255), (3, 4), sh)
        full.alpha_composite(base)
        frames = max(2, int(round(cfg["label_fade"] * cfg["fps"])))
        for k in range(frames):
            p = k / (frames - 1)
            e = 1 - (1 - p) ** 2.5
            fade_alpha(full, cfg["list_alpha"] * e).save(tmp / f"lab_{tag}_{k:03d}.png")
        return tmp / f"lab_{tag}_%03d.png", int(lx - pad), int(y - g_lab.height // 2 - pad)
    l, t, r, b = fl.getbbox(text, stroke_width=cfg["list_stroke"], anchor="lm")
    base = Image.new("RGBA", (r - l + pad * 2, b - t + pad * 2), (0, 0, 0, 0))
    d = ImageDraw.Draw(base)
    draw_list_text(d, (pad - l, pad - t), text, fl, rank_rgb(cfg, rank), cfg)
    sh = base.split()[3].filter(ImageFilter.GaussianBlur(4)).point(lambda v: int(v * 0.55))
    full = Image.new("RGBA", base.size, (0, 0, 0, 0))
    full.paste((0, 0, 0, 255), (3, 4), sh)
    full.alpha_composite(base)
    frames = max(2, int(round(cfg["label_fade"] * cfg["fps"])))
    for k in range(frames):
        p = k / (frames - 1)
        e = 1 - (1 - p) ** 3  # ease-out
        f = full.filter(ImageFilter.GaussianBlur(cfg["label_blur"] * (1 - e))) if e < 1 else full
        fade_alpha(f, cfg["list_alpha"] * e).save(tmp / f"lab_{tag}_{k:03d}.png")
    # 이미지의 (pad, pad) 가 순위표 안 글자 위치 (lx + l, y + t) 에 오도록
    return tmp / f"lab_{tag}_%03d.png", int(lx + l - pad), int(y + t - pad)


SUB_COLORS = {"y": "#ffe23a", "yellow": "#ffe23a", "r": "#ff3b3b", "red": "#ff3b3b",
              "w": "#ffffff", "white": "#ffffff", "g": "#5dff6b", "green": "#5dff6b",
              "b": "#4dc3ff", "blue": "#4dc3ff", "o": "#ffa23a", "orange": "#ffa23a"}


def sub_spans(line):
    """'[y:대박] 이게 [r:되네]' → [('대박','#ffe23a'),(' 이게 ','#fff'),('되네','#ff3b3b')]"""
    import re
    out = []
    for m in re.finditer(r"\[(\w+):([^\]]+)\]|([^\[]+)", line):
        if m.group(3):
            out.append((m.group(3), "#ffffff"))
        else:
            out.append((m.group(2), SUB_COLORS.get(m.group(1).lower(), "#ffffff")))
    return out


def sub_image(text, cfg, box=True, upto=None, size_override=None):
    """검은 박스 + 글자. [y:..] 노랑, [r:..] 빨강 등 단어별 색.
    box=False: 박스 없이 두꺼운 검정 외곽선 글자만 (뇌정지 리액션 ??????).
    upto=k: 앞에서부터 k 글자만 그림 (typewriter 용, 캔버스 크기는 전체 기준 그대로)."""
    sz = int(size_override or cfg["sub_size"])
    if cfg.get("template") == "ref_black_header":
        p_esam = ROOT / "assets/fonts/EsamanruMed.ttf"
        f = font(sz, p_esam) if p_esam.exists() else sub_font(sz)
        xs = float(cfg.get("sub_x_scale", 0.88))
    else:
        f = sub_font(sz)
        xs = 1.0
    lines = [sub_spans(l) for l in text.split("|")]
    lh = int(sz * 1.32)
    widths = [sum(f.getlength(s) for s, _ in ln) for ln in lines]
    tw = max(1, int(max(widths)))
    stroke = 0 if box else max(3, int(sz * 0.13))
    px = (18 if xs == 1.0 else int(round(16 / xs))) + stroke
    py = (10 if xs == 1.0 else 13) + stroke
    b_alpha = int(cfg.get("sub_box_alpha", 225)) if box else 0
    img = Image.new("RGBA", (tw + px * 2, lh * len(lines) + py * 2), (0, 0, 0, b_alpha))
    dd = ImageDraw.Draw(img)
    left = upto if upto is not None else 10 ** 9
    for i, (ln, lw) in enumerate(zip(lines, widths)):
        x = (img.width - lw) / 2
        for s, c in ln:
            shown = s[:max(left, 0)]
            left -= len(s)
            if shown:
                dd.text((x, py + lh * i + lh // 2), shown, font=f, fill=hex_rgb(c), anchor="lm",
                        stroke_width=stroke, stroke_fill=(0, 0, 0))
            x += f.getlength(s)
    if abs(xs - 1.0) > 1e-3:
        img = img.resize((max(1, int(round(img.width * xs))), img.height), Image.LANCZOS)
    return img


def sub_len(text):
    """typewriter 로 칠 글자 수 (색 표시 [y:..] 와 줄바꿈 | 제외)."""
    return sum(len(s) for l in text.split("|") for s, _ in sub_spans(l))


def sub_anim_frames(img, anim, fps, tmp, tag, text=None, cfg=None, dur=None, box=True):
    """자막 등장 애니메이션 PNG 시퀀스 (고정 캔버스, 마지막 프레임 유지).
    pop: 작게→크게 튕김 / shake: 좌우 흔들림 / drop: 위에서 떨어져 통통 / zoom: 크게→제자리 쾅 / none
    typewriter: dur*0.75 동안 왼쪽부터 한 글자씩 채워지고 남은 시간 유지 (text·cfg·dur 필요)"""
    import math
    pad = int(max(img.size) * 0.35) + 30
    cw, ch = img.width + pad * 2, img.height + pad * 2
    if anim == "typewriter" and text is not None and cfg is not None:
        total = max(1, sub_len(text))
        n = max(2, int(round(float(dur or 1.2) * 0.75 * fps)))
        for k in range(n):
            upto = int(math.ceil(total * (k + 1) / n))
            fr = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
            fr.alpha_composite(sub_image(text, cfg, box=box, upto=upto), (pad, pad))
            fr.save(tmp / f"sa_{tag}_{k:03d}.png")
        return tmp / f"sa_{tag}_%03d.png", pad
    dur = {"pop": 0.28, "shake": 0.45, "drop": 0.35, "zoom": 0.22, "none": 0.04}.get(anim, 0.28)
    n = max(2, int(round(dur * fps)))
    for k in range(n):
        p = k / (n - 1)
        sc, dx, dy, al = 1.0, 0.0, 0.0, 1.0
        if anim == "pop":  # 0.4 → 1.2 로 튀어나왔다가 1.0 으로 안착
            if p < 0.5:
                q = p / 0.5
                sc = 0.4 + 0.8 * (1 - (1 - q) ** 2)
            else:
                q = (p - 0.5) / 0.5
                sc = 1.2 - 0.2 * q - 0.05 * math.sin(q * math.pi) * (1 - q)
            al = min(1.0, p * 4)
        elif anim == "shake":
            dx = 14 * math.sin(p * math.pi * 7) * (1 - p)
        elif anim == "drop":
            e = 1 - (1 - p) ** 2
            dy = -90 * (1 - e) + 14 * math.sin(p * math.pi * 3) * (1 - p) * (p > 0.6)
            al = min(1.0, p * 3)
        elif anim == "zoom":
            sc = 1.9 - 0.9 * (1 - (1 - p) ** 3)
            al = min(1.0, p * 3)
        fr = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
        im = img if abs(sc - 1) < 1e-3 else img.resize((max(1, int(img.width * sc)), max(1, int(img.height * sc))),
                                                       Image.LANCZOS)
        if al < 1:
            im = fade_alpha(im, al)
        fr.alpha_composite(im, (int((cw - im.width) / 2 + dx), int((ch - im.height) / 2 + dy)))
        fr.save(tmp / f"sa_{tag}_{k:03d}.png")
    return tmp / f"sa_{tag}_%03d.png", pad


# ───────────────────────── 렌더 ─────────────────────────
def video_box_h(info, cfg):
    natural = W * info["h"] / info["w"]
    return int(min(max(natural, cfg["video_min_h"]), cfg["video_max_h"])) // 2 * 2


def item_frames(cfg, items, idx, tmp, include_current=True):
    """idx 번째 순위의 오버레이 PNG (헤더+순위표) 경로.
    include_current=False 면 지금 순위 라벨은 빼고 그림 (애니메이션으로 따로 얹음)."""
    n = len(items)
    upto = idx + 1 if include_current else idx
    revealed = {it["rank"]: it.get("label", "") for it in items[:upto]}
    layer = header_layer(cfg)
    layer.alpha_composite(list_layer(cfg, n, revealed))
    p = tmp / f"ov_{idx:02d}_{int(include_current)}.png"
    layer.save(p)
    return p


def slow_map(it):
    """"slow": [a, b, 배율] (클립 안 원본 초) → 원본 시각을 결과 시각으로 바꾸는 함수와 늘어난 길이."""
    s = it.get("slow")
    fz = it.get("freeze")
    if fz and not s:  # "freeze": [t, 초] — 클립 안 t초 프레임을 정지 (레퍼런스 공중 정지컷)
        ft, fd = float(fz[0]), float(fz[1])
        return (lambda t: t if t < ft else t + fd), fd
    if not s:
        return (lambda t: t), 0.0
    a, b, f = float(s[0]), float(s[1]), float(s[2])

    def m(t):
        if t < a:
            return t
        if t < b:
            return a + (t - a) * f
        return t + (b - a) * (f - 1)
    return m, (b - a) * (f - 1)


def motion_track(it, start, dur, cfg):
    """클립에서 움직임이 몰린 가로 위치(0~1)를 시간별로 [(t, cx)]. 원본 crop·flip 반영."""
    import numpy as np
    cr = it.get("crop")
    vf = [f"crop=iw*{cr[2]}:ih*{cr[3]}:iw*{cr[0]}:ih*{cr[1]}"] if cr else []
    if it.get("flip"):
        vf.append("hflip")
    fps, gw = 10, 160
    vf += [f"fps={fps}", f"scale={gw}:-2", "format=gray"]
    r = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{start}", "-t", f"{dur}", "-i", str(it["src"]),
                        "-vf", ",".join(vf), "-f", "rawvideo", "-"], capture_output=True)
    pr = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{start}", "-t", "0.2", "-i", str(it["src"]),
                         "-vf", ",".join(vf), "-frames:v", "1", "-f", "rawvideo", "-"], capture_output=True)
    if not pr.stdout:
        return []
    gh = len(pr.stdout) // gw
    a = np.frombuffer(r.stdout, dtype=np.uint8)
    n = a.size // (gw * gh)
    if n < 3:
        return []
    fr = a[: n * gw * gh].reshape(n, gh, gw).astype(np.int16)
    xs = (np.arange(gw) + 0.5) / gw
    track, prev = [], 0.5
    for k in range(1, n):
        d = np.abs(fr[k] - fr[k - 1])
        d = d - np.median(d)  # 카메라가 움직일 때 깔리는 전체 변화는 빼기
        col = np.clip(d, 0, None).sum(axis=0) ** 2
        if col.sum() > 1e-6 and np.abs(fr[k] - fr[k - 1]).mean() > 1.5:
            prev = float((col * xs).sum() / col.sum())
        track.append(prev)
    track = np.array(track)
    w = max(1, int(fps * 0.6))  # 0.6초 이동평균으로 흔들림 줄이기
    sm = np.convolve(np.pad(track, (w, w), mode="edge"), np.ones(2 * w + 1) / (2 * w + 1), "valid")
    # 화면 이동 속도 제한 (초당 화면 폭의 70%)
    out, cur = [], float(sm[0])
    for k, v in enumerate(sm):
        step = 0.7 / fps
        cur += max(-step, min(step, float(v) - cur))
        out.append(((k + 1) / fps, cur))
    return out


def focus_expr(it, start, dur, cfg, src_w, src_h, bh, tmap):
    """가로로 잘리는 클립의 crop x 식. 주인공(움직임)이 화면 안에 오도록 시간에 따라 이동.
    우선순위: "focus": [[t, cx], ...] 수동 키프레임 > "focus_x" 고정 > 자동 추적."""
    scaled_w = max(W, src_w * bh / src_h)
    v = W / scaled_w  # 보이는 가로 비율
    if v >= 0.98 or ("focus_x" in it and "focus" not in it):
        return f"(iw-ow)*{float(it.get('focus_x', 0.5))}"
    if it.get("focus"):
        keys = [(float(t), float(c)) for t, c in it["focus"]]
    else:
        keys = motion_track(it, start, dur, cfg)[::3] or [(0.0, 0.5)]
    keys = [(tmap(t), min(max((c - v / 2) / (1 - v), 0.0), 1.0)) for t, c in keys]
    if len(keys) == 1:
        return f"(iw-ow)*{keys[0][1]:.4f}"
    expr = f"{keys[-1][1]:.4f}"
    for (t0, x0), (t1, x1) in reversed(list(zip(keys, keys[1:]))):
        seg = f"{x0:.4f}+({x1:.4f}-{x0:.4f})*(t-{t0:.3f})/{max(t1 - t0, 1e-3):.3f}"
        expr = f"if(lt(t,{t1:.3f}),{seg},{expr})"  # 식 전체를 ' ' 로 감싸므로 쉼표 이스케이프 불필요
    expr = f"if(lt(t,{keys[0][0]:.3f}),{keys[0][1]:.4f},{expr})"
    return f"(iw-ow)*({expr})"


def label_x_expr(cfg, lab_x):
    """"label_slide": N px — 라벨이 오른쪽 N px 에서 제자리로 미끄러져 들어옴 (페이드와 같은 감속 곡선)."""
    s = float(cfg.get("label_slide", 0) or 0)
    if s <= 0:
        return str(lab_x)
    d, f = float(cfg["label_delay"]), max(float(cfg["label_fade"]), 1e-3)
    return f"'{lab_x}+{s}*pow(max(0,1-max(0,t-{d})/{f}),2.5)'"


def punch_chain(punches, bh, dur, out_tag="[fg]"):
    """"punch": [{"at","dur","zoom","cx","cy"}, ...] — 클립 시간(초) 구간을 하드컷으로 확대(포인트 줌인) 후 복귀.
    [fgp] (W x bh) → out_tag. cx/cy 는 확대할 지점 (0~1, 영상 박스 기준)."""
    if not punches:
        return []
    ps = sorted(punches, key=lambda p: float(p["at"]))
    segs, t = [], 0.0
    for p in ps:
        a = max(t, float(p["at"]))
        b = min(dur, a + float(p.get("dur", 0.8)))
        if a > t:
            segs.append((t, a, None))
        if b > a:
            segs.append((a, b, p))
        t = b
    if t < dur:
        segs.append((t, None, None))
    out = [f"[fgp]split={len(segs)}" + "".join(f"[ps{k}]" for k in range(len(segs)))]
    for k, (a, b, p) in enumerate(segs):
        tr = f"trim={a:.3f}" + (f":{b:.3f}" if b is not None else "")
        f = f"[ps{k}]{tr},setpts=PTS-STARTPTS,setsar=1"
        if p:
            z = float(p.get("zoom", 1.6))
            cw, ch = int(W / z) // 2 * 2, int(bh / z) // 2 * 2
            cx, cy = float(p.get("cx", 0.5)), float(p.get("cy", 0.5))
            f += (f",crop={cw}:{ch}:{(W - cw) * cx:.0f}:{(bh - ch) * cy:.0f},"
                  f"scale={W}:{bh}:flags=lanczos,unsharp=5:5:0.6:5:5:0.0,setsar=1")
        out.append(f + f"[pz{k}]")
    out.append("".join(f"[pz{k}]" for k in range(len(segs))) + f"concat=n={len(segs)}:v=1:a=0{out_tag}")
    return out


def end_freeze_chain(ef, bh, base_dur, has_punch=False, in_tag="[fg0]", out_tag="[fg]"):
    """"end_freeze": {"at", "dur", "zoom", "cx", "cy"} — 본편이 끝난 직후(다음 순위 전환 직전),
    클립 안 결정적 순간(at 초, 미지정 시 본편 끝 직전) 한 프레임을 하드컷으로 확! 당겨 dur 초간 정지 박제."""
    if not ef:
        return []
    ef_dur = float(ef.get("dur", 0.8))
    at = min(max(float(ef.get("at", max(0.0, base_dur - 0.08))), 0.0), max(0.0, base_dur - 0.04))
    z = float(ef.get("zoom", 1.65))
    cw, ch = max(2, int(W / z) // 2 * 2), max(2, int(bh / z) // 2 * 2)
    cx, cy = min(max(float(ef.get("cx", 0.5)), 0.0), 1.0), min(max(float(ef.get("cy", 0.5)), 0.0), 1.0)
    ox, oy = int(round((W - cw) * cx)), int(round((bh - ch) * cy))
    head = [] if has_punch else [f"{in_tag}setsar=1,split=2[ef_main][ef_src]"]
    return [
        *head,
        f"[ef_src]trim=start={at:.3f},trim=end_frame=1,setpts=PTS-STARTPTS,"
        f"crop={cw}:{ch}:{ox}:{oy},scale={W}:{bh}:flags=lanczos,"
        f"unsharp=5:5:0.75:5:5:0.0,setsar=1,"
        f"tpad=stop_mode=clone:stop_duration={ef_dur + 0.05:.4f},trim=0:{ef_dur:.4f},setpts=PTS-STARTPTS[ef_frz]",
        f"[ef_main][ef_frz]concat=n=2:v=1:a=0{out_tag}",
    ]


def render_item(cfg, items, idx, tmp, out, still_at=None):
    it = items[idx]
    info = probe(it["src"])
    start = float(it.get("start", 0))
    end = float(it.get("end", info["dur"]))
    end = min(end, info["dur"])
    src_dur = end - start
    if src_dur <= 0.2:
        raise SystemExit(f"{it['rank']}등 클립 구간이 너무 짧습니다 (start={start}, end={end})")
    tmap, extra = slow_map(it)
    rp = it.get("replay")  # {"start","end","zoom","cx","cy"}: 본편 뒤 하드컷 확대 리플레이 (원본 초)
    rp_dur = (min(float(rp["end"]), info["dur"]) - float(rp["start"])) if rp else 0.0
    base_dur = src_dur + extra + rp_dur
    ef = it.get("end_freeze")  # {"at","dur","zoom","cx","cy"}: 본편 직후 결정적 순간 확대 정지 박제
    ef_dur = float(ef.get("dur", 0.8)) if ef else 0.0
    dur = base_dur + ef_dur
    bh = int(it["box_h"]) // 2 * 2 if it.get("box_h") else video_box_h(info, cfg)
    cr = it.get("crop") or [0, 0, 1, 1]
    sw, sh = info["w"] * cr[2], info["h"] * cr[3]
    fx_expr = focus_expr(it, start, src_dur, cfg, sw, sh, bh, tmap)
    up = max(bh / sh, W / sw)  # 확대 배율: 원본보다 키우면 살짝 선명하게
    sharp = ",unsharp=5:5:0.7:5:5:0.0" if up > 1.15 else ""
    if up > 1.6:
        print(f"  ⚠ {it['rank']}등 원본 화질이 낮습니다 ({int(sw)}x{int(sh)} → {up:.1f}배 확대). 더 높은 화질 원본을 쓰세요")
    ov = item_frames(cfg, items, idx, tmp, include_current=False)
    lab_pat, lab_x, lab_y = label_anim_frames(cfg, len(items), it["rank"], it.get("label", "") or " ",
                                              tmp, f"{idx:02d}")

    cmd = ["ffmpeg", "-y", "-v", "error", "-ss", f"{start}", "-t", f"{src_dur}", "-i", str(it["src"]),
           "-loop", "1", "-i", str(ov),
           "-framerate", str(cfg["fps"]), "-i", str(lab_pat)]
    if rp:
        cmd += ["-ss", f"{float(rp['start'])}", "-t", f"{rp_dur}", "-i", str(it["src"])]
    sb = 4 if rp else 3  # 자막 입력 시작 번호
    flip = ",hflip" if it.get("flip") else ""  # 주인공이 왼쪽 순위표에 가릴 때 좌우반전
    # 원본에 박힌 영어 자막 등 가리기: "blur": [[x, y, w, h], ...] (원본 화면 기준 0~1 비율)
    # 원본 먼저 자르기 (세로 영상 양옆 검은 띠 등): "crop": [x, y, w, h] (0~1 비율)
    cr = it.get("crop")
    crop = f",crop=iw*{cr[2]}:ih*{cr[3]}:iw*{cr[0]}:ih*{cr[1]}" if cr else ""
    pre = []
    vin, ain = "[0:v]", "[0:a]"
    sl = it.get("slow")
    if sl:  # 슬로모션: [a, b, 배율] 구간만 느리게 (소리도 같이)
        a0, b0, f0 = float(sl[0]), float(sl[1]), float(sl[2])
        pre += ["[0:v]split=3[p0][p1][p2]",
                f"[p0]trim=0:{a0},setpts=PTS-STARTPTS[q0]",
                f"[p1]trim={a0}:{b0},setpts=(PTS-STARTPTS)*{f0}[q1]",
                f"[p2]trim={b0},setpts=PTS-STARTPTS[q2]",
                "[q0][q1][q2]concat=n=3:v=1:a=0[vslow]"]
        vin = "[vslow]"
        if info["audio"] and still_at is None:
            tempo, chain = 1 / f0, []
            while tempo < 0.5:
                chain.append("atempo=0.5")
                tempo /= 0.5
            chain.append(f"atempo={tempo:.4f}")
            pre += ["[0:a]asplit=3[r0][r1][r2]",
                    f"[r0]atrim=0:{a0},asetpts=PTS-STARTPTS[u0]",
                    f"[r1]atrim={a0}:{b0},asetpts=PTS-STARTPTS,{','.join(chain)}[u1]",
                    f"[r2]atrim={b0},asetpts=PTS-STARTPTS[u2]",
                    "[u0][u1][u2]concat=n=3:v=0:a=1[aslow]"]
            ain = "[aslow]"
    fz = it.get("freeze")
    loopf = ""
    if fz:
        if sl:
            raise SystemExit(f"{it['rank']}등: slow 와 freeze 는 함께 쓸 수 없습니다")
        ft = float(fz[0])
        fi, nfz = int(round(ft * cfg["fps"])), max(1, int(round(float(fz[1]) * cfg["fps"])))
        loopf = f",loop=loop={nfz}:size=1:start={fi},setpts=N/FRAME_RATE/TB"
        if info["audio"] and still_at is None:  # 정지 동안 소리도 멈춤 (무음), 이후 원본 이어짐
            pre += ["[0:a]aresample=48000,aformat=channel_layouts=stereo,asplit=2[z0][z1]",
                    f"[z0]atrim=0:{ft},asetpts=PTS-STARTPTS[y0]",
                    f"[z1]atrim={ft},asetpts=PTS-STARTPTS[y2]",
                    f"anullsrc=r=48000:cl=stereo,atrim=0:{nfz / cfg['fps']:.4f}[y1]",
                    "[y0][y1][y2]concat=n=3:v=0:a=1[afrz]"]
            ain = "[afrz]"
    pre.append(f"{vin}fps={cfg['fps']}{loopf}{crop},setsar=1{flip}{'[wm]' if rp else '[w0]'}")
    if rp:
        crr = it.get("crop") or [0, 0, 1, 1]
        mw, mh = int(info["w"] * crr[2]) // 2 * 2, int(info["h"] * crr[3]) // 2 * 2
        z = float(rp.get("zoom", 1.5))
        cx, cy = float(rp.get("cx", 0.5)), float(rp.get("cy", 0.5))
        pre += [f"[3:v]fps={cfg['fps']}{crop},setsar=1{flip},"
                f"crop=iw/{z}:ih/{z}:(iw-iw/{z})*{cx}:(ih-ih/{z})*{cy},scale={mw}:{mh},setsar=1[wr]",
                f"[wm]scale={mw}:{mh},setsar=1[wm2]",
                "[wm2][wr]concat=n=2:v=1:a=0[w0]"]
        if info["audio"] and still_at is None:
            pre += [f"{ain}aresample=48000,aformat=channel_layouts=stereo,apad,"
                    f"atrim=0:{src_dur + extra:.3f}[am]",
                    "[3:a]aresample=48000,aformat=channel_layouts=stereo[ar]",
                    "[am][ar]concat=n=2:v=0:a=1[arp]"]
            ain = "[arp]"
    boxes = it.get("blur", [])
    if boxes:
        n = len(boxes)
        pre.append(f"[w0]split={n + 1}[bb0]" + "".join(f"[bc{k}]" for k in range(n)))
        for k, (bx, by, bw, bhh) in enumerate(boxes):
            pre.append(f"[bc{k}]crop=iw*{bw}:ih*{bhh}:iw*{bx}:ih*{by},"
                       f"boxblur=luma_radius=min(w\\,h)/6:luma_power=3[bl{k}]")
            pre.append(f"[bb{k}][bl{k}]overlay=main_w*{bx}:main_h*{by}[bb{k + 1}]")
        pre.append(f"[bb{n}]null[w1]")
    else:
        pre.append("[w0]null[w1]")
    ef_pad = f",tpad=stop_mode=clone:stop_duration={ef_dur + 0.05:.4f}" if ef else ""
    if cfg["template"] == "black":
        bgf = [*pre, "[w1]null[b]",
               f"color=black:s={W}x{H}:r={cfg['fps']}:d={dur + 0.1:.4f}[bg]"]
        vy = cfg["video_top"] + (cfg["video_area_h"] - bh) // 2
    elif cfg["template"] == "ref_black_header":
        bgf = [*pre, "[w1]split[a][b]",
               f"[a]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
               f"gblur=sigma=30,eq=brightness=-0.05:saturation=1.08,"
               f"drawbox=x=0:y=0:w={W}:h={cfg['video_top']}:color=black@1:t=fill{ef_pad}[bg]"]
        vy = cfg["video_top"]
    else:
        bgf = [*pre, "[w1]split[a][b]",
               f"[a]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
               f"gblur=sigma=28,eq=brightness=-0.06:saturation=1.1{ef_pad}[bg]"]
        vy = cfg["video_top"]
    has_p = bool(it.get("punch"))
    if has_p and ef:
        fg_out, p_split, p_out = "[fg_raw]", ["[fg_raw]setsar=1,split=2[fgp][ef_src]"], "[ef_main]"
    elif has_p:
        fg_out, p_split, p_out = "[fgp]", [], "[fg]"
    elif ef:
        fg_out, p_split, p_out = "[fg0]", [], "[fg0]"
    else:
        fg_out, p_split, p_out = "[fg]", [], "[fg]"
    fc = [*bgf,
          f"[b]scale={W}:{bh}:force_original_aspect_ratio=increase:flags=lanczos{sharp},"
          f"crop={W}:{bh}:'{fx_expr}':(ih-oh)*{float(it.get('focus_y', 0.5))}{fg_out}",
          *p_split,
          *punch_chain(it.get("punch"), bh, base_dur, out_tag=p_out),
          *end_freeze_chain(ef, bh, base_dur, has_punch=has_p, in_tag="[fg0]", out_tag="[fg]"),
          f"[bg][fg]overlay=0:{vy}:shortest=1[v0]",
          "[v0][1:v]overlay=0:0:shortest=1[v1a]",
          # 새 라벨: 클립 시작과 함께 흐릿→선명하게 나타나고, 마지막 프레임 유지
          f"[2:v]setpts=PTS+{cfg['label_delay']}/TB[lab]",
          f"[v1a][lab]overlay={label_x_expr(cfg, lab_x)}:{lab_y}:eof_action=repeat[v1]"]
    last = "v1"
    for j, s in enumerate(it.get("subs", [])):
        box = s.get("box", True) is not False
        img = sub_image(s["text"], cfg, box=box, size_override=s.get("size"))
        anim = s.get("anim", cfg["sub_anim"])
        pat, pad = sub_anim_frames(img, anim, cfg["fps"], tmp, f"{idx:02d}_{j:02d}",
                                   text=s["text"], cfg=cfg, dur=float(s.get("dur", 1.2)), box=box)
        cmd += ["-framerate", str(cfg["fps"]), "-i", str(pat)]
        a = float(s.get("at", 0))
        b = a + float(s.get("dur", 1.2))
        x, y = (W - img.width) // 2 - pad, int(s.get("y", cfg["sub_y"])) - img.height // 2 - pad
        fc.append(f"[{j + sb}:v]setpts=PTS+{a:.3f}/TB[sa{j}]")
        fc.append(f"[{last}][sa{j}]overlay={x}:{y}:eof_action=repeat:"
                  f"enable='between(t,{a:.3f},{b:.3f})'[s{j}]")
        last = f"s{j}"

    if still_at is not None:
        cmd += ["-filter_complex", ";".join(fc), "-map", f"[{last}]",
                "-ss", f"{still_at}", "-frames:v", "1", str(out)]
        run(cmd)
        return out

    vol = float(it.get("volume", 1.0))
    if info["audio"]:
        a_end = base_dur if ef else dur
        fc.append(f"{ain}aresample=48000,aformat=channel_layouts=stereo,volume={vol},"
                  f"apad,atrim=0:{a_end:.3f},afade=t=in:d=0.02,"
                  f"afade=t=out:st={max(a_end - 0.04, 0):.3f}:d=0.04,apad,atrim=0:{dur:.3f}[aout]")  # 컷 경계 '틱' 방지
    else:
        fc.append(f"anullsrc=r=48000:cl=stereo,atrim=0:{dur:.3f}[aout]")
    cmd += ["-filter_complex", ";".join(fc), "-map", f"[{last}]", "-map", "[aout]",
            "-t", f"{dur:.3f}", *ENC, str(out)]
    run(cmd)
    return dur


ENC = ["-c:v", "libx264", "-preset", "slow", "-crf", "16", "-tune", "film", "-pix_fmt", "yuv420p",
       "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2"]


def item_gap(cfg, items, i):
    """i 번째 클립 '앞' 암전 길이 (클립의 "gap" → 없으면 최상위 gap). 첫 클립 앞은 없음."""
    if i <= 0:
        return 0.0
    g = items[i].get("gap")
    return max(float(cfg["gap"] if g is None else g), 0.0)


def render_gap(cfg, items, idx, tmp, out, g=None):
    """idx 번째 순위가 끝난 뒤 검은 화면 (그때까지 공개된 순위표만)."""
    ov = item_frames(cfg, items, idx, tmp)
    g = float(cfg["gap"] if g is None else g)
    nf = max(1, int(round(g * cfg["fps"])))
    real_g = nf / cfg["fps"]
    run(["ffmpeg", "-y", "-v", "error",
         "-f", "lavfi", "-i", f"color=black:s={W}x{H}:r={cfg['fps']}:d={real_g + 0.1:.4f}",
         "-loop", "1", "-i", str(ov),
         "-f", "lavfi", "-i", f"anullsrc=r=48000:cl=stereo",
         "-filter_complex", "[0:v][1:v]overlay=0:0:shortest=1,setsar=1[v]",
         "-map", "[v]", "-map", "2:a", "-frames:v", str(nf), "-t", f"{real_g:.6f}", *ENC, str(out)])
    return real_g


# ───────────────────────── 효과음 ─────────────────────────
_SFX_CACHE = {}


def find_sfx(name, cfg, plan_dir):
    """이름으로 효과음 파일 찾기: 경로 → sfx_dir 안 파일명 → '번호_' 를 뗀 이름이 같은 것 → 포함하는 것."""
    p = Path(name)
    for cand in (p, plan_dir / p, ROOT / p):
        if cand.is_file():
            return cand
    d = Path(cfg["sfx_dir"])
    if not d.is_dir():
        raise SystemExit(f"효과음 폴더가 없습니다: {d}  (plan.json 의 \"sfx_dir\" 로 지정)")
    files = [f for f in d.rglob("*") if f.is_file() and f.suffix.lower() in (".mp3", ".wav", ".ogg", ".m4a", ".flac", ".aac")]
    key = name.lower()
    for test in (lambda s: s.lower() == key,
                 lambda s: s.split("_", 1)[-1].lower() == key,
                 lambda s: key in s.lower()):
        hit = sorted(f for f in files if test(f.stem))
        if hit:
            return hit[0]
    raise SystemExit(f"효과음을 찾을 수 없습니다: {name}  (폴더: {d})")


def sfx_info(path):
    """(정점 dBFS, 정점 시각 초, 길이 초) — 정점을 컷에 맞추고 크기를 고르게 하려고."""
    import numpy as np
    if path in _SFX_CACHE:
        return _SFX_CACHE[path]
    r = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-ac", "1", "-ar", "24000",
                        "-f", "f32le", "-"], capture_output=True)
    a = np.frombuffer(r.stdout, dtype=np.float32)
    if a.size == 0:
        raise SystemExit(f"효과음을 읽을 수 없습니다: {path}")
    hop = 240
    env = np.array([np.abs(a[i:i + hop]).max() for i in range(0, a.size, hop)])
    i = int(env.argmax())
    # 평균 크기(RMS, 소리 나는 부분만): 배경음은 정점 대신 이걸로 맞춤 (잔잔한 곡이 묻히지 않게)
    blk = 2400
    rms = np.sqrt(np.array([np.mean(a[j:j + blk] ** 2) for j in range(0, max(a.size - blk, 1), blk)]) + 1e-12)
    act = rms[rms > rms.max() * 0.05] if rms.size else rms
    rms_db = 20 * np.log10(float(np.sqrt(np.mean(act ** 2))) if act.size else 1e-6)
    info = (20 * np.log10(max(float(env[i]), 1e-6)), i * hop / 24000, a.size / 24000, rms_db)
    _SFX_CACHE[path] = info
    return info


def sfx_events(cfg, items, starts, ends, plan_dir):
    """[(파일, 영상 기준 시작 초, 선형 게인, 이름)] — 정점을 목표 시점에 맞추고 정점 크기를 맞춤."""
    plan_dir = Path(plan_dir).parent if Path(plan_dir).is_file() else Path(plan_dir)
    ev = []

    def add(name, at, vol, align_peak, dur=None, src_off=0.0, fade_in=0.0, bgm=False, kind="sfx"):
        path = find_sfx(name, cfg, plan_dir)
        pk_db, pk_t, _, rms_db = sfx_info(path)
        base = (cfg["bgm_rms_db"] - rms_db) if bgm else (cfg["sfx_peak_db"] - pk_db)
        gain_db = base + 20 * __import__("math").log10(max(vol, 1e-4))
        ev.append((path, at - (pk_t if align_peak else 0.0), 10 ** (gain_db / 20), path.stem, dur,
                   src_off, fade_in, kind))

    # 장면별 배경음: 클립 시작부터 다음 클립 시작까지. 같은 곡이 이어지면 끊김 없이 이어서 재생
    run_off, prev = {}, None
    for i, it in enumerate(items):
        b = it.get("bgm")
        if not b:
            prev = None
            continue
        b = {"sfx": b} if isinstance(b, str) else b
        name = b["sfx"]
        seg_end = starts[i + 1] if i + 1 < len(items) else ends[i]
        length = seg_end - starts[i]
        off = run_off.get(name, float(b.get("from", 0))) if prev == name else float(b.get("from", 0))
        add(name, starts[i], float(b.get("vol", 0.35)), False, dur=length + 0.15, src_off=off,
            fade_in=0.08 if prev != name else 0.0, bgm=True, kind="bgm")
        run_off[name], prev = off + length, name

    tr = cfg.get("transition") or {}
    whooshes = tr.get("whoosh") or []
    end_at = ends[-1]  # 1위 칼컷 = 엔딩 암전 시작
    has_end = float(cfg.get("end_gap") or 0) > 0
    if cfg.get("transition_mode") in ("none", "off", False):
        pass
    elif cfg.get("transition_mode") == "ref_hum":
        # 레퍼런스 7편 실측: 0.0초 전환음 없음, 모든 순위 사이 암전(0.433초) + 엔딩 암전에만 45.4Hz 서브베이스 험 단독 재생
        hum_sfx = str(ROOT / "assets/sfx/ref_gap_hum.wav")
        hum_vol = float(cfg.get("hum_vol", 0.85))
        for i in range(1, len(items)):
            gdur = starts[i] - ends[i - 1]
            if gdur > 0.02:
                add(hum_sfx, ends[i - 1], hum_vol, False, dur=gdur)
        if has_end:
            add(hum_sfx, end_at, hum_vol, False, dur=float(cfg["end_gap"]))
    elif cfg.get("transition_mode") == "clean_drop":
        # 저음 드롭 1종만: 0.0초 오프닝, 순위 경계(암전이면 암전 진입, 하드컷이면 컷), 엔딩 암전 진입
        cd = cfg.get("clean_drop") or {}
        name, vol = cd.get("sfx", "007_쿵 효과음"), float(cd.get("vol", 0.9))
        add(name, 0.0, vol, True)
        for i in range(1, len(items)):
            add(name, ends[i - 1], vol, True)
        if has_end:
            add(cfg.get("end_sfx") or name, end_at, vol, True)
    else:
        for i in range(len(items)):
            if whooshes and (i > 0 or tr.get("whoosh_first", True)):
                # 휙: 검은 화면 한가운데에서 가장 크게 (첫 클립은 시작 직후, 하드컷이면 컷 순간)
                peak_at = (ends[i - 1] + starts[i]) / 2 if i > 0 else 0.12
                add(whooshes[i % len(whooshes)], peak_at, tr.get("whoosh_vol", 0.8), True)
            if tr.get("hit") and i > 0:
                add(tr["hit"], starts[i], tr.get("hit_vol", 0.45), True)  # 쿵: 다음 영상이 뜨는 순간
        end_name = cfg.get("end_sfx") or tr.get("hit") or (whooshes[0] if whooshes else None)
        if has_end and end_name:
            add(end_name, end_at, tr.get("hit_vol", 0.5), True)  # 마감 저음: 엔딩 암전 진입
    for i, it in enumerate(items):
        for fx in it.get("fx", []):
            add(fx["sfx"], starts[i] + float(fx["at"]), float(fx.get("vol", 1.0)), fx.get("peak", False),
                fx.get("dur"))
        st = it.get("sting")
        if st:  # 하이라이트 브금: 배경음을 순간 내리고 이 곡을 크게 (레퍼런스 2등 '캐치' 장면 방식)
            add(st["sfx"], starts[i] + float(st["at"]), float(st.get("vol", 1.0)), False,
                float(st.get("dur", 2.0)), float(st.get("from", 0)), 0.05, bgm=True, kind="sting")
    return sorted(ev, key=lambda e: e[1])


def sting_windows(items, starts):
    """[(시작, 끝, 배경음 배율, 원본 배율)] 하이라이트 브금이 나오는 동안 나머지를 내리는 구간."""
    out = []
    for i, it in enumerate(items):
        st = it.get("sting")
        if st:
            a = starts[i] + float(st["at"])
            out.append((a, a + float(st.get("dur", 2.0)), float(st.get("duck", 0.12)),
                        float(st.get("orig_duck", 0.55))))
    return out


def duck_expr(wins, which):
    """volume 필터 식: 구간 안에서 배율까지 0.12초에 내려가고 0.3초에 돌아옴."""
    if not wins:
        return None
    terms = []
    for a, b, g_bgm, g_orig in wins:
        g = g_bgm if which == "bgm" else g_orig
        ramp = f"min(max((t-{a - 0.12:.3f})/0.12,0),1)*min(max(({b + 0.3:.3f}-t)/0.3,0),1)"
        terms.append(f"(1-{1 - g:.3f}*{ramp})")
    return "*".join(terms)


def cmd_render(a):
    plan_path, cfg, items = load_plan(a.plan)
    out = Path(a.out) if a.out else plan_path.parent / "final.mp4"
    if not out.is_absolute():
        out = plan_path.parent / out
    tmp = Path(tempfile.mkdtemp(prefix="rank_"))
    try:
        parts, starts, ends, t = [], [], [], 0.0
        for i, it in enumerate(items):
            print(f"[{i + 1}/{len(items)}] {it['rank']}등 {it.get('label', '')}  ← {it['src'].name}")
            gi = item_gap(cfg, items, i)  # 이 클립 앞 암전 (0 = 하드컷)
            if gi > 0:
                g = tmp / f"gap_{i - 1:02d}.mp4"
                t += render_gap(cfg, items, i - 1, tmp, g, gi)
                parts.append(g)
            p = tmp / f"part_{i:02d}.mp4"
            starts.append(t)
            t += render_item(cfg, items, i, tmp, p)
            ends.append(t)
            parts.append(p)
        eg = float(cfg.get("end_gap") or 0)
        if eg > 0:  # 엔딩 루프 암전: 완성된 순위표 유지
            g = tmp / "gap_end.mp4"
            t += render_gap(cfg, items, len(items) - 1, tmp, g, eg)
            parts.append(g)
        lst = tmp / "list.txt"
        lst.write_text("".join(f"file '{p.as_posix()}'\n" for p in parts), encoding="utf-8")
        joined = tmp / "joined.mp4"
        run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(lst),
             "-c", "copy", str(joined)])
        total = probe(joined)["dur"]

        # 최종 오디오: 원본 + (효과음) + (BGM) → 라우드니스 맞춤
        cmd = ["ffmpeg", "-y", "-v", "error", "-i", str(joined)]
        mix, k = ["[0:a]"], 1
        fc = []
        # 효과음: 순위 전환(휙+쿵) + 클립 안 효과음(fx) + (활성화 시) AI 자동 효과음 디렉터
        events = sfx_events(cfg, items, starts, ends, plan_path)
        cli_auto = True if getattr(a, "auto_sfx", False) else (False if getattr(a, "no_auto_sfx", False) else None)
        cue_override = Path(a.cue_sheet).resolve() if getattr(a, "cue_sheet", None) else None
        from sfx_director.pipeline import run_auto_sfx_for_render
        auto_events = run_auto_sfx_for_render(
            joined_video_path=joined,
            plan_path=plan_path,
            plan_cfg=cfg,
            items=items,
            starts=starts,
            ends=ends,
            cli_auto_sfx=cli_auto,
            cue_sheet_override=cue_override,
        )
        if auto_events:
            events = sorted([*events, *auto_events], key=lambda e: e[1])
        wins = sting_windows(items, starts)
        dz_bgm, dz_orig = duck_expr(wins, "bgm"), duck_expr(wins, "orig")
        if dz_orig:
            fc.append(f"[0:a]volume='{dz_orig}':eval=frame[orig]")
            mix[0] = "[orig]"
        for j, (path, at, gain, name, dur, src_off, fade_in, kind) in enumerate(events):
            s0 = (-at if at < 0 else 0.0) + float(src_off)
            trim = f"atrim=start={s0:.3f}" + (f":end={s0 + float(dur):.3f}" if dur else "") + ","
            fade = (f"afade=t=out:st={max(float(dur) - 0.35, 0):.3f}:d=0.35," if dur else "")
            if fade_in:
                fade = f"afade=t=in:d={fade_in:.3f}," + fade
            ms = int(max(at, 0) * 1000)
            loop = ["-stream_loop", "-1"] if dur and (src_off or fade_in) else []  # 배경음은 반복 허용
            cmd += [*loop, "-i", str(path)]
            fc.append(f"[{k}:a]{trim}asetpts=PTS-STARTPTS,{fade}aresample=48000,"
                      f"aformat=channel_layouts=stereo,volume={gain:.4f},adelay={ms}|{ms}"
                      + (f",volume='{dz_bgm}':eval=frame" if dz_bgm and kind == "bgm" else "")
                      + f"[e{j}]")
            mix.append(f"[e{j}]")
            k += 1
        if events:
            print("효과음: " + ", ".join(f"{max(e[1], 0):.2f}s {e[3]}" for e in events))
        if cfg.get("sfx") and cfg["sfx"].get("path"):
            sp = Path(cfg["sfx"]["path"])
            sp = sp if sp.is_absolute() else plan_path.parent / sp
            for j, st in enumerate(starts):
                cmd += ["-i", str(sp)]
                ms = int(st * 1000)
                fc.append(f"[{k}:a]aresample=48000,aformat=channel_layouts=stereo,"
                          f"volume={cfg['sfx'].get('volume', 0.6)},adelay={ms}|{ms}[x{j}]")
                mix.append(f"[x{j}]")
                k += 1
        if cfg.get("narration"):
            nar = cfg["narration"] if isinstance(cfg["narration"], dict) else {"path": str(cfg["narration"])}
            if nar.get("path"):
                np_p = Path(nar["path"])
                np_p = np_p if np_p.is_absolute() else plan_path.parent / np_p
                cmd += ["-i", str(np_p)]
                fc.append(f"[{k}:a]aresample=48000,aformat=channel_layouts=stereo,"
                          f"volume={nar.get('volume', 1.0)},apad,atrim=0:{total:.3f}[nar]")
                mix.append("[nar]")
                k += 1
        if cfg.get("bgm") and cfg["bgm"].get("path"):
            bp = Path(cfg["bgm"]["path"])
            bp = bp if bp.is_absolute() else plan_path.parent / bp
            cmd += ["-stream_loop", "-1", "-i", str(bp)]
            fc.append(f"[{k}:a]aresample=48000,aformat=channel_layouts=stereo,"
                      f"volume={cfg['bgm'].get('volume', 0.12)},atrim=0:{total:.3f},"
                      f"afade=t=out:st={max(total - 1.5, 0):.3f}:d=1.5[bgm]")
            mix.append("[bgm]")
            k += 1
        # 2-pass loudnorm: 1차로 측정, 2차에서 측정값으로 선형 보정 (1-pass 는 목표보다 작게 나옴)
        mixed = f"{''.join(mix)}amix=inputs={len(mix)}:duration=first:normalize=0"
        ln = f"loudnorm=I={cfg['loudness']}:TP=-1.5:LRA=11"
        measure = [("info" if x == "error" else x) for x in cmd]  # loudnorm 측정값은 info 로그로 나옴
        r = run([*measure, "-filter_complex", ";".join(fc + [f"{mixed},{ln}:print_format=json[m]"]),
                 "-map", "[m]", "-f", "null", "-"])
        m = json.loads(r.stderr[r.stderr.rindex("{"): r.stderr.rindex("}") + 1])
        ln += (f":measured_I={m['input_i']}:measured_TP={m['input_tp']}:measured_LRA={m['input_lra']}"
               f":measured_thresh={m['input_thresh']}:offset={m['target_offset']}:linear=true")
        # 마지막 안전 리미터: loudnorm 이 선형 모드를 못 쓸 때 정점이 0dB 를 넘는 것 방지 (-2.5 dBFS, AAC 인코딩 오버슈트 여유)
        fc.append(f"{mixed},{ln},alimiter=limit=0.75:attack=1:release=50:level=false,"
                  f"aresample=48000,aformat=sample_rates=48000:channel_layouts=stereo[aout]")
        cmd += ["-filter_complex", ";".join(fc), "-map", "0:v", "-map", "[aout]",
                "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2", "-movflags", "+faststart", str(out)]
        run(cmd)
        print(f"\n완료: {out}  ({total:.1f}초)" + ("  ⚠ 60초 초과" if total > 60 else ""))
    finally:
        if a.keep_tmp:
            print(f"임시 폴더: {tmp}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)


def cmd_preview(a):
    plan_path, cfg, items = load_plan(a.plan)
    tmp = Path(tempfile.mkdtemp(prefix="rank_"))
    try:
        out = plan_path.parent / f"preview_{a.item:02d}.png"
        render_item(cfg, items, a.item, tmp, out, still_at=a.at)
        print(out)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def clip_audio(it, tmp, idx):
    """plan 구간의 오디오를 16kHz 모노 wav 로. 오디오가 없으면 None."""
    if not probe(it["src"])["audio"]:
        return None
    start = float(it.get("start", 0))
    end = float(it.get("end", probe(it["src"])["dur"]))
    wav = tmp / f"a_{idx:02d}.wav"
    run(["ffmpeg", "-y", "-v", "error", "-ss", f"{start}", "-t", f"{end - start}", "-i", str(it["src"]),
         "-vn", "-ac", "1", "-ar", "16000", str(wav)])
    return wav


def sound_peaks(wav, top=3, min_gap=0.8):
    """큰 소리가 갑자기 나는 순간(효과음·비명·충격음 후보) 상위 몇 개 (초)."""
    import numpy as np
    import soundfile as sf
    a, sr = sf.read(str(wav), dtype="float32")
    if a.ndim > 1:
        a = a.mean(axis=1)
    hop = int(sr * 0.05)
    n = len(a) // hop
    if n < 3:
        return []
    rms = np.sqrt(np.array([np.mean(a[i * hop:(i + 1) * hop] ** 2) for i in range(n)]) + 1e-12)
    db = 20 * np.log10(rms)
    jump = np.maximum(db - np.concatenate([[db[0]], db[:-1]]), 0) + (db - np.median(db)) * 0.5
    peaks = []
    for i in np.argsort(-jump):
        t = i * 0.05
        if db[i] < np.median(db) + 6:
            break
        if all(abs(t - p["at"]) >= min_gap for p in peaks):
            peaks.append({"at": round(float(t), 2), "db_over": round(float(db[i] - np.median(db)), 1)})
        if len(peaks) >= top:
            break
    return sorted(peaks, key=lambda p: p["at"])


def cmd_draft(a):
    """원본 소리 받아쓰기 + 큰 소리 시점 → plan.json 의 각 순위에 "heard"/"peaks" 기록.
    자막(subs)이 비어 있는 순위는 그 시점에 맞춘 자막 자리(text 는 원문)를 채워 둠."""
    plan_path = Path(a.plan).resolve()
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    _, cfg, items = load_plan(plan_path)
    from faster_whisper import WhisperModel
    import soundfile as sf
    model = WhisperModel(a.model, device="cpu", compute_type="int8")
    tmp = Path(tempfile.mkdtemp(prefix="rank_"))
    try:
        for idx, (raw, it) in enumerate(zip(plan["items"], items)):
            wav = clip_audio(it, tmp, idx)
            if wav is None:
                raw["heard"], raw["peaks"] = [], []
                print(f"{it['rank']}등: 소리 없음")
                continue
            audio, _ = sf.read(str(wav), dtype="float32")
            segs, info = model.transcribe(audio, vad_filter=True, word_timestamps=False)
            heard = [{"at": round(s.start, 2), "end": round(s.end, 2), "text": s.text.strip()}
                     for s in segs if s.text.strip() and s.no_speech_prob < 0.6]
            raw["heard"] = heard
            raw["peaks"] = sound_peaks(wav)
            print(f"{it['rank']}등 ({info.language}): " +
                  (" / ".join(f"{h['at']}s {h['text']}" for h in heard) or "말소리 없음") +
                  f"  | 큰 소리: {[p['at'] for p in raw['peaks']]}")
            if a.fill and not raw.get("subs"):
                marks = [{"at": h["at"], "text": h["text"]} for h in heard] or \
                        [{"at": p["at"], "text": "?"} for p in raw["peaks"][:1]]
                raw["subs"] = [{"at": max(0.0, m["at"] - 0.1), "dur": 1.6, "text": m["text"], "todo": True}
                               for m in marks[:3]]
        plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n저장: {plan_path}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi"}


def cmd_init(a):
    folder = Path(a.folder).resolve()
    clips = sorted(p for p in folder.iterdir() if p.suffix.lower() in VIDEO_EXT)
    if not clips:
        raise SystemExit(f"영상 파일이 없습니다: {folder}")
    n = len(clips)
    items = []
    for i, c in enumerate(clips):
        d = probe(c)["dur"]
        items.append({"rank": n - i, "label": "라벨", "src": c.name, "start": 0,
                      "end": round(min(d, 6.0), 2),
                      "subs": [{"at": 0.3, "dur": 1.2, "text": "(..)"}]})
    plan = {"title": a.title, "items": items}
    p = folder / "plan.json"
    if p.exists() and not a.force:
        raise SystemExit(f"이미 있습니다: {p}  (덮어쓰려면 --force)")
    p.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{p}  ({n}개 클립, 파일 이름순으로 {n}등 → 1등)")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("init")
    i.add_argument("folder")
    i.add_argument("--title", default="주제를 적어주세요")
    i.add_argument("--force", action="store_true")
    p = sub.add_parser("preview")
    p.add_argument("plan")
    p.add_argument("--item", type=int, default=0, help="재생 순서 번호 (0 = 첫 클립)")
    p.add_argument("--at", type=float, default=1.0, help="클립 안의 초")
    r = sub.add_parser("render")
    r.add_argument("plan")
    r.add_argument("--out")
    r.add_argument("--keep-tmp", action="store_true")
    r.add_argument("--auto-sfx", action="store_true", help="AUTO_SFX_ENABLED 값과 무관하게 AI 효과음 디렉터 강제 실행")
    r.add_argument("--no-auto-sfx", action="store_true", help="AUTO_SFX_ENABLED=true 여도 AI 효과음 디렉터 비활성화")
    r.add_argument("--cue-sheet", help="지정된 sfx_cue_sheet.json 파일을 사용하여 효과음 합성")
    dr = sub.add_parser("draft", help="원본 소리 받아쓰기 + 큰 소리 시점을 plan.json 에 기록")
    dr.add_argument("plan")
    dr.add_argument("--model", default="small")
    dr.add_argument("--fill", action="store_true", help="자막이 빈 순위에 자막 자리 채우기")
    a = ap.parse_args()
    {"init": cmd_init, "preview": cmd_preview, "render": cmd_render, "draft": cmd_draft}[a.cmd](a)


if __name__ == "__main__":
    main()
