"""
Geuphoria Cloud Publisher
Runs in GitHub Actions — no computer needed, no Claude Code needed.
Posts reels at 8AM AST (12:00 UTC) and carousels at 12PM AST (16:00 UTC).

Dedup: published_log.json is committed back to the repo after each post.
Caption: always sent in request body (data=), never in URL params — prevents truncation.

Environment variables (GitHub Secrets):
  IG_USER_ID        Instagram account ID
  IG_ACCESS_TOKEN   Instagram page access token (long-lived)
  FREEIMAGE_KEY     freeimage.host API key
"""

import sys
import os
import json
import time
import base64
import logging
import requests
from datetime import date, datetime, timezone, timedelta

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger('cloud_publisher')

API_BASE = 'https://graph.facebook.com/v21.0'
AST = timezone(timedelta(hours=-4))

ROOT = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(ROOT, 'published_log.json')

# ── Credentials from env ───────────────────────────────────────────
IG_USER_ID    = os.environ['IG_USER_ID']
IG_TOKEN      = os.environ['IG_ACCESS_TOKEN']
FREEIMAGE_KEY = os.environ.get('FREEIMAGE_KEY', '6d207e02198a847aa98d0a2a901485a5')


# ── Schedule ───────────────────────────────────────────────────────
def load_schedule():
    with open(os.path.join(ROOT, 'publish_schedule.json'), 'r', encoding='utf-8') as f:
        return json.load(f)


# ── Published log (dedup) ──────────────────────────────────────────
def load_log():
    if os.path.exists(LOG_PATH):
        with open(LOG_PATH, 'r') as f:
            return json.load(f)
    return {}


def is_already_published(today, content_type):
    log_data = load_log()
    return today in log_data and content_type in log_data[today]


def mark_published(today, content_type, media_id):
    log_data = load_log()
    if today not in log_data:
        log_data[today] = {}
    log_data[today][content_type] = media_id
    with open(LOG_PATH, 'w') as f:
        json.dump(log_data, f, indent=2)
    log.info(f'Logged: {today} {content_type} = {media_id}')


# ── Upload helpers ─────────────────────────────────────────────────
def upload_image(image_path):
    """Upload local PNG to freeimage.host. Returns public URL."""
    log.info(f'  Uploading: {os.path.basename(image_path)}')
    with open(image_path, 'rb') as f:
        img_b64 = base64.b64encode(f.read()).decode()
    # NOTE: data= sends as request body — not URL params
    r = requests.post('https://freeimage.host/api/1/upload', data={
        'key': FREEIMAGE_KEY,
        'source': img_b64,
        'format': 'json',
    }, timeout=30)
    r.raise_for_status()
    url = r.json()['image']['url']
    log.info(f'    -> {url}')
    return url


# ── Publish carousel ───────────────────────────────────────────────
def publish_carousel(folder_name, caption):
    slides_dir = os.path.join(ROOT, 'carousels', folder_name)
    if not os.path.exists(slides_dir):
        raise FileNotFoundError(f'Carousel folder not found: {slides_dir}')

    slides = sorted([f for f in os.listdir(slides_dir) if f.lower().endswith('.png')])
    if not slides:
        raise FileNotFoundError(f'No PNG slides in: {slides_dir}')

    log.info(f'Publishing carousel: {folder_name} ({len(slides)} slides)')
    log.info(f'Caption ({len(caption)} chars): {caption[:100]}...')

    # Upload each slide and create child containers
    children_ids = []
    for i, slide in enumerate(slides):
        url = upload_image(os.path.join(slides_dir, slide))
        # NOTE: caption NOT on child containers — only on parent CAROUSEL container
        # NOTE: data= (body), NOT params= (URL) — prevents truncation
        r = requests.post(f'{API_BASE}/{IG_USER_ID}/media', data={
            'image_url': url,
            'is_carousel_item': 'true',
            'access_token': IG_TOKEN,
        }, timeout=30)
        data = r.json()
        if 'id' not in data:
            raise Exception(f'Slide {i+1} container failed: {data}')
        children_ids.append(data['id'])
        log.info(f'  Slide {i+1}/{len(slides)}: {data["id"]}')
        time.sleep(2)

    # Create carousel container — full caption goes here
    # NOTE: data= (body), NOT params= (URL) — critical for captions with newlines/hashtags
    r = requests.post(f'{API_BASE}/{IG_USER_ID}/media', data={
        'media_type': 'CAROUSEL',
        'children': ','.join(children_ids),
        'caption': caption,
        'access_token': IG_TOKEN,
    }, timeout=30)
    container = r.json()
    if 'id' not in container:
        raise Exception(f'Carousel container failed: {container}')
    log.info(f'  Carousel container: {container["id"]}')

    time.sleep(12)

    # Publish
    r = requests.post(f'{API_BASE}/{IG_USER_ID}/media_publish', data={
        'creation_id': container['id'],
        'access_token': IG_TOKEN,
    }, timeout=30)
    result = r.json()
    if 'id' not in result:
        raise Exception(f'Publish failed: {result}')

    log.info(f'CAROUSEL PUBLISHED. Post ID: {result["id"]}')

    # Verify caption was received correctly
    time.sleep(3)
    verify = requests.get(f'{API_BASE}/{result["id"]}', params={
        'fields': 'caption',
        'access_token': IG_TOKEN,
    }, timeout=15).json()
    posted_caption = verify.get('caption', '')
    if caption.strip() in posted_caption or len(posted_caption) >= len(caption) * 0.9:
        log.info(f'Caption verified OK ({len(posted_caption)} chars on Instagram).')
    else:
        log.warning(f'Caption may be truncated! Sent {len(caption)} chars, got {len(posted_caption)} chars back.')

    return result['id']


# ── Publish reel ───────────────────────────────────────────────────
def publish_reel(cdn_url, caption):
    log.info(f'Publishing reel from: {cdn_url}')
    log.info(f'Caption ({len(caption)} chars): {caption[:100]}...')

    if not cdn_url:
        raise Exception('cdn_url is required for reel publishing in cloud mode')

    # Create reel container
    # NOTE: data= (body), NOT params= (URL) — critical for full caption
    r = requests.post(f'{API_BASE}/{IG_USER_ID}/media', data={
        'media_type': 'REELS',
        'video_url': cdn_url,
        'caption': caption,
        'access_token': IG_TOKEN,
    }, timeout=30)
    data = r.json()
    if 'id' not in data:
        raise Exception(f'Reel container failed: {data}')
    container_id = data['id']
    log.info(f'  Reel container: {container_id}')

    # Poll for Instagram video processing (up to 5 minutes)
    for i in range(30):
        time.sleep(10)
        r = requests.get(f'{API_BASE}/{container_id}', params={
            'fields': 'status_code',
            'access_token': IG_TOKEN,
        }, timeout=15)
        code = r.json().get('status_code', 'UNKNOWN')
        log.info(f'  Processing: {code} ({i+1}/30)')
        if code == 'FINISHED':
            break
        if code == 'ERROR':
            raise Exception('Video processing failed at Instagram end')
    else:
        raise Exception('Video processing timeout after 5 minutes')

    # Publish
    r = requests.post(f'{API_BASE}/{IG_USER_ID}/media_publish', data={
        'creation_id': container_id,
        'access_token': IG_TOKEN,
    }, timeout=30)
    result = r.json()
    if 'id' not in result:
        raise Exception(f'Reel publish failed: {result}')

    log.info(f'REEL PUBLISHED. Post ID: {result["id"]}')

    # Verify caption was received correctly
    time.sleep(3)
    verify = requests.get(f'{API_BASE}/{result["id"]}', params={
        'fields': 'caption',
        'access_token': IG_TOKEN,
    }, timeout=15).json()
    posted_caption = verify.get('caption', '')
    if caption.strip() in posted_caption or len(posted_caption) >= len(caption) * 0.9:
        log.info(f'Caption verified OK ({len(posted_caption)} chars on Instagram).')
    else:
        log.warning(f'Caption may be truncated! Sent {len(caption)} chars, got {len(posted_caption)} chars back.')

    return result['id']


# ── Time-window guard ─────────────────────────────────────────────
def is_before_scheduled_time(slot: dict) -> bool:
    """True if current AST time is before the slot's scheduled time.
    Prevents a GH cron delayed from the previous day from posting
    the next day's content before its scheduled hour (e.g. midnight)."""
    slot_time_str = slot.get('slot_time_ast', '').strip()
    if not slot_time_str:
        return False
    try:
        t = datetime.strptime(slot_time_str, '%I:%M %p')
        now = datetime.now(AST)
        return (now.hour * 60 + now.minute) < (t.hour * 60 + t.minute)
    except Exception:
        return False


# ── Main ───────────────────────────────────────────────────────────
def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ('reel', 'carousel'):
        print('Usage: python cloud_publish.py reel|carousel')
        sys.exit(1)

    content_type = sys.argv[1]
    today = datetime.now(AST).date().isoformat()

    log.info(f'=== Cloud publisher: {content_type} for {today} (AST) ===')

    # Dedup check — never double-post
    if is_already_published(today, content_type):
        existing = load_log()[today][content_type]
        log.info(f'Already published today ({content_type}: post {existing}). Skipping.')
        return

    schedule = load_schedule()
    if today not in schedule:
        log.info(f'Nothing scheduled for {today}. Done.')
        return

    slot = schedule[today].get(content_type)
    if not slot:
        log.info(f'No {content_type} for {today}. Done.')
        return

    # Time-window guard — skip if current AST time is before scheduled time
    if is_before_scheduled_time(slot):
        log.info(f'Skipping — current AST time is before scheduled time '
                 f'({slot.get("slot_time_ast")}). Likely a delayed previous-day '
                 f'cron. Safety net will re-trigger at the correct hour.')
        return

    name = slot['name']
    # Caption can be specified inline as `caption` (legacy) or via `caption_path`
    # pointing at a file on disk. Caption files take priority when both are set.
    caption_path = slot.get('caption_path')
    if caption_path:
        cap_full = os.path.join(ROOT, caption_path)
        if not os.path.exists(cap_full):
            raise FileNotFoundError(f'Caption file not found: {cap_full}')
        with open(cap_full, 'r', encoding='utf-8') as f:
            caption = f.read().strip()
        log.info(f'Read caption from file: {caption_path} ({len(caption)} chars)')
    else:
        caption = slot.get('caption')
        if not caption:
            raise Exception(f'No caption or caption_path in slot for {name}')
    log.info(f'Target: {name} | Caption: {len(caption)} chars')

    try:
        if content_type == 'reel':
            cdn_url = slot.get('cdn_url')
            if not cdn_url:
                raise Exception(f'No cdn_url in schedule for {name}. Upload the video to CDN first.')
            media_id = publish_reel(cdn_url, caption)

        elif content_type == 'carousel':
            media_id = publish_carousel(slot['folder'], caption)

        # Record success — prevents double-post on retry
        mark_published(today, content_type, media_id)

        # Write GitHub Actions step summary
        summary_file = os.environ.get('GITHUB_STEP_SUMMARY')
        if summary_file:
            with open(summary_file, 'a', encoding='utf-8') as f:
                f.write(f'## Published\n')
                f.write(f'- **Type**: {content_type}\n')
                f.write(f'- **Name**: {name}\n')
                f.write(f'- **Post ID**: `{media_id}`\n')
                f.write(f'- **Date**: {today}\n')
                f.write(f'- **Caption length**: {len(caption)} chars\n')

    except Exception as e:
        log.error(f'PUBLISH FAILED: {e}')
        import traceback
        log.error(traceback.format_exc())
        sys.exit(1)

    log.info(f'=== Done ===')


if __name__ == '__main__':
    main()
