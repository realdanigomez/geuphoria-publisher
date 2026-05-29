"""
Publish a carousel as a YouTube Short (video slideshow).

Reads today's carousel entry from publish_schedule.json:
  carousel.yt_carousel_folder  → path inside carousels/ with YT-variant slides
  carousel.yt_caption_path     → path to YouTube caption file

Steps:
  1. Load slides from carousels/<yt_carousel_folder>/
  2. Compile into a 1080x1920 MP4 via ffmpeg (each slide = 4s, padded to 9:16)
  3. Upload to YouTube as a Short via Data API v3
  4. Log result as yt_carousel in published_log.json

Usage:
    python publish_yt_carousel.py
    python publish_yt_carousel.py --date 2026-05-29
    python publish_yt_carousel.py --dry-run

Env:
    IG_USER_ID, IG_ACCESS_TOKEN — not used; kept for parity with other scripts
    GOOGLE_TOKEN_JSON           — base64-encoded .google_token.json (decoded by workflow)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("yt_carousel")

ROOT = Path(__file__).resolve().parent
LOG_PATH = ROOT / "published_log.json"
SCHED_PATH = ROOT / "publish_schedule.json"
TOKEN_PATH = ROOT / ".google_token.json"
AST = timezone(timedelta(hours=-4))

LOG_KEY = "yt_carousel"

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube",
]


# ── Log helpers ─────────────────────────────────────────────────

def aruba_today() -> str:
    return datetime.now(AST).date().isoformat()


def load_log() -> dict:
    if LOG_PATH.exists():
        return json.loads(LOG_PATH.read_text(encoding="utf-8"))
    return {}


def mark_published(today: str, key: str, value: str) -> None:
    d = load_log()
    d.setdefault(today, {})[key] = value
    LOG_PATH.write_text(json.dumps(d, indent=2), encoding="utf-8")
    log.info(f"Logged: {today}.{key} = {value}")


def is_already_published(today: str, key: str) -> bool:
    return key in load_log().get(today, {})


# ── Time-window guard ────────────────────────────────────────────

def is_before_scheduled_time(slot: dict) -> bool:
    slot_time_str = slot.get('slot_time_ast', '').strip()
    if not slot_time_str:
        return False
    try:
        t = datetime.strptime(slot_time_str, '%I:%M %p')
        now = datetime.now(AST)
        return (now.hour * 60 + now.minute) < (t.hour * 60 + t.minute)
    except Exception:
        return False


# ── Compile slides to MP4 ────────────────────────────────────────

def compile_slides_to_video(slides_dir: Path, out_mp4: Path) -> None:
    """Use ffmpeg to compile PNGs into a 1080x1920 Short (each slide 4s)."""
    slides = sorted(p for p in slides_dir.iterdir()
                    if p.name.startswith('slide-') and p.name.endswith('.png'))
    if not slides:
        raise RuntimeError(f"No slide-*.png files in {slides_dir}")

    log.info(f"Compiling {len(slides)} slides into video: {out_mp4}")

    # Build ffmpeg concat filelist
    filelist = out_mp4.parent / "filelist.txt"
    with open(filelist, 'w') as f:
        for s in slides:
            f.write(f"file '{s.as_posix()}'\n")
            f.write("duration 4\n")
        # ffmpeg requires last entry repeated without duration for correct last frame
        f.write(f"file '{slides[-1].as_posix()}'\n")

    cmd = [
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0", "-i", str(filelist),
        "-vf", "scale=1080:1080:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2:black",
        "-c:v", "libx264", "-crf", "18", "-preset", "fast",
        "-pix_fmt", "yuv420p", "-r", "30",
        "-an",
        str(out_mp4),
    ]
    log.info(f"ffmpeg: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        log.error(f"ffmpeg stderr: {result.stderr[-2000:]}")
        raise RuntimeError(f"ffmpeg failed (rc={result.returncode})")
    size_mb = out_mp4.stat().st_size / 1024 / 1024
    log.info(f"Video compiled: {size_mb:.1f} MB, {len(slides) * 4}s")
    filelist.unlink(missing_ok=True)


# ── YouTube upload ────────────────────────────────────────────────

def build_youtube():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    if not TOKEN_PATH.exists():
        raise RuntimeError(f"No OAuth token at {TOKEN_PATH}")
    creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            log.info("OAuth token expired; refreshing...")
            creds.refresh(Request())
            TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
        else:
            raise RuntimeError("OAuth token invalid and no refresh_token. Re-auth needed.")
    return build("youtube", "v3", credentials=creds)


def upload_to_youtube(video_path: Path, title: str, description: str) -> str:
    from googleapiclient.http import MediaFileUpload
    from googleapiclient.errors import HttpError

    if "#shorts" not in description.lower():
        description = description + "\n\n#Shorts"

    yt = build_youtube()
    body = {
        "snippet": {
            "title": title[:100],
            "description": description[:5000],
            "tags": ["online fitness coach", "online fitness coaches",
                     "fitness coach business", "real.danigomez", "Shorts"],
            "categoryId": "27",
        },
        "status": {
            "privacyStatus": "public",
            "selfDeclaredMadeForKids": False,
        },
    }
    media = MediaFileUpload(str(video_path), mimetype="video/mp4",
                            resumable=True, chunksize=8 * 1024 * 1024)
    log.info("Uploading to YouTube (resumable)...")
    request = yt.videos().insert(part="snippet,status", body=body, media_body=media)
    response = None
    last_pct = 0
    while response is None:
        try:
            status, response = request.next_chunk()
            if status:
                pct = int(status.progress() * 100)
                if pct >= last_pct + 10:
                    log.info(f"  {pct}% uploaded")
                    last_pct = pct
        except HttpError as e:
            log.error(f"Upload error: {e}")
            raise
    video_id = response["id"]
    log.info(f"YT Short published. ID: {video_id}")
    log.info(f"URL: https://www.youtube.com/shorts/{video_id}")
    return video_id


# ── Main ─────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    today = args.date or aruba_today()
    log.info(f"=== YT Carousel publisher: today={today} dry_run={args.dry_run} ===")

    # Dedup
    if is_already_published(today, LOG_KEY):
        existing = load_log()[today][LOG_KEY]
        log.info(f"Already published {LOG_KEY} for {today} ({existing}). Skipping.")
        return 0

    # Load schedule
    sched = json.loads(SCHED_PATH.read_text(encoding="utf-8"))
    if today not in sched or "carousel" not in sched[today]:
        log.info(f"No carousel scheduled for {today}. Exiting 0.")
        return 0

    slot = sched[today]["carousel"]
    yt_folder = slot.get("yt_carousel_folder")
    yt_cap_path = slot.get("yt_caption_path")

    if not yt_folder:
        log.info(f"No yt_carousel_folder in schedule for {today}. Skipping.")
        return 0
    if not yt_cap_path:
        log.info(f"No yt_caption_path in schedule for {today}. Skipping.")
        return 0

    # Time-window guard
    if not args.dry_run and is_before_scheduled_time(slot):
        log.info(f"Skipping — current AST time is before scheduled time "
                 f"({slot.get('slot_time_ast')}). Safety net will re-trigger.")
        return 0

    slides_dir = ROOT / "carousels" / yt_folder
    if not slides_dir.exists():
        log.error(f"YT slides folder not found: {slides_dir}")
        return 1

    cap_file = ROOT / yt_cap_path
    if not cap_file.exists():
        log.error(f"YT caption file not found: {cap_file}")
        return 1
    caption = cap_file.read_text(encoding="utf-8").strip()

    name = slot.get("name", yt_folder)
    title_line = next((ln.strip() for ln in caption.splitlines() if ln.strip()), name)
    title = title_line[:100]

    log.info(f"Carousel: {name} | Slides: {slides_dir} | Caption: {len(caption)} chars")
    log.info(f"Title: {title}")

    if args.dry_run:
        slides = sorted(p for p in slides_dir.iterdir() if p.name.endswith('.png'))
        log.info(f"[dry-run] Would compile {len(slides)} slides and upload to YouTube.")
        for s in slides:
            log.info(f"  {s.name}")
        log.info("Dry-run OK.")
        return 0

    # Compile and upload
    with tempfile.TemporaryDirectory() as tmpdir:
        out_mp4 = Path(tmpdir) / f"yt-carousel-{name}.mp4"
        try:
            compile_slides_to_video(slides_dir, out_mp4)
            video_id = upload_to_youtube(out_mp4, title, caption)
            mark_published(today, LOG_KEY, video_id)

            summary_file = os.environ.get("GITHUB_STEP_SUMMARY")
            if summary_file:
                with open(summary_file, "a", encoding="utf-8") as f:
                    f.write("## YT Carousel published\n")
                    f.write(f"- **Name**: {name}\n")
                    f.write(f"- **Video ID**: `{video_id}`\n")
                    f.write(f"- **URL**: https://www.youtube.com/shorts/{video_id}\n")
                    f.write(f"- **Date (AST)**: {today}\n")
                    f.write(f"- **Slides**: {yt_folder}\n")
        except Exception as e:
            log.error(f"PUBLISH FAILED: {e}")
            import traceback
            log.error(traceback.format_exc())
            return 1

    log.info("=== Done ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
