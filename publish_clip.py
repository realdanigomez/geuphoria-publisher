"""
Standalone IG + YT clip publisher for the cloud.
Publishes doc-clip / longform-clip slots to Instagram (Reels) and/or YouTube (Shorts).

Reads the IG cdn_url + caption from publish_schedule.json[today][<slot>] and the YT
asset + caption from the same entry. Reuses cloud_publish.publish_reel() for IG and
the YT longform publisher pattern for YT Shorts.

Usage:
    python publish_clip.py --slot doc_clip --platform both
    python publish_clip.py --slot longform_clip --platform ig
    python publish_clip.py --slot doc_clip --platform yt
    python publish_clip.py --slot doc_clip --platform yt --gate yt_doc_ep3
    python publish_clip.py --slot doc_clip --platform yt --dry-run

Schedule entry format (publish_schedule.json):
    {
      "2026-05-04": {
        "doc_clip": {
          "name": "figure-it-out-thick-skin",
          "cdn_url":    "https://raw.githubusercontent.com/.../figure-it-out-thick-skin-instagram.mp4",
          "yt_cdn_url": "https://raw.githubusercontent.com/.../figure-it-out-thick-skin-youtube.mp4",
          "caption_path":    "captions/figure-it-out-thick-skin/caption.txt",
          "yt_caption_path": "captions/figure-it-out-thick-skin/caption-youtube.txt"
        }
      }
    }

Logs:
    - IG: logs to <slot> (e.g. "doc_clip")
    - YT: logs to "yt_<slot>" (e.g. "yt_doc_clip")
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

import requests

import slot_lock

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("clip_publisher")

ROOT = Path(__file__).resolve().parent
LOG_PATH = ROOT / "published_log.json"
SCHED_PATH = ROOT / "publish_schedule.json"
TOKEN_PATH = ROOT / ".google_token.json"
AST = timezone(timedelta(hours=-4))

API_BASE = "https://graph.facebook.com/v21.0"


# ── Log + dedup ─────────────────────────────────────────────────
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


def gate_satisfied(today: str, gate_key: str | None) -> bool:
    """Check if a gate dependency (e.g. yt_doc_ep3) is satisfied."""
    if not gate_key:
        return True
    d = load_log()
    if today not in d or gate_key not in d[today]:
        log.warning(f"Gate not yet satisfied: {today}.{gate_key} not in log")
        return False
    log.info(f"Gate satisfied: {today}.{gate_key} = {d[today][gate_key]}")
    return True


# ── Schedule ────────────────────────────────────────────────────
def load_schedule_slot(today: str, slot: str) -> dict:
    if not SCHED_PATH.exists():
        raise FileNotFoundError(f"Missing {SCHED_PATH}")
    sched = json.loads(SCHED_PATH.read_text(encoding="utf-8"))
    if today not in sched:
        raise RuntimeError(f"No schedule entry for {today}")
    if slot not in sched[today]:
        raise RuntimeError(f"No {slot} for {today}")
    return sched[today][slot]


def read_caption_file(rel_path: str) -> str:
    p = ROOT / rel_path
    if not p.exists():
        raise FileNotFoundError(f"Caption file not found: {p}")
    return p.read_text(encoding="utf-8").strip()


# ── IG publish ───────────────────────────────────────────────────
def publish_ig_clip(today: str, slot: str, slot_data: dict, dry_run: bool = False) -> str | None:
    # Claim before posting — see slot_lock.py. The old read-check-then-post
    # guard here had the same minutes-wide gap that duplicated reels twice.
    if not dry_run:
        won, reason = slot_lock.claim_slot(today, slot)
        if not won:
            log.info(f"IG not publishing {slot}: {reason}.")
            return None
        log.info(f"Claimed IG slot {slot} for {today}.")

    cdn_url = slot_data.get("cdn_url")
    if not cdn_url:
        raise RuntimeError(f"No cdn_url in schedule[{today}][{slot}]")
    caption_path = slot_data.get("caption_path") or slot_data.get("caption_file")
    if caption_path:
        caption = read_caption_file(caption_path)
    elif "caption" in slot_data:
        caption = slot_data["caption"]
    else:
        raise RuntimeError(f"No caption / caption_path in schedule[{today}][{slot}]")

    log.info(f"=== IG clip publish: {slot} (today={today}) ===")
    log.info(f"CDN URL    : {cdn_url}")
    log.info(f"Caption    : {len(caption)} chars; {caption[:80]}...")
    log.info(f"Dry run    : {dry_run}")

    if dry_run:
        return None

    # Inline import to avoid pulling Google libs when only doing IG
    from cloud_publish import publish_reel  # type: ignore

    try:
        media_id = publish_reel(cdn_url, caption)
    except Exception:
        slot_lock.release_claim(today, slot)
        log.info(f"Released IG claim on {slot} after failure.")
        raise
    if not slot_lock.complete_slot(today, slot, media_id):
        log.error(f"POSTED but FAILED TO RECORD {slot}={media_id} — record it manually.")
        raise RuntimeError(f"could not record {slot}={media_id}")
    log.info(f"Logged: {today}.{slot} = {media_id}")
    return media_id


# ── YT publish ───────────────────────────────────────────────────
SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube",
]


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


def download_yt_video(yt_cdn_url: str, dest: Path) -> None:
    log.info(f"Downloading YT video from CDN: {yt_cdn_url}")
    r = requests.get(yt_cdn_url, stream=True, timeout=300)
    r.raise_for_status()
    with open(dest, "wb") as f:
        for chunk in r.iter_content(8192):
            if chunk:
                f.write(chunk)
    size_mb = dest.stat().st_size / 1024 / 1024
    log.info(f"Downloaded {size_mb:.1f} MB to {dest}")


# ── YT Short hardening (2026-09-25) ─────────────────────────────
# The 3 PM Short of 2026-09-25: YouTube answered a finished upload with 409 "Requested entity already exists", the
# run failed, and the video it left (hsZ7Ud02zK4) listed as public but stayed stuck in processing (duration P0D) until
# Dani caught it an hour later. So a Short now counts as posted only once YouTube reports it PROCESSED with a real
# duration. A 409 is resolved by finding the upload by its title. A stuck upload is retired (private + renamed) and
# re-uploaded ONCE. Worst case (IG 5 min + 2 x (upload + PROCESS_WAIT_S)) stays inside the workflows' 30-min timeout.
PROCESS_WAIT_S = 480
PROCESS_POLL_S = 15
RETIRED_TITLE = "INCOMPLETE UPLOAD, DELETE ME"


def _find_upload_by_title(yt, title: str, since: datetime) -> str | None:
    """The video on our channel with exactly this title, created since `since` (UTC), or None."""
    ch = yt.channels().list(part="contentDetails", mine=True).execute()["items"][0]
    uploads = ch["contentDetails"]["relatedPlaylists"]["uploads"]
    items = yt.playlistItems().list(part="snippet", playlistId=uploads, maxResults=15).execute().get("items", [])
    for it in items:
        s = it["snippet"]
        if s.get("title") != title:
            continue
        created = datetime.fromisoformat(s["publishedAt"].replace("Z", "+00:00"))
        if created >= since - timedelta(seconds=60):
            return s["resourceId"]["videoId"]
    return None


def _upload_short(yt, body: dict, local_video: Path, title: str) -> str:
    """Resumable videos.insert -> the video id. A 409 'already exists' after the upload is looked up by title."""
    from googleapiclient.http import MediaFileUpload
    from googleapiclient.errors import HttpError

    since = datetime.now(timezone.utc)
    media = MediaFileUpload(str(local_video), mimetype="video/mp4", resumable=True, chunksize=8 * 1024 * 1024)
    request = yt.videos().insert(part="snippet,status", body=body, media_body=media)
    response = None
    last_progress = 0
    while response is None:
        try:
            status, response = request.next_chunk(num_retries=5)
            if status:
                pct = int(status.progress() * 100)
                if pct >= last_progress + 10:
                    log.info(f"  {pct}% uploaded")
                    last_progress = pct
        except HttpError as e:
            if getattr(getattr(e, "resp", None), "status", None) == 409:
                log.warning(f"YT answered 409 after the upload ({e}); looking the video up by its title...")
                for _ in range(6):
                    time.sleep(10)
                    found = _find_upload_by_title(yt, title, since)
                    if found:
                        log.info(f"Found the upload on the channel: {found}")
                        return found
            log.error(f"Upload error: {e}")
            raise
    return response["id"]


def _wait_processed(yt, video_id: str, wait_s: float | None = None) -> tuple[bool, dict]:
    """Poll until YouTube has PROCESSED the video. -> (ok, info); ok = uploadStatus processed + a real duration."""
    deadline = time.time() + (PROCESS_WAIT_S if wait_s is None else wait_s)
    info: dict = {}
    while True:
        items = yt.videos().list(part="status,processingDetails,contentDetails", id=video_id).execute().get("items", [])
        if items:
            v = items[0]
            up = (v.get("status") or {}).get("uploadStatus")
            ps = (v.get("processingDetails") or {}).get("processingStatus")
            dur = (v.get("contentDetails") or {}).get("duration")
            info = {"uploadStatus": up, "processingStatus": ps, "duration": dur}
            if up == "processed" and dur not in (None, "P0D"):
                return True, info
            if up in ("failed", "rejected", "deleted") or ps in ("failed", "terminated"):
                return False, info
        if time.time() > deadline:
            return False, dict(info, timeout_s=PROCESS_WAIT_S if wait_s is None else wait_s)
        time.sleep(PROCESS_POLL_S)


def _retire_upload(yt, video_id: str) -> None:
    """A broken upload: private, then titled RETIRED_TITLE. Two separate calls, because a stuck video took the privacy
    change before the title on 2026-09-25. Deleting it stays Dani's, in Studio."""
    try:
        snip = yt.videos().list(part="snippet", id=video_id).execute()["items"][0]["snippet"]
        yt.videos().update(part="status", body={"id": video_id, "status": {
            "privacyStatus": "private", "selfDeclaredMadeForKids": False}}).execute()
        yt.videos().update(part="snippet", body={"id": video_id, "snippet": {
            "title": RETIRED_TITLE, "description": snip.get("description", ""),
            "categoryId": snip.get("categoryId", "27")}}).execute()
        log.warning(f"Retired the broken upload {video_id}: private, titled '{RETIRED_TITLE}'.")
    except Exception as e:
        log.error(f"Could not retire the broken upload {video_id}: {e}")


def publish_yt_short(today: str, slot: str, slot_data: dict, dry_run: bool = False) -> str | None:
    yt_log_key = f"yt_{slot}"
    if not dry_run:
        won, reason = slot_lock.claim_slot(today, yt_log_key)
        if not won:
            log.info(f"YT not publishing {yt_log_key}: {reason}.")
            return None
        log.info(f"Claimed YT slot {yt_log_key} for {today}.")

    yt_cdn_url = slot_data.get("yt_cdn_url")
    if not yt_cdn_url:
        raise RuntimeError(f"No yt_cdn_url in schedule[{today}][{slot}]")
    yt_caption_path = slot_data.get("yt_caption_path")
    if not yt_caption_path:
        raise RuntimeError(f"No yt_caption_path in schedule[{today}][{slot}]")
    yt_caption = read_caption_file(yt_caption_path)
    name = slot_data.get("name", slot)

    # Prefer a dedicated yt_title_path (deliberately-written title, distinct from the
    # caption) when present. Fall back to the first non-empty caption line otherwise.
    yt_title_path = slot_data.get("yt_title_path")
    if yt_title_path:
        title = read_caption_file(yt_title_path)
    else:
        title = next((ln.strip() for ln in yt_caption.splitlines() if ln.strip()), name)
    title = title[:100]
    if "#shorts" not in yt_caption.lower():
        yt_caption = yt_caption + "\n\n#Shorts"
    description = yt_caption[:5000]

    log.info(f"=== YT Short publish: {slot} ({name}) ===")
    log.info(f"YT CDN URL : {yt_cdn_url}")
    log.info(f"Title      : {title}")
    log.info(f"Description: {len(description)} chars / {len(description.splitlines())} lines")
    log.info(f"Dry run    : {dry_run}")

    if dry_run:
        return None

    # Download MP4 to local temp
    local_video = ROOT / f"_tmp_{slot}_{name}.mp4"
    try:
        download_yt_video(yt_cdn_url, local_video)

        yt = build_youtube()
        body = {
            "snippet": {
                "title": title,
                "description": description,
                "tags": [
                    "online fitness coach",
                    "online fitness coaches",
                    "fitness coach business",
                    "real.danigomez",
                    "Shorts",
                ],
                "categoryId": "27",  # Education
            },
            "status": {
                "privacyStatus": "public",
                "selfDeclaredMadeForKids": False,
            },
        }
        # Upload, then WAIT until YouTube has processed it; a stuck upload is retired and re-uploaded ONCE
        video_id, proc = None, {}
        for attempt in (1, 2):
            log.info(f"Uploading YT Short (resumable), attempt {attempt}...")
            vid = _upload_short(yt, body, local_video, title)
            log.info(f"Uploaded {vid}; waiting for YouTube to process it (up to {PROCESS_WAIT_S}s)...")
            ok, proc = _wait_processed(yt, vid)
            log.info(f"Processing: {proc}")
            if ok:
                video_id = vid
                break
            log.error(f"YT Short {vid} did not process ({proc}).")
            _retire_upload(yt, vid)
        if video_id is None:
            raise RuntimeError(f"YT Short did not process after 2 uploads (last: {proc})")
        public_url = f"https://www.youtube.com/shorts/{video_id}"
        log.info(f"YT Short published. Video ID: {video_id}")
        log.info(f"URL: {public_url}")
        if not slot_lock.complete_slot(today, yt_log_key, video_id):
            log.error(f"POSTED but FAILED TO RECORD {yt_log_key}={video_id} — record it manually.")
        else:
            log.info(f"Logged: {today}.{yt_log_key} = {video_id}")

        summary_file = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary_file:
            with open(summary_file, "a", encoding="utf-8") as f:
                f.write("## YT Short published\n")
                f.write(f"- **Slot**: {slot}\n")
                f.write(f"- **Name**: {name}\n")
                f.write(f"- **Video ID**: `{video_id}`\n")
                f.write(f"- **URL**: {public_url}\n")
                f.write(f"- **Title**: {title}\n")
                f.write(f"- **Processing**: {proc}\n")
                f.write(f"- **Date (AST)**: {today}\n")

        return video_id
    finally:
        if local_video.exists():
            try:
                local_video.unlink()
            except Exception:
                pass


# ── Time-window guard ────────────────────────────────────────────
def is_before_scheduled_time(slot: dict) -> bool:
    """True if current AST time is before the slot's scheduled time.
    Prevents a GH cron delayed from the previous day from posting
    the next day's content before its scheduled hour."""
    slot_time_str = slot.get('slot_time_ast', '').strip()
    if not slot_time_str:
        return False
    try:
        t = datetime.strptime(slot_time_str, '%I:%M %p')
        now = datetime.now(AST)
        return (now.hour * 60 + now.minute) < (t.hour * 60 + t.minute)
    except Exception:
        return False


# ── Main ────────────────────────────────────────────────────────
def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--slot", required=True,
                        choices=["doc_clip", "longform_clip", "longform_clip_bonus", "reel", "reel_2"])
    parser.add_argument("--platform", default="both", choices=["ig", "yt", "both"])
    parser.add_argument("--gate", default=None,
                        help="Dependency log key (e.g. yt_doc_ep3) — if missing in published_log "
                             "for today, exit 0 with a notice.")
    parser.add_argument("--date", default=None, help="Override AST date (YYYY-MM-DD)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    today = args.date or aruba_today()
    log.info(f"=== Clip publisher: slot={args.slot} platform={args.platform} today={today} ===")

    if not gate_satisfied(today, args.gate):
        log.info(f"Gate {args.gate} not satisfied; exiting 0 (skipping clip publish).")
        return 0

    try:
        slot_data = load_schedule_slot(today, args.slot)
    except Exception as e:
        # Graceful no-op when this slot simply isn't scheduled today (e.g. bonus
        # slots only exist on some days). Daily cron then exits clean, no error.
        if f"No {args.slot} for" in str(e):
            log.info(f"No {args.slot} scheduled for {today}; nothing to publish. Exiting 0.")
            return 0
        log.error(f"Schedule lookup failed: {e}")
        return 1

    # Time-window guard — skip if current AST time is before scheduled time
    if not args.dry_run and is_before_scheduled_time(slot_data):
        log.info(f'Skipping — current AST time is before scheduled time '
                 f'({slot_data.get("slot_time_ast")}). Likely a delayed previous-day '
                 f'cron. Safety net will re-trigger at the correct hour.')
        return 0

    failures = []

    if args.platform in ("ig", "both"):
        try:
            publish_ig_clip(today, args.slot, slot_data, dry_run=args.dry_run)
        except Exception as e:
            log.error(f"IG clip publish FAILED: {e}")
            import traceback
            log.error(traceback.format_exc())
            failures.append("ig")

    if args.platform in ("yt", "both"):
        try:
            publish_yt_short(today, args.slot, slot_data, dry_run=args.dry_run)
        except Exception as e:
            log.error(f"YT Short publish FAILED: {e}")
            import traceback
            log.error(traceback.format_exc())
            failures.append("yt")

    if failures:
        log.error(f"Failed platforms: {failures}")
        return 1

    log.info("=== Done ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
