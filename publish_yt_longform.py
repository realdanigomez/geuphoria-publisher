"""
Standalone YouTube longform publisher for the cloud.
Runs in GitHub Actions with no PC + no Railway.

Reads OAuth credentials from .google_token.json (decoded from
the GOOGLE_TOKEN_JSON GitHub secret in the workflow) and the
client_id/client_secret from GOOGLE_CLIENT_ID + GOOGLE_CLIENT_SECRET env.

Usage:
    python publish_yt_longform.py burnout
    python publish_yt_longform.py doc-ep3
    python publish_yt_longform.py burnout --dry-run

Inputs (downloaded into the working dir by `gh release download` before run):
    {which}.mp4
    {which}-thumb.jpg
    {which}-title.txt
    {which}-description.txt
    {which}-chapters.md          (optional — already embedded in description.txt)

Logs result to published_log.json under date key + slot key:
    "2026-05-04": { "yt_burnout_longform": "<video_id>" }
    "2026-05-04": { "yt_doc_ep3":         "<video_id>" }
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("yt_longform")

ROOT = Path(__file__).resolve().parent
LOG_PATH = ROOT / "published_log.json"
TOKEN_PATH = ROOT / ".google_token.json"
AST = timezone(timedelta(hours=-4))

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube",
]

# ── Configuration per slot ────────────────────────────────────────
SLOTS = {
    "burnout": {
        "log_key": "yt_burnout_longform",
        "video": "burnout-longform.mp4",
        "thumb": "burnout-longform-thumb.jpg",
        "title": "burnout-longform-title.txt",
        "description": "burnout-longform-description.txt",
        "category_id": "27",   # Education
        "tags": [
            "online fitness coach",
            "online fitness coaches",
            "fitness business",
            "fitness coach burnout",
            "client filter",
            "online coaching",
        ],
        "privacy": "public",
        "made_for_kids": False,
    },
    "doc-ep3": {
        "log_key": "yt_doc_ep3",
        "video": "doc-ep3.mp4",
        "thumb": "doc-ep3-thumb.jpg",
        "title": "doc-ep3-title.txt",
        "description": "doc-ep3-description.txt",
        "category_id": "22",   # People & Blogs
        "tags": [
            "online fitness coach",
            "online fitness coaches",
            "documentary",
            "building in public",
            "real.danigomez",
            "fitness coach business",
        ],
        "privacy": "public",
        "made_for_kids": False,
    },
}


# ── Log + dedup ──────────────────────────────────────────────────
def aruba_today() -> str:
    return datetime.now(AST).date().isoformat()


def load_log() -> dict:
    if LOG_PATH.exists():
        return json.loads(LOG_PATH.read_text(encoding="utf-8"))
    return {}


def save_log(d: dict) -> None:
    LOG_PATH.write_text(json.dumps(d, indent=2), encoding="utf-8")


def is_already_published(today: str, key: str) -> bool:
    d = load_log()
    return today in d and key in d[today]


def mark_published(today: str, key: str, value: str) -> None:
    d = load_log()
    if today not in d:
        d[today] = {}
    d[today][key] = value
    save_log(d)
    log.info(f"Logged: {today}.{key} = {value}")


# ── OAuth ────────────────────────────────────────────────────────
def build_youtube():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    if not TOKEN_PATH.exists():
        raise RuntimeError(f"No OAuth token at {TOKEN_PATH}. Did you decode GOOGLE_TOKEN_JSON?")

    creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            log.info("OAuth token expired; refreshing...")
            creds.refresh(Request())
            TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
        else:
            raise RuntimeError("OAuth token invalid and no refresh_token. Re-auth needed.")
    return build("youtube", "v3", credentials=creds)


# ── Publish ─────────────────────────────────────────────────────
def publish_one(which: str, dry_run: bool = False) -> dict:
    if which not in SLOTS:
        raise ValueError(f"Unknown slot: {which}. Choose from {list(SLOTS)}")
    slot = SLOTS[which]
    today = aruba_today()
    log_key = slot["log_key"]

    if is_already_published(today, log_key):
        existing = load_log()[today][log_key]
        log.info(f"Already published today: {today}.{log_key} = {existing}. Skipping.")
        return {"video_id": existing, "skipped": True}

    video_path = ROOT / slot["video"]
    thumb_path = ROOT / slot["thumb"]
    title_path = ROOT / slot["title"]
    desc_path = ROOT / slot["description"]

    for p in (video_path, thumb_path, title_path, desc_path):
        if not p.exists():
            raise FileNotFoundError(f"Missing asset: {p}")

    title = title_path.read_text(encoding="utf-8").strip()
    description = desc_path.read_text(encoding="utf-8").strip()

    # YouTube enforces title ≤ 100 chars + description ≤ 5000 chars
    title = title[:100]
    description = description[:5000]

    log.info(f"=== YT longform publisher: {which} ===")
    log.info(f"Video      : {video_path.name} ({video_path.stat().st_size / 1024 / 1024:.1f} MB)")
    log.info(f"Thumbnail  : {thumb_path.name}")
    log.info(f"Title      : {title}")
    log.info(f"Privacy    : {slot['privacy']}")
    log.info(f"Category   : {slot['category_id']}")
    log.info(f"Tags       : {len(slot['tags'])} tags")
    log.info(f"Description: {len(description)} chars / {len(description.splitlines())} lines")
    log.info(f"Dry run    : {dry_run}")

    if dry_run:
        log.info("[dry-run] Skipping API publish + thumbnail set.")
        return {
            "dry_run": True,
            "video": str(video_path),
            "thumb": str(thumb_path),
            "title": title,
        }

    from googleapiclient.http import MediaFileUpload
    from googleapiclient.errors import HttpError

    yt = build_youtube()

    body = {
        "snippet": {
            "title": title,
            "description": description,
            "tags": slot["tags"][:500],
            "categoryId": slot["category_id"],
        },
        "status": {
            "privacyStatus": slot["privacy"],
            "selfDeclaredMadeForKids": slot["made_for_kids"],
        },
    }

    media = MediaFileUpload(
        str(video_path),
        mimetype="video/mp4",
        resumable=True,
        chunksize=8 * 1024 * 1024,
    )

    log.info("Uploading video (resumable)...")
    request = yt.videos().insert(part="snippet,status", body=body, media_body=media)
    response = None
    last_progress = 0
    while response is None:
        try:
            status, response = request.next_chunk()
            if status:
                pct = int(status.progress() * 100)
                if pct >= last_progress + 5:
                    log.info(f"  {pct}% uploaded")
                    last_progress = pct
        except HttpError as e:
            log.error(f"Upload error: {e}")
            raise

    video_id = response["id"]
    public_url = f"https://www.youtube.com/watch?v={video_id}"
    log.info(f"Uploaded. Video ID: {video_id}")
    log.info(f"URL: {public_url}")

    log.info("Setting thumbnail...")
    yt.thumbnails().set(
        videoId=video_id,
        media_body=MediaFileUpload(str(thumb_path), mimetype="image/jpeg", resumable=False),
    ).execute()
    log.info("Thumbnail set.")

    mark_published(today, log_key, video_id)

    summary_file = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_file:
        with open(summary_file, "a", encoding="utf-8") as f:
            f.write("## YT longform published\n")
            f.write(f"- **Slot**: {which}\n")
            f.write(f"- **Video ID**: `{video_id}`\n")
            f.write(f"- **URL**: {public_url}\n")
            f.write(f"- **Title**: {title}\n")
            f.write(f"- **Date (AST)**: {today}\n")

    return {"video_id": video_id, "url": public_url, "title": title}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("which", choices=list(SLOTS), help="Which longform to publish")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    try:
        result = publish_one(args.which, dry_run=args.dry_run)
    except Exception as e:
        log.error(f"PUBLISH FAILED: {e}")
        import traceback
        log.error(traceback.format_exc())
        return 1

    log.info(f"=== Done. {result} ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
