"""
Geuphoria Cloud Publisher
Runs in GitHub Actions — no computer needed, no Claude Code needed.
Posts reels at 8AM AST and carousels at 12PM AST every day.

Environment variables (set as GitHub Secrets):
  IG_USER_ID            Instagram account ID
  IG_ACCESS_TOKEN       Instagram page access token
  FREEIMAGE_KEY         freeimage.host API key
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


# ── Config from env ────────────────────────────────────────────────
IG_USER_ID   = os.environ['IG_USER_ID']
IG_TOKEN     = os.environ['IG_ACCESS_TOKEN']
FREEIMAGE_KEY = os.environ.get('FREEIMAGE_KEY', '6d207e02198a847aa98d0a2a901485a5')


# ── Schedule ───────────────────────────────────────────────────────
def load_schedule():
    path = os.path.join(os.path.dirname(__file__), 'publish_schedule.json')
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)


# ── Upload helpers ─────────────────────────────────────────────────
def upload_image(image_path):
    """Upload local PNG to freeimage.host and return public URL."""
    log.info(f'  Uploading image: {os.path.basename(image_path)}')
    with open(image_path, 'rb') as f:
        img_b64 = base64.b64encode(f.read()).decode()
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
    slides_dir = os.path.join(os.path.dirname(__file__), 'carousels', folder_name)
    if not os.path.exists(slides_dir):
        raise FileNotFoundError(f'Carousel folder not found: {slides_dir}')

    slides = sorted([f for f in os.listdir(slides_dir) if f.lower().endswith('.png')])
    if not slides:
        raise FileNotFoundError(f'No PNG slides in: {slides_dir}')

    log.info(f'Publishing carousel: {folder_name} ({len(slides)} slides)')

    children_ids = []
    for i, slide in enumerate(slides):
        url = upload_image(os.path.join(slides_dir, slide))
        r = requests.post(f'{API_BASE}/{IG_USER_ID}/media', data={
            'image_url': url,
            'is_carousel_item': 'true',
            'access_token': IG_TOKEN,
        }, timeout=30)
        data = r.json()
        if 'id' not in data:
            raise Exception(f'Slide {i+1} container failed: {data}')
        children_ids.append(data['id'])
        log.info(f'  Slide {i+1}: container {data["id"]}')
        time.sleep(2)

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
    r = requests.post(f'{API_BASE}/{IG_USER_ID}/media_publish', data={
        'creation_id': container['id'],
        'access_token': IG_TOKEN,
    }, timeout=30)
    result = r.json()
    if 'id' not in result:
        raise Exception(f'Publish failed: {result}')

    log.info(f'CAROUSEL PUBLISHED. Post ID: {result["id"]}')
    return result['id']


# ── Publish reel ───────────────────────────────────────────────────
def publish_reel(cdn_url, caption):
    log.info(f'Publishing reel from CDN: {cdn_url}')

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
            raise Exception('Video processing failed')
    else:
        raise Exception('Video processing timeout')

    r = requests.post(f'{API_BASE}/{IG_USER_ID}/media_publish', data={
        'creation_id': container_id,
        'access_token': IG_TOKEN,
    }, timeout=30)
    result = r.json()
    if 'id' not in result:
        raise Exception(f'Reel publish failed: {result}')

    log.info(f'REEL PUBLISHED. Post ID: {result["id"]}')
    return result['id']


# ── Main ───────────────────────────────────────────────────────────
def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ('reel', 'carousel'):
        print('Usage: python cloud_publish.py reel|carousel')
        sys.exit(1)

    content_type = sys.argv[1]
    today = datetime.now(AST).date().isoformat()

    log.info(f'=== Cloud publisher: {content_type} for {today} ===')

    schedule = load_schedule()
    if today not in schedule:
        log.info(f'Nothing scheduled for {today}. Done.')
        return

    slot = schedule[today].get(content_type)
    if not slot:
        log.info(f'No {content_type} for {today}. Done.')
        return

    name = slot['name']
    log.info(f'Target: {name}')

    try:
        if content_type == 'reel':
            cdn_url = slot.get('cdn_url')
            if not cdn_url:
                raise Exception(f'No cdn_url in schedule for {name}')
            media_id = publish_reel(cdn_url, slot['caption'])

        elif content_type == 'carousel':
            media_id = publish_carousel(slot['folder'], slot['caption'])

        # Write result for GitHub Actions step summary
        summary = f'Published {content_type}: {name} — Post ID: {media_id}'
        log.info(summary)
        summary_file = os.environ.get('GITHUB_STEP_SUMMARY')
        if summary_file:
            with open(summary_file, 'a') as f:
                f.write(f'## Published\n- **{content_type}**: {name}\n- **Post ID**: `{media_id}`\n- **Date**: {today}\n')

    except Exception as e:
        log.error(f'FAILED: {e}')
        sys.exit(1)

    log.info(f'=== Done ===')


if __name__ == '__main__':
    main()
