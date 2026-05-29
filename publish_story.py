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
                       if f.name.lower().startswith("frame-") and f.name.lower().endswith(".png"))
    if not names:
        raise FileNotFoundError(f"no frame-N.png files in {fdir}")
    # public URL per frame
    return [(n, f"{RAW_BASE}/{folder}/{n}") for n in names]


def publish_story_frame(image_url: str) -> str:
    # 1) container
    r = requests.post(f"{API_BASE}/{IG_USER_ID}/media", data={
        "media_type": "STORIES",
        "image_url": image_url,
        "access_token": IG_TOKEN,
    }, timeout=30)
    data = r.json()
    if "id" not in data:
        raise Exception(f"story container failed: {data}")
    cid = data["id"]
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
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    today = args.date or aruba_today()
    log.info(f"=== Story publisher: today={today} dry_run={args.dry_run} ===")

    sched = json.loads(SCHED_PATH.read_text(encoding="utf-8"))
    if today not in sched or "story" not in sched[today]:
        log.info(f"No story scheduled for {today}; nothing to publish. Exiting 0.")
        return 0

    slot = sched[today]["story"]
    frames = resolve_frames(slot)
    log.info(f"Story '{slot.get('name')}' -> {len(frames)} frames")

    if args.dry_run:
        for i, (n, url) in enumerate(frames, 1):
            log.info(f"  [dry-run] frame {i}: {url}")
        log.info("Dry-run OK — frames resolve. No posting.")
        return 0

    if not (IG_USER_ID and IG_TOKEN):
        log.error("Missing IG_USER_ID / IG_ACCESS_TOKEN env.")
        return 1

    failures = 0
    for i, (name, url) in enumerate(frames, 1):
        key = f"story_frame_{i}"
        if already_published(today, key):
            log.info(f"  frame {i} already published ({load_log()[today][key]}); skip.")
            continue
        try:
            mid = publish_story_frame(url)
            mark_published(today, key, mid)
            log.info(f"  frame {i}/{len(frames)} published: {mid}")
            time.sleep(4)
        except Exception as e:
            log.error(f"  frame {i} FAILED: {e}")
            failures += 1

    if failures:
        log.error(f"{failures} story frame(s) failed.")
        return 1
    mark_published(today, "story", slot.get("name", "published"))
    log.info("=== Story sequence published ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
