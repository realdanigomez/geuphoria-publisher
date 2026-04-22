"""
Geuphoria Cloud Publisher
Runs in GitHub Actions — no computer needed, no Claude Code needed.
Posts reels at 8AM AST (12:00 UTC) and carousels at 12PM AST (16:00 UTC).

Dedup: published_log.json is committed back to the repo after each post.
Caption: always sent in request body (data=), never in URL params — prevents truncation.

Carousel images: served via GitHub raw content URLs (repo is public, permanent + free).
Reel videos: served via GitHub Releases (permanent + free, accessible worldwide).

Environment variables (GitHub Secrets):
  IG_USER_ID        Instagram account ID
  IG_ACCESS_TOKEN   Instagram page access token (long-lived)
"""

import sys
import os
import json
import time
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
GITHUB_RAW_BASE = 'https://raw.githubusercontent.com/realdanigomez/geuphoria-publisher/main'

# ── Credentials from env ───────────────────────────────────────────
IG_USER_ID = os.environ['IG_USER_ID']
IG_TOKEN   = os.environ['IG_ACCESS_TOKEN']


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
def image_url_for_slide(folder_name, slide_name):
    """Return a permanent public URL for a carousel slide.

    Images are committed to the repo so GitHub raw content URLs are permanent,
    free, and don't require any upload step. freeimage.host was unreliable
    (returned 400 errors) so we no longer use it.
    """
    url = f'{GITHUB_RAW_BASE}/carousels/{folder_name}/{slide_name}'
    log.info(f'  Slide URL: {url}')
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

    # Create child containers using GitHub raw content URLs (no upload needed)
    children_ids = []
    for i, slide in enumerate(slides):
        url = image_url_for_slide(folder_name, slide)
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
    if not cdn_url:
        raise Exception('cdn_url is required for reel publishing in cloud mode')

    log.info(f'Publishing reel from: {cdn_url}')
    log.info(f'Caption ({len(caption)} chars): {caption[:100]}...')

    # Retry up to 2 times — handles transient CDN or Instagram processing errors
    for attempt in range(3):
        if attempt > 0:
            log.info(f'Retry attempt {attempt + 1}/3 (waiting 30s)...')
            time.sleep(30)

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
        processing_ok = False
        for i in range(30):
            time.sleep(10)
            r = requests.get(f'{API_BASE}/{container_id}', params={
                'fields': 'status_code',
                'access_token': IG_TOKEN,
            }, timeout=15)
            code = r.json().get('status_code', 'UNKNOWN')
            log.info(f'  Processing: {code} ({i+1}/30)')
            if code == 'FINISHED':
                processing_ok = True
                break
            if code == 'ERROR':
                log.warning(f'  Instagram returned ERROR on attempt {attempt + 1}')
                break
        else:
            raise Exception('Video processing timeout after 5 minutes')

        if processing_ok:
            break
        if attempt == 2:
            raise Exception('Video processing failed at Instagram end after 3 attempts')

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

    name = slot['name']
    caption = slot['caption']
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
