"""Publish an original multi-frame Instagram Story sequence (7PM AST slot).

Unlike story-reshare.py (which reshares a feed post), this posts Dani's ORIGINAL
5-frame educational story sequences. Each frame is committed to the repo under
stories/<dir>/frame-N.png and served via raw.githubusercontent.com. Each frame is
posted as a separate IG Story card via the Graph API (media_type=STORIES), in order.

Reads publish_schedule.json[<today AST>].story:
    "story": {
        "name": "story-1-order-problem",
        "folder": "stories/story-1-order-problem",     # repo-relative
        "frames": ["frame-1.png", ... ]                # optional; else globbed in order
    }

Usage:
    python publish_story.py                 # publish today's story (all frames)
    python publish_story.py --date 2026-05-29
    python publish_story.py --dry-run       # resolve + list frames, no posting

Env (same as cloud_publish.py):
    IG_USER_ID, IG_ACCESS_TOKEN

Idempotent: logs each frame to published_log.json under story_frame_<N>; skips
already-published frames so a safety-net retry never double-posts.
"""
import os
import sys
import json
import time
import argparse
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
import requests

import slot_lock

ROOT = Path(__file__).resolve().parent
SCHED_PATH = ROOT / "publish_schedule.json"
LOG_PATH = ROOT / "published_log.json"
API_BASE = "https://graph.facebook.com/v21.0"
RAW_BASE = "https://raw.githubusercontent.com/realdanigomez/geuphoria-publisher/main"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("publish_story")

IG_USER_ID = os.environ.get("IG_USER_ID")
IG_TOKEN = os.environ.get("IG_ACCESS_TOKEN")


def aruba_today() -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=4)).strftime("%Y-%m-%d")


def load_log() -> dict:
    if LOG_PATH.exists():
        return json.loads(LOG_PATH.read_text(encoding="utf-8"))
    return {}


def mark_published(today: str, key: str, media_id: str):
    data = load_log()
    data.setdefault(today, {})[key] = media_id
    LOG_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def already_published(today: str, key: str) -> bool:
    return key in load_log().get(today, {})


def resolve_frames(slot: dict) -> list:
    """Returns [(name, url, is_video), ...]. Frames may be static PNG (legacy
    story_engine.py) or animated MP4 (story_engine_animated.py, current bar as of
    2026-07-24 — see memory story-animated-infographic-bar)."""
    folder = slot.get("folder")
    if not folder:
        raise RuntimeError("story slot has no 'folder'")
    fdir = ROOT / folder
    if not fdir.exists():
        raise FileNotFoundError(f"story folder not found in repo: {fdir}")
    frames = slot.get("frames")
    if frames:
        names = list(frames)
    else:
        names = sorted(f.name for f in fdir.iterdir()
                       if f.name.lower().startswith("frame-")
                       and f.name.lower().endswith((".png", ".mp4")))
    if not names:
        raise FileNotFoundError(f"no frame-N.png/.mp4 files in {fdir}")
    # public URL per frame
    return [(n, f"{RAW_BASE}/{folder}/{n}", n.lower().endswith(".mp4")) for n in names]


def is_before_scheduled_time(slot: dict) -> bool:
    """True if current AST time is before the slot's scheduled time."""
    slot_time_str = slot.get('slot_time_ast', '').strip()
    if not slot_time_str:
        return False
    try:
        t = datetime.strptime(slot_time_str, '%I:%M %p')
        now = datetime.now(timezone(timedelta(hours=-4)))
        return (now.hour * 60 + now.minute) < (t.hour * 60 + t.minute)
    except Exception:
        return False


def publish_story_frame(media_url: str, is_video: bool = False) -> str:
    # 1) container
    payload = {
        "media_type": "STORIES",
        "access_token": IG_TOKEN,
    }
    payload["video_url" if is_video else "image_url"] = media_url
    r = requests.post(f"{API_BASE}/{IG_USER_ID}/media", data=payload, timeout=30)
    data = r.json()
    if "id" not in data:
        raise Exception(f"story container failed: {data}")
    cid = data["id"]

    if is_video:
        # Video containers need IG-side processing before they can be published
        # (same FINISHED-polling pattern as reels in cloud_publish.publish_reel).
        for i in range(18):  # up to 3 minutes — story frames are short (~3-6s)
            time.sleep(10)
            r = requests.get(f"{API_BASE}/{cid}", params={
                "fields": "status_code",
                "access_token": IG_TOKEN,
            }, timeout=15)
            code = r.json().get("status_code", "UNKNOWN")
            log.info(f"    processing: {code} ({i+1}/18)")
            if code == "FINISHED":
                break
            if code == "ERROR":
                raise Exception("story video processing failed at Instagram end")
        else:
            raise Exception("story video processing timeout after 3 minutes")
    else:
        time.sleep(6)

    # 2) publish
    r = requests.post(f"{API_BASE}/{IG_USER_ID}/media_publish", data={
        "creation_id": cid,
        "access_token": IG_TOKEN,
    }, timeout=30)
    res = r.json()
    if "id" not in res:
        raise Exception(f"story publish failed: {res}")
    return res["id"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None)
    ap.add_argument("--slot", default="story", help="schedule slot key (story, or story_2 for 2x weeks)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    today = args.date or aruba_today()
    log.info(f"=== Story publisher: today={today} dry_run={args.dry_run} ===")

    sched = json.loads(SCHED_PATH.read_text(encoding="utf-8"))
    if today not in sched or args.slot not in sched[today]:
        log.info(f"No {args.slot} scheduled for {today}; nothing to publish. Exiting 0.")
        return 0

    slot = sched[today][args.slot]

    # Time-window guard — skip if current AST time is before scheduled time
    if not args.dry_run and is_before_scheduled_time(slot):
        log.info(f'Skipping — current AST time is before scheduled time '
                 f'({slot.get("slot_time_ast")}). Likely a delayed previous-day '
                 f'cron. Safety net will re-trigger at the correct hour.')
        return 0

    frames = resolve_frames(slot)
    log.info(f"Story '{slot.get('name')}' -> {len(frames)} frames")

    if args.dry_run:
        for i, (n, url, is_video) in enumerate(frames, 1):
            log.info(f"  [dry-run] frame {i} ({'video' if is_video else 'image'}): {url}")
        log.info("Dry-run OK — frames resolve. No posting.")
        return 0

    if not (IG_USER_ID and IG_TOKEN):
        log.error("Missing IG_USER_ID / IG_ACCESS_TOKEN env.")
        return 1

    # Claim the whole story slot before posting any frame — see slot_lock.py.
    # Per-frame keys below already stop a resumed run from re-posting a frame
    # it finished, but without a slot-level claim two concurrent runs could
    # each start the sequence from frame 1 and post the story twice.
    won, reason = slot_lock.claim_slot(today, args.slot)
    if not won:
        log.info(f"Not publishing {args.slot}: {reason}.")
        return 0
    log.info(f"Claimed {args.slot} for {today}.")

    failures = 0
    for i, (name, url, is_video) in enumerate(frames, 1):
        key = f"{args.slot}_frame_{i}"
        if already_published(today, key):
            log.info(f"  frame {i} already published ({load_log()[today][key]}); skip.")
            continue
        try:
            mid = publish_story_frame(url, is_video)
            mark_published(today, key, mid)
            log.info(f"  frame {i}/{len(frames)} published: {mid}")
            time.sleep(4)
        except Exception as e:
            log.error(f"  frame {i} FAILED: {e}")
            failures += 1

    if failures:
        log.error(f"{failures} story frame(s) failed.")
        slot_lock.release_claim(today, args.slot)
        log.info(f"Released claim on {args.slot} so a retry can resume it.")
        return 1
    if not slot_lock.complete_slot(today, args.slot, slot.get("name", "published")):
        log.error(f"POSTED but FAILED TO RECORD {args.slot} — record it manually.")
        return 1
    log.info("=== Story sequence published ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
