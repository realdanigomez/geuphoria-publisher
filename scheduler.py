"""Cron safety net.

GitHub Actions schedule cron is unreliable — historically delayed 30 min to
several hours, sometimes skipped entirely. This script runs every 10 minutes
from `cron-safety-net.yml`, computes current AST time, and fires any
publish workflow that:
  1. Is scheduled to have run by now (current AST time >= scheduled + grace)
  2. Has not yet been logged in published_log.json for today
  3. (For dated one-shots) is for today's date

Each fire is a `gh workflow run <yaml>` — same as a manual dispatch. The
GITHUB_TOKEN with `actions: write` permission can trigger workflow_dispatch
on workflows in the same repo. Workflow_run downstream hooks do NOT fire
from a GITHUB_TOKEN-triggered dispatch (GitHub's loop-protection), which is
fine for us since the auto Story-reshare hook was disabled on 2026-05-04.

Idempotency: the underlying publish scripts check published_log.json
themselves and skip if the slot is already published for today, so
double-firing this script is safe.
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
GRACE_MIN = 5  # cron has up to ~30min jitter; only fire after grace

# Slot definitions: (workflow_yaml, log_key_to_check, scheduled_AST_HH:MM, dates_active)
# - "all" means every day
# - For one-shots, list specific YYYY-MM-DD strings
# - log_key is what the workflow's publish script writes to published_log.json[date][key]
SLOTS = [
    # Daily slots (all dates)
    ("publish-reel.yml",       "reel",        "08:00", "all"),
    ("publish-yt-reel.yml",    "yt_reel",     "08:00", "all"),
    ("publish-carousel.yml",   "carousel",    "12:00", "all"),
    ("publish-yt-carousel-asset.yml", "yt_carousel_asset_notif", "12:01", "all"),

    # Daily slots — only fire on May 5+ (May 4's clips are the *-may4.yml versions below)
    ("publish-doc-clip.yml",      "doc_clip",      "14:00",
     ["2026-05-05","2026-05-06","2026-05-07","2026-05-08","2026-05-09","2026-05-10"]),
    ("publish-longform-clip.yml", "longform_clip", "15:00",
     ["2026-05-05","2026-05-06","2026-05-07","2026-05-08","2026-05-09","2026-05-10"]),

    # May 4 one-shots (longforms + evening clips)
    ("publish-yt-burnout-longform-may4.yml", "yt_burnout_longform", "15:00", ["2026-05-04"]),
    ("publish-yt-doc-ep3-may4.yml",          "yt_doc_ep3",          "18:00", ["2026-05-04"]),
    ("publish-doc-clip-may4.yml",            "doc_clip",            "18:30", ["2026-05-04"]),
    ("publish-longform-clip-may4.yml",       "longform_clip",       "19:30", ["2026-05-04"]),
]


def aruba_now() -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=-4)


def is_overdue(hh_mm: str) -> bool:
    """True if current AST time is past HH:MM today + grace minutes."""
    h, m = map(int, hh_mm.split(":"))
    now = aruba_now()
    target = now.replace(hour=h, minute=m, second=0, microsecond=0)
    target += timedelta(minutes=GRACE_MIN)
    return now >= target


def load_log() -> dict:
    if LOG_FILE.exists():
        try:
            return json.loads(LOG_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print(f"[scheduler] WARN: {LOG_FILE} is corrupt; treating as empty")
    return {}


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
        print(f"[scheduler]   OK: {result.stdout.strip()}")
        return True
    print(f"[scheduler]   FAIL: rc={result.returncode} stderr={result.stderr.strip()}")
    return False


def main() -> int:
    dry_run = "--dry-run" in sys.argv or os.environ.get("DRY_RUN") == "true"
    now = aruba_now()
    today = now.strftime("%Y-%m-%d")
    print(f"[scheduler] === Cron safety net @ AST {now:%Y-%m-%d %H:%M:%S} (UTC{now.utcoffset()}) ===")
    if dry_run:
        print("[scheduler] (DRY-RUN MODE — no workflows will actually fire)")

    log_data = load_log()
    today_log = log_data.get(today, {})
    print(f"[scheduler] Today's log keys: {sorted(today_log.keys()) or '(none)'}")

    fired = 0
    skipped = 0
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
        if key in today_log:
            print(f"[scheduler] - {yaml_name} ({key} @ {sched_time}): already logged ({today_log[key][:24]})")
            skipped += 1
            continue
        # Special case: yt_carousel_asset_notif is a notify-only workflow that
        # doesn't write to published_log. Use a different sentinel: skip if
        # we've already fired it once today (track via .last_fire/<date>-<key>).
        if key == "yt_carousel_asset_notif":
            sentinel = ROOT / ".last_fire" / f"{today}-{key}"
            if sentinel.exists():
                print(f"[scheduler] - {yaml_name} ({key}): sentinel exists, already fired today")
                skipped += 1
                continue
        # Overdue + missing → fire
        if fire_workflow(yaml_name, dry_run=dry_run):
            fired += 1
            if key == "yt_carousel_asset_notif" and not dry_run:
                sentinel.parent.mkdir(exist_ok=True)
                sentinel.write_text(now.isoformat())

    print(f"[scheduler] === Done. Fired={fired}, skipped={skipped} ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
