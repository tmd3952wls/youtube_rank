"""완성 영상을 네이버 메일로 보내기.

  python send_mail.py <영상.mp4> [더 보낼 파일 ...] [--to 주소] [--subject 제목]

준비: 네이버 메일 환경설정 → POP3/IMAP 설정 → SMTP 사용함,
      프로젝트 폴더의 secrets.env 에 NAVER_ID / NAVER_PW 적기.
네이버 SMTP 첨부는 10MB 까지라서, 큰 영상은 자동으로 압축본(*_mail.mp4)을 만들어 보냄.
"""
import argparse
import shutil
import smtplib
import subprocess
import sys
import tempfile
from email.message import EmailMessage
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parent.parent
LIMIT = 9.5 * 1024 * 1024
CHANNEL = "[1분랭킹]"  # 유튜브 채널 이름


def load_env():
    env = {}
    for p in (ROOT / ".env", ROOT / "secrets.env"):
        if p.exists():
            for line in p.read_text(encoding="utf-8-sig", errors="replace").splitlines():
                if "=" in line and not line.lstrip().startswith("#"):
                    k, v = line.split("=", 1)
                    val = v.strip().strip('"').strip("'")
                    if val:
                        env[k.strip()] = val
    return env


def duration(f):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(f)],
                       capture_output=True, text=True)
    return float(r.stdout.strip())


def shrink(f):
    """10MB 안쪽으로 2-pass 재인코딩."""
    out = f.with_name(f.stem + "_mail.mp4")
    if out.exists() and 0 < out.stat().st_size < LIMIT and out.stat().st_mtime > f.stat().st_mtime:
        return out
    kbps = int(LIMIT * 8 / 1000 / duration(f) * 0.92) - 128
    tmp_dir = Path(tempfile.mkdtemp(prefix="mail2p_"))
    try:
        log = str(tmp_dir / "pass")
        base = ["ffmpeg", "-y", "-v", "error", "-i", str(f), "-c:v", "libx264", "-preset", "slow",
                "-b:v", f"{kbps}k", "-passlogfile", log]
        subprocess.run([*base, "-pass", "1", "-an", "-f", "mp4", "NUL" if sys.platform == "win32" else "/dev/null"],
                       check=True)
        for tp in tmp_dir.glob("*.temp"):
            dst = tp.with_suffix("")
            if not dst.exists():
                dst.write_bytes(tp.read_bytes())
        subprocess.run([*base, "-pass", "2", "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(out)],
                       check=True)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    print(f"  압축: {f.name} {f.stat().st_size / 1e6:.1f}MB → {out.name} {out.stat().st_size / 1e6:.1f}MB")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+")
    ap.add_argument("--to")
    ap.add_argument("--subject")
    a = ap.parse_args()
    env = load_env()
    uid, pw = env.get("NAVER_ID", ""), env.get("NAVER_PW", "")
    if not uid or not pw:
        raise SystemExit(f"✗ {ROOT / 'secrets.env'} 에 NAVER_ID 와 NAVER_PW 를 적어주세요")
    uid = uid.removesuffix("@naver.com")
    sender = f"{uid}@naver.com"
    to = a.to or env.get("MAIL_TO") or sender

    files = [Path(f).resolve() for f in a.files]
    files = [shrink(f) if f.suffix.lower() in (".mp4", ".mov") and f.stat().st_size > LIMIT else f for f in files]
    total = sum(f.stat().st_size for f in files)
    if total > LIMIT:
        raise SystemExit(f"✗ 첨부 합계 {total / 1e6:.1f}MB — 네이버는 10MB 까지라 파일을 나눠서 보내세요")

    msg = EmailMessage()
    msg["From"], msg["To"] = sender, to
    # 채널 이름 [1분랭킹] 을 항상 제목 앞에 붙임
    subj = (a.subject or f"{files[0].parent.name} 완성 영상").removeprefix(CHANNEL).strip()
    msg["Subject"] = f"{CHANNEL} {subj}"
    body = "완성 영상 첨부합니다.\n\n" + "\n".join(f"- {f.name} ({f.stat().st_size / 1e6:.1f}MB)" for f in files)
    # 같은 폴더의 upload.txt (유튜브 제목·설명·태그) 를 본문에 넣음
    up = files[0].parent / "upload.txt"
    if up.exists():
        body += "\n\n━━━━ 유튜브 업로드용 ━━━━\n\n" + up.read_text(encoding="utf-8")
    msg.set_content(body)
    for f in files:
        sub = "mp4" if f.suffix.lower() == ".mp4" else "octet-stream"
        maintype = "video" if sub == "mp4" else "application"
        msg.add_attachment(f.read_bytes(), maintype=maintype, subtype=sub, filename=f.name)

    try:
        with smtplib.SMTP_SSL("smtp.naver.com", 465, timeout=120) as s:
            s.login(uid, pw)
            s.send_message(msg)
    except smtplib.SMTPAuthenticationError:
        raise SystemExit("✗ 로그인 실패 — 네이버 메일 설정에서 'SMTP 사용함'을 켰는지, "
                         "2단계 인증이면 애플리케이션 비밀번호를 썼는지 확인하세요")
    print(f"✓ 보냄 → {to}  ({', '.join(f.name for f in files)})")


if __name__ == "__main__":
    main()
