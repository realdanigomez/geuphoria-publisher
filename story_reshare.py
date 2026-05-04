"""IG Story reshare for a just-published feed post.

Reads the latest media_id for today from published_log.json and reshares it
as a Story via Graph API. Idempotent: skips if `<slot>_story` is already
logged for today.

Usage (from GitHub Actions or local):
  python story_reshare.py reel
  python story_reshare.py carousel
  python story_reshare.py reel --media-id 17876520645465609   # explicit override

Env vars required:
  IG_USER_ID
  IG_ACCESS_TOKEN

Logs to published_log.json under key f"{slot}_story".
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

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent
LOG_FILE = REPO_ROOT / "published_log.json"
GRAPH = "https://graph.facebook.com/v21.0"


def aruba_today() -> str:
    """Aruba is UTC-4 year-round (no DST)."""
    return (datetime.now(timezone.utc) + timedelta(hours=-4)).strftime("%Y-%m-%d")


def load_log() -> dict:
    if LOG_FILE.exists():
        return json.loads(LOG_FILE.read_text(encoding="utf-8"))
    return {}


def save_log(log_data: dict) -> None:
    LOG_FILE.write_text(json.dumps(log_data, indent=2), encoding="utf-8")


def get_media_info(media_id: str, token: str) -> dict:
    """Fetch media_url + media_type for the just-published feed post."""
    fields = "media_type,media_url,thumbnail_url,permalink,children{media_url,media_type}"
    r = requests.get(f"{GRAPH}/{media_id}", params={"fields": fields, "access_token": token}, timeout=30)
    r.raise_for_status()
    return r.json()


def create_story_container(ig_user_id: str, token: str, media_info: dict) -> str:
    """Create a Story media container.

    For VIDEO/REELS: uses video_url.
    For IMAGE/CAROUSEL_ALBUM: uses image_url. For carousels, picks first child's media_url.
    """
    media_type = media_info.get("media_type", "")
    if media_type in ("VIDEO", "REELS"):
        video_url = media_info.get("media_url")
        if not video_url:
            raise RuntimeError(f"No media_url for video media: {media_info}")
        log.info(f"Creating Story container (video) from {video_url[:80]}...")
        params = {
            "media_type": "STORIES",
            "video_url": video_url,
            "access_token": token,
        }
    elif media_type == "CAROUSEL_ALBUM":
        # Use first slide's image_url
        children = media_info.get("children", {}).get("data", [])
        if not children:
            raise RuntimeError(f"No children in carousel: {media_info}")
        first_slide = children[0]
        image_url = first_slide.get("media_url")
        if not image_url:
            raise RuntimeError(f"No media_url on first slide: {first_slide}")
        log.info(f"Creating Story container (image, slide 1 of carousel) from {image_url[:80]}...")
        params = {
            "media_type": "STORIES",
            "image_url": image_url,
            "access_token": token,
        }
    elif media_type == "IMAGE":
        image_url = media_info.get("media_url")
        log.info(f"Creating Story container (image) from {image_url[:80]}...")
        params = {
            "media_type": "STORIES",
            "image_url": image_url,
            "access_token": token,
        }
    else:
        raise RuntimeError(f"Unsupported media_type for Story reshare: {media_type}")

    r = requests.post(f"{GRAPH}/{ig_user_id}/media", data=params, timeout=60)
    r.raise_for_status()
    creation_id = r.json()["id"]
    log.info(f"Story container created: {creation_id}")
    return creation_id


def wait_for_container_ready(creation_id: str, token: str, max_wait: int = 60) -> None:
    """Poll status_code until FINISHED or timeout."""
    for attempt in range(max_wait // 3):
        r = requests.get(
            f"{GRAPH}/{creation_id}",
            params={"fields": "status_code", "access_token": token},
            timeout=30,
        )
        if r.ok:
            status = r.json().get("status_code", "")
            if status == "FINISHED":
                log.info(f"Container ready after {attempt * 3}s")
                return
            if status in ("ERROR", "EXPIRED"):
                raise RuntimeError(f"Container failed: {status} | {r.json()}")
        time.sleep(3)
    log.warning(f"Container status not FINISHED after {max_wait}s — attempting publish anyway")


def publish_story(ig_user_id: str, creation_id: str, token: str) -> str:
    """Publish the story container."""
    r = requests.post(
        f"{GRAPH}/{ig_user_id}/media_publish",
        data={"creation_id": creation_id, "access_token": token},
        timeout=60,
    )
    r.raise_for_status()
    story_id = r.json()["id"]
    log.info(f"Story published: {story_id}")
    return story_id


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("slot", choices=["reel", "carousel", "doc_clip", "longform_clip"])
    parser.add_argument("--media-id", help="Override: explicit media_id to reshare")
    parser.add_argument("--date", default=None, help="Override: YYYY-MM-DD (default = today AST)")
    args = parser.parse_args()

    token = os.environ.get("IG_ACCESS_TOKEN")
    ig_user_id = os.environ.get("IG_USER_ID")
    if not token or not ig_user_id:
        log.error("Missing IG_ACCESS_TOKEN or IG_USER_ID env vars")
        return 1

    today = args.date or aruba_today()
    log.info(f"=== Story reshare for {today} / slot={args.slot} ===")

    log_data = load_log()
    today_entry = log_data.get(today, {})

    # Idempotency check
    story_key = f"{args.slot}_story"
    if today_entry.get(story_key):
        log.info(f"Already reshared: {today}.{story_key} = {today_entry[story_key]} — skipping")
        return 0

    # Resolve media_id
    media_id = args.media_id or today_entry.get(args.slot)
    if not media_id:
        log.error(f"No media_id for {today}.{args.slot}. Pass --media-id explicitly.")
        return 1

    log.info(f"Resharing media_id={media_id}")

    # Fetch media info
    media_info = get_media_info(media_id, token)
    log.info(f"Media type: {media_info.get('media_type')} | permalink: {media_info.get('permalink')}")

    # Wait a beat for IG processing
    log.info("Waiting 10s for IG to fully process the feed media...")
    time.sleep(10)

    # Create + publish story
    creation_id = create_story_container(ig_user_id, token, media_info)
    wait_for_container_ready(creation_id, token, max_wait=60)
    story_id = publish_story(ig_user_id, creation_id, token)

    # Log
    today_entry[story_key] = story_id
    log_data[today] = today_entry
    save_log(log_data)
    log.info(f"=== Done. Story ID logged: {today}.{story_key} = {story_id} ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
