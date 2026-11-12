"""유튜브 크리에이티브 커먼즈(CC BY) 영상에서 랭킹 쇼츠 소재 찾기·받기.

  python find_cc.py search <에피소드폴더> "funny dog" "dog fail" ... [--per 30] [--max-dur 600]
      → CC 필터로 검색 → 영상마다 라이선스 직접 확인 → <폴더>/candidates.txt (조회수 순)
  python find_cc.py get <에피소드폴더> <영상ID> [<영상ID> ...] [--section 영상ID=시작-끝 ...]
      → <폴더>/raw/y00.mp4 ... 로 받고 <폴더>/raw/sources.json 에 출처 기록
  python find_cc.py credits <plan.json>
      → plan 에 쓰인 클립의 출처 표기를 <폴더>/credits.txt 로 (Commons·유튜브 둘 다)

주의: CC 표시가 있어도 '모음집'은 남의 영상을 모아 CC 만 붙인 경우가 많아 안전하지 않음.
      원본을 직접 찍은 채널(반려동물 채널 등)의 영상을 쓰고, ⚠ 표시된 건 피할 것.
"""
import argparse
import json
import re
import subprocess
import sys
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

CC_FILTER = "EgIwAQ%3D%3D"  # 유튜브 검색 필터: 크리에이티브 커먼즈
RISK_WORDS = re.compile(r"compilation|funniest|funny .*videos|best of|try not to laugh|moments|"
                        r"모음|레전드|tiktok|reels|viral|fails? of|top \d+|#shorts? compilation", re.I)


def ytdlp(args, timeout=180):
    # 요청 사이 간격: 너무 빨리 많이 조회하면 유튜브가 "봇 확인"으로 한동안 막음
    r = subprocess.run(["yt-dlp", "--no-warnings", "--sleep-requests", "1.5", *args], capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=timeout)
    return r


class Blocked(Exception):
    pass


def search_ids(query, per):
    url = ("https://www.youtube.com/results?search_query=" + urllib.parse.quote_plus(query)
           + "&sp=" + CC_FILTER)
    r = ytdlp(["--flat-playlist", "-j", "--playlist-end", str(per), url])
    out = []
    for line in r.stdout.splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return out


def full_info(vid):
    r = ytdlp(["-j", "--skip-download", f"https://www.youtube.com/watch?v={vid}"])
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        if "not a bot" in r.stderr or "429" in r.stderr:
            raise Blocked(vid)
        return None


def risk(d):
    why = []
    if RISK_WORDS.search(d.get("title") or ""):
        why.append("모음집 제목")
    if (d.get("duration") or 0) > 300:
        why.append("5분 넘음")
    if RISK_WORDS.search(d.get("channel") or ""):
        why.append("모음 채널명")
    return why


def cmd_search(a):
    ep = Path(a.folder)
    ep.mkdir(parents=True, exist_ok=True)
    flat = {}
    for q in a.queries:
        for d in search_ids(q, a.per):
            if d.get("id") and (d.get("duration") or 0) <= a.max_dur:
                flat.setdefault(d["id"], {**d, "query": q})
        print(f"검색 '{q}': 누적 {len(flat)}개", flush=True)
    def safe(vid):
        try:
            return full_info(vid)
        except Blocked:
            return "blocked"
    with ThreadPoolExecutor(2) as ex:
        infos = list(ex.map(safe, flat))
    nb = sum(1 for i in infos if i == "blocked")
    if nb:
        print(f"⚠ 유튜브가 {nb}개 조회를 막았습니다 (봇 확인). 30분~1시간 뒤 다시 검색하세요.")
    infos = [None if i == "blocked" else i for i in infos]
    rows = []
    for vid, info in zip(flat, infos):
        if not info or "Creative Commons" not in (info.get("license") or ""):
            continue
        rows.append({"id": vid, "title": info.get("title"), "channel": info.get("channel"),
                     "views": info.get("view_count") or 0, "dur": info.get("duration"),
                     "w": info.get("width"), "h": info.get("height"),
                     "license": info.get("license"), "query": flat[vid]["query"],
                     "url": f"https://www.youtube.com/watch?v={vid}", "risk": risk(info)})
    for r in rows:
        if (r["h"] or 0) < 1080:
            r["risk"].append(f"{r['h']}p 저화질")
    rows.sort(key=lambda r: (bool(r["risk"]), -r["views"]))
    (ep / "candidates.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [f"{'⚠ ' + ','.join(r['risk']) if r['risk'] else 'OK'} | {r['views']:>10,} | {r['dur']:>4}s | "
             f"{r['w']}x{r['h']} | {r['channel']} | {r['title']} | {r['id']}" for r in rows]
    (ep / "candidates.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    ok = sum(1 for r in rows if not r["risk"])
    print(f"\nCC 확인 {len(rows)}개 (안전해 보이는 것 {ok}개) → {ep / 'candidates.txt'}")


def cmd_get(a):
    raw = Path(a.folder) / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    meta_p = raw / "sources.json"
    meta = json.loads(meta_p.read_text(encoding="utf-8")) if meta_p.exists() else []
    have = {m.get("id") for m in meta}
    sections = dict(s.split("=", 1) for s in (a.section or []))
    by_id = {m.get("id"): m for m in meta}
    for vid in a.ids:
        if vid in have and not a.hq:
            print(f"이미 있음: {vid}")
            continue
        try:
            info = full_info(vid)
        except Blocked:
            print(f"✗ 유튜브가 조회를 막음 (봇 확인): {vid} — 30분~1시간 뒤 다시 실행하세요")
            raise SystemExit(2)
        if not info or "Creative Commons" not in (info.get("license") or ""):
            print(f"✗ CC 라이선스가 아님, 건너뜀: {vid}")
            continue
        old = by_id.get(vid)
        fn = old["file"] if old else f"y{len(meta):02d}.mp4"
        # 최고 화질 (4K 까지). 렌더러가 1080 폭으로 줄이므로 원본이 클수록 선명함
        args = ["-f", "bv*[height<=2160]+ba/b[height<=2160]/b", "-S", "res,fps,vcodec:h264",
                "--merge-output-format", "mp4", "--force-overwrites", "-o", str(raw / fn)]
        if vid in sections:
            args += ["--download-sections", f"*{sections[vid]}", "--force-keyframes-at-cuts"]
        r = ytdlp([*args, "--", f"https://www.youtube.com/watch?v={vid}"], timeout=900)
        if r.returncode != 0 or not (raw / fn).exists():
            print(f"✗ 받기 실패 {vid}: {r.stderr[-300:]}")
            continue
        if old:
            meta.remove(old)
        meta.append({"file": fn, "id": vid, "title": info.get("title"), "artist": info.get("channel"),
                     "page": f"https://www.youtube.com/watch?v={vid}", "license": "CC BY (YouTube)",
                     "license_url": "https://creativecommons.org/licenses/by/3.0/",
                     "section": sections.get(vid)})
        meta_p.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        # 유튜브 정보의 height 는 실제로 받을 수 있는 화질과 다를 때가 있음 → 받은 파일을 직접 확인
        pr = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v", "-show_entries",
                             "stream=width,height", "-of", "csv=p=0", str(raw / fn)],
                            capture_output=True, text=True)
        try:
            w_, h_ = (int(v) for v in pr.stdout.strip().split("\n")[0].split(","))
            h = min(w_, h_)  # 세로 영상은 짧은 변 기준
        except ValueError:
            h = info.get("height") or 0
        print(f"✓ {fn}  {h}p (실제 파일)  {info.get('channel')} | {info.get('title')}"
              + ("  ⚠ 1080p 미만" if h and h < 1080 else ""))


def cmd_credits(a):
    plan_p = Path(a.plan).resolve()
    plan = json.loads(plan_p.read_text(encoding="utf-8"))
    src = {}
    for sp in plan_p.parent.rglob("sources.json"):
        for m in json.loads(sp.read_text(encoding="utf-8")):
            src[m["file"]] = m
    lines = ["영상 출처 (크리에이티브 커먼즈 / 자유 라이선스)",
             "편집: 구간 자르기, 세로 화면 크롭/확대, 자막·순위 그래픽·효과음 추가", ""]
    for it in plan["items"]:
        m = src.get(Path(it["src"]).name)
        if not m:
            lines.append(f"{it['rank']}등 {it.get('label', '')}: (출처 정보 없음: {it['src']})")
            continue
        lic = m["license"] + (f" ({m['license_url']})" if m.get("license_url") else "")
        title = str(m.get("title", "")).removeprefix("File:")
        lines.append(f"{it['rank']}등 {it.get('label', '')}: \"{title}\" by {m.get('artist') or '(작성자 표기 없음)'}"
                     f" / {lic} / {m['page']}")
    lines += ["", "폰트: Jua, Noto Sans KR (SIL Open Font License)"]
    out = plan_p.parent / "credits.txt"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("search")
    s.add_argument("folder")
    s.add_argument("queries", nargs="+")
    s.add_argument("--per", type=int, default=30, help="검색어당 결과 수")
    s.add_argument("--max-dur", type=int, default=600, help="이 길이(초) 넘는 영상 제외")
    g = sub.add_parser("get")
    g.add_argument("folder")
    g.add_argument("ids", nargs="+")
    g.add_argument("--section", nargs="*", help="영상ID=시작-끝 (예: abc123=10-25) 일부만 받기")
    g.add_argument("--hq", action="store_true", help="이미 받은 영상도 최고 화질로 다시 받기")
    c = sub.add_parser("credits")
    c.add_argument("plan")
    a = ap.parse_args()
    {"search": cmd_search, "get": cmd_get, "credits": cmd_credits}[a.cmd](a)


if __name__ == "__main__":
    main()
