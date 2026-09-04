"""Cron safety net — multi-trigger edition (codified 2026-05-04).

GitHub Actions schedule cron is unreliable — historically delayed 2-4h,
sometimes skipped entirely. This script runs from multiple trigger sources
(cron every 5min, push to published_log.json, workflow_run after each
publish completes, daily heartbeat push). Together these sources ensure
the safety net fires even if all crons fail.

IDEMPOTENCY: publish scripts self-check published_log.json and skip if
the slot is already logged. Double-firing is always safe.

SLOT TYPES:
  SLOTS — recurring daily or dated one-shot slots (fired via gh workflow run)
  PENDING_LONGFORMS_PATH — JSON queue for documentary/longform source videos
    that don't have a recurring cron. Format: see pending_longforms.json.

HOW IT WORKS:
  1. Compute current AST time.
  2. For each SLOT entry: if today is in dates AND time is past (HH:MM + grace)
     AND log key is not in published_log for today → fire `gh workflow run`.
  3. For each entry in pending_longforms.json with publish_date=today AND
     publish_time_ast past grace AND log_key not yet logged → fire its workflow.
  4. Idempotent via published_log.json (and sentinel files for notify-only slots).
"""
from __future__ import annotations
import json
import os
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LOG_FILE = ROOT / "published_log.json"
PENDING_LONGFORMS_PATH = ROOT / "pending_longforms.json"
GRACE_MIN = 5  # fire only after grace period past scheduled time
# Must match slot_lock.STALE_CLAIM_MINUTES — how long an unfinished claim is
# respected before we assume the run holding it died and allow a re-fire.
STALE_CLAIM_MINUTES = 20

# ── Daily + dated one-shot slots ─────────────────────────────────────────────
# Format: (workflow_yaml, log_key, scheduled_AST_HH:MM, dates_active)
#   "all"  → every day
#   list   → specific YYYY-MM-DD dates only
#
# MAINTENANCE: update the date lists each week.
# Expired one-shot entries (past dates) are safe to leave — date filter skips them.
# Add next week's dates before Monday 8AM.

SLOTS = [
    # ── Daily slots (all dates) ────────────────────────────────────────────
    ("publish-reel.yml",                    "reel",                    "08:00", "all"),
    ("publish-yt-reel.yml",                 "yt_reel",                 "08:00", "all"),
    ("publish-carousel.yml",                "carousel",                "12:00", "all"),
    ("publish-yt-carousel-asset.yml",       "yt_carousel_asset_notif", "12:01", "all"),
    # NOTE: yt_carousel is posted MANUALLY via YouTube Studio Community Posts (not Shorts).
    # The slides live in carousels/*-youtube/ and captions in captions/carousel-*/caption-youtube.txt.

    # ── 2x split-test: 2nd daily slots (reel_2 / carousel_2 / story_2) ─────
    # Dispatch-only workflows fired by THIS safety net. Date lists are EMPTY on 1x weeks
    # (never fire). `/go-live` populates them with the week's dates ONLY for a 2x week,
    # and adds the matching reel_2/carousel_2/story_2 entries to publish_schedule.json.
    # One-off exception: 2026-09-14 carries should-i-quit's "B" hook variant as a single
    # bonus reel (Dani's call, 2026-08-31) — not a full 2x week, just this one day.
    ("publish-reel-2.yml",      "reel_2",      "17:00", ["2026-09-14"]),
    ("publish-yt-reel-2.yml",   "yt_reel_2",   "17:00", ["2026-09-14"]),
    ("publish-carousel-2.yml",  "carousel_2",  "18:00", []),
    ("publish-story-2.yml",     "story_2",     "11:00", []),

    # ── Doc-clip + longform-clip — 2026-09-01 through 2026-09-14 go-live ────
    # No 2x slots used this cycle (reel_2/carousel_2/story_2 stay empty — the
    # backlog was spread across 2 weeks of the proven 1x cadence instead).
    # UPDATE THIS LIST each week before Monday 8AM.
    ("publish-doc-clip.yml",      "doc_clip",      "14:00",
     ["2026-09-05","2026-09-09","2026-09-12"]),
    ("publish-longform-clip.yml", "longform_clip", "15:00",
     ["2026-09-02","2026-09-03","2026-09-04","2026-09-05","2026-09-06","2026-09-07",
      "2026-09-09","2026-09-10","2026-09-11","2026-09-12","2026-09-13","2026-09-14"]),

    # ── Longform-clip bonus — not used this cycle (12 clips fit 1/day) ─────
    ("publish-longform-clip-bonus.yml", "longform_clip_bonus", "15:30", []),

    # ── Story — 7PM AST daily ──────────────────────────────────────────────
    # UPDATE THIS LIST each week before Monday 8AM.
    ("publish-story.yml", "story", "19:00",
     ["2026-09-01","2026-09-03","2026-09-05","2026-09-07","2026-09-09","2026-09-11","2026-09-13"]),

    # ── Dated one-shots (documentary episodes, special longforms) ──────────
    # NEW PATTERN as of 2026-05-05: dated one-shots go in pending_longforms.json,
    # NOT as standalone .yml files with dated cron schedules. The May 4 one-shot
    # workflows have been archived to .github/workflows/.archived/ because their
    # dated crons fired LATE on May 5 06:29 UTC (8.5h delayed) — when delayed
    # across AST midnight, the publish script's `today` check uses the new date
    # and republished content under the wrong key (yt_doc_ep3 duplicate Ep3 +
    # doc_clip published next day's content 11.5h early). Use pending_longforms.json
    # for new one-shots; the safety net's date filter prevents this bug.
]


# ── Time helpers ─────────────────────────────────────────────────────────────

def aruba_now() -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=-4)


def is_overdue(hh_mm: str) -> bool:
    """True if current AST time >= HH:MM today + GRACE_MIN."""
    h, m = map(int, hh_mm.split(":"))
    now = aruba_now()
    target = now.replace(hour=h, minute=m, second=0, microsecond=0)
    target += timedelta(minutes=GRACE_MIN)
    return now >= target


# ── Log helpers ───────────────────────────────────────────────────────────────

def load_log() -> dict:
    if LOG_FILE.exists():
        try:
            return json.loads(LOG_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print(f"[scheduler] WARN: {LOG_FILE} is corrupt; treating as empty")
    return {}


def slot_settled(entry) -> bool:
    """True if this slot needs no further firing.

    A slot value is either a completed media-id string or an in-flight claim
    dict written by slot_lock. Both mean "don't fire" — done, or owned by a
    live run. A claim older than the staleness window means its holder died,
    so the slot IS fireable again; that is what stops a crashed run from
    silently costing the day's post.
    """
    if isinstance(entry, str):
        return True
    if isinstance(entry, dict):
        if entry.get("media_id"):
            return True
        claimed_at = entry.get("claimed_at")
        if not claimed_at:
            return False
        try:
            ts = datetime.fromisoformat(claimed_at.replace("Z", "+00:00"))
        except ValueError:
            return False
        return (datetime.now(timezone.utc) - ts) < timedelta(minutes=STALE_CLAIM_MINUTES)
    return False


# ── Workflow trigger ──────────────────────────────────────────────────────────

def fire_workflow(yaml_name: str, dry_run: bool = False) -> bool:
    """Trigger a workflow_dispatch via gh CLI. Returns True on success."""
    if dry_run:
        print(f"[scheduler] [DRY-RUN] would fire {yaml_name}")
        return True
    print(f"[scheduler] FIRING {yaml_name}")
    result = subprocess.run(
        ["gh", "workflow", "run", yaml_name, "--ref", "main"],
        capture_output=True, text=True
    )
    if result.returncode == 0:
        print(f"[scheduler]   OK: {result.stdout.strip() or 'dispatched'}")
        return True
    print(f"[scheduler]   FAIL: rc={result.returncode} stderr={result.stderr.strip()}")
    return False


# ── Pending longforms queue ───────────────────────────────────────────────────
# pending_longforms.json format:
# [
#   {
#     "name": "documentary-ep4",
#     "workflow": "publish-yt-doc-ep4.yml",
#     "log_key": "yt_doc_ep4",
#     "publish_date_ast": "2026-05-11",
#     "publish_time_ast": "18:00"
#   }
# ]
# Add entries here when a new documentary or longform source video is approved.
# The scheduler fires the workflow on the specified date + time.
# Once the workflow logs the video_id, it won't fire again (idempotent).

def check_pending_longforms(today: str, today_log: dict, dry_run: bool = False) -> tuple[int, int]:
    """Check pending_longforms.json and fire any that are overdue + unlogged."""
    fired = 0
    skipped = 0
    if not PENDING_LONGFORMS_PATH.exists():
        return fired, skipped

    try:
        entries = json.loads(PENDING_LONGFORMS_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        print(f"[scheduler] WARN: could not read {PENDING_LONGFORMS_PATH}: {e}")
        return fired, skipped

    for entry in entries:
        name = entry.get("name", "?")
        workflow = entry.get("workflow")
        log_key = entry.get("log_key")
        pub_date = entry.get("publish_date_ast")
        pub_time = entry.get("publish_time_ast", "18:00")

        if not all([workflow, log_key, pub_date]):
            print(f"[scheduler] SKIP pending entry {name}: missing required fields")
            skipped += 1
            continue

        if pub_date != today:
            skipped += 1
            continue

        if not is_overdue(pub_time):
            print(f"[scheduler] - {workflow} ({log_key} @ {pub_time}): not yet due [pending]")
            skipped += 1
            continue

        if slot_settled(today_log.get(log_key)):
            print(f"[scheduler] - {workflow} ({log_key} @ {pub_time}): already logged [pending]")
            skipped += 1
            continue

        print(f"[scheduler] PENDING LONGFORM overdue: {name} ({log_key} @ {pub_time})")
        if fire_workflow(workflow, dry_run=dry_run):
            fired += 1

    return fired, skipped


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    dry_run = "--dry-run" in sys.argv or os.environ.get("DRY_RUN") == "true"
    now = aruba_now()
    today = now.strftime("%Y-%m-%d")

    print(f"[scheduler] === Safety net @ AST {now:%Y-%m-%d %H:%M:%S} (UTC{now.utcoffset()}) ===")
    if dry_run:
        print("[scheduler] (DRY-RUN MODE — no workflows will actually fire)")

    log_data = load_log()
    today_log = log_data.get(today, {})
    print(f"[scheduler] Today's log keys: {sorted(today_log.keys()) or '(none)'}")

    fired = 0
    skipped = 0

    # ── Check regular SLOTS ────────────────────────────────────────────────
    for yaml_name, key, sched_time, dates in SLOTS:
        # Date filter
        if dates != "all" and today not in dates:
            continue
        # Time filter
        if not is_overdue(sched_time):
            print(f"[scheduler] - {yaml_name} ({key} @ {sched_time}): not yet due")
            skipped += 1
            continue
        # Already logged?
        if slot_settled(today_log.get(key)):
            print(f"[scheduler] - {yaml_name} ({key} @ {sched_time}): already logged ({str(today_log[key])[:24]})")
            skipped += 1
            continue
        # Special case: yt_carousel_asset_notif is a notify-only workflow that
        # doesn't write to published_log. Use a sentinel file instead.
        if key == "yt_carousel_asset_notif":
            sentinel = ROOT / ".last_fire" / f"{today}-{key}"
            if sentinel.exists():
                print(f"[scheduler] - {yaml_name} ({key}): sentinel exists, already fired today")
                skipped += 1
                continue
        # Overdue + not logged → fire
        if fire_workflow(yaml_name, dry_run=dry_run):
            fired += 1
            if key == "yt_carousel_asset_notif" and not dry_run:
                sentinel.parent.mkdir(exist_ok=True)
                sentinel.write_text(now.isoformat())

    # ── Check pending longforms queue ──────────────────────────────────────
    pf_fired, pf_skipped = check_pending_longforms(today, today_log, dry_run=dry_run)
    fired += pf_fired
    skipped += pf_skipped

    print(f"[scheduler] === Done. Fired={fired}, Skipped={skipped} ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
