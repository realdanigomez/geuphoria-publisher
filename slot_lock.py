"""Atomic slot claiming for the publish workflows.

WHY THIS EXISTS (2026-09-03): every publisher used to guard against double
posting by reading published_log.json, checking whether today's slot was
present, posting, and only then writing the log back. That is a
check-then-act with a wide gap in the middle — on these runners the gap is
minutes, because the checkout alone took minutes. Two runs dispatched a few
minutes apart both read "not posted yet" and both posted. It produced a reel
posted 3x on 2026-09-02 and again on 2026-09-03.

The fix is to stop treating the log as an advisory note and start using it as
a real lock. `git push` is a compare-and-swap: exactly one of N racing runners
can move a ref from a given parent commit. So a runner CLAIMS the slot by
pushing a claim entry first, and only posts if its push won. The loser sees
the winner's claim on re-fetch and bails. No timing assumption anywhere.

Log entry shapes (both valid, backward compatible with all prior history):
    "reel": "17901037731354974"                 -> COMPLETED (media id string)
    "reel": {"claimed_at": "...", "claimed_by": "..."}  -> claim in flight

A claim older than STALE_CLAIM_MINUTES is treated as abandoned (the runner
holding it crashed or was cancelled) and may be taken over, so a dead claim
delays a post by at most that long instead of dropping it forever.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone, timedelta

LOG_FILE = "published_log.json"

# Longest a publish can legitimately take before we assume the holder died.
# The slowest real path is an IG Reel: container create + up to 5 min of
# Instagram-side processing + publish. 20 min leaves generous headroom.
STALE_CLAIM_MINUTES = 20

GIT_EMAIL = "bot@geuphoria.com"
GIT_NAME = "Geuphoria Publisher"


def _run(args: list[str], check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, check=check)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _run_id() -> str:
    return os.environ.get("GITHUB_RUN_ID", "local") + ":" + os.environ.get("GITHUB_RUN_ATTEMPT", "0")


def fetch_remote_log() -> dict:
    """Read published_log.json as it exists on the remote right now.

    Uses FETCH_HEAD, not origin/main: `git fetch origin main` always sets
    FETCH_HEAD, but only updates the origin/main tracking ref if the remote's
    configured fetch refspec maps to it — which actions/checkout@v4's shallow
    single-branch checkout does not reliably do. Reading origin/main here
    returned a stale pre-fetch blob and was the direct cause of the
    2026-09-03 triple post.
    """
    _run(["git", "fetch", "origin", "main"])
    r = _run(["git", "show", f"FETCH_HEAD:{LOG_FILE}"])
    if r.returncode == 0 and r.stdout.strip():
        return json.loads(r.stdout)
    return {}


def _entry_state(entry) -> str:
    """'completed' | 'claimed' | 'stale' for a single slot value."""
    if isinstance(entry, str):
        return "completed"
    if isinstance(entry, dict):
        if entry.get("media_id"):
            return "completed"
        claimed_at = entry.get("claimed_at")
        if not claimed_at:
            return "stale"
        try:
            ts = datetime.fromisoformat(claimed_at.replace("Z", "+00:00"))
        except ValueError:
            return "stale"
        age = _now() - ts
        return "claimed" if age < timedelta(minutes=STALE_CLAIM_MINUTES) else "stale"
    return "stale"


def claim_slot(today: str, slot: str, attempts: int = 5) -> tuple[bool, str]:
    """Try to become the single owner of (today, slot).

    Returns (won, reason). Only the caller that gets True may publish.

    The push is the lock. If it is rejected, another runner moved the ref
    first — we re-read the remote and re-evaluate rather than retrying
    blindly, because a blind retry would stack our claim on top of the
    winner's and defeat the whole mechanism.
    """
    for _ in range(attempts):
        remote = fetch_remote_log()
        entry = remote.get(today, {}).get(slot)

        if entry is not None:
            state = _entry_state(entry)
            if state == "completed":
                media = entry if isinstance(entry, str) else entry.get("media_id")
                return False, f"already published ({media})"
            if state == "claimed":
                holder = entry.get("claimed_by") if isinstance(entry, dict) else "?"
                return False, f"claimed by another run ({holder})"
            # stale -> fall through and take it over

        remote.setdefault(today, {})[slot] = {
            "claimed_at": _now().isoformat().replace("+00:00", "Z"),
            "claimed_by": _run_id(),
        }
        with open(LOG_FILE, "w", encoding="utf-8") as f:
            json.dump(remote, f, indent=2, ensure_ascii=False)

        _run(["git", "add", LOG_FILE])
        if _run(["git", "diff", "--staged", "--quiet"]).returncode == 0:
            return False, "nothing to claim (no diff)"

        _run([
            "git", "-c", f"user.email={GIT_EMAIL}", "-c", f"user.name={GIT_NAME}",
            "commit", "-m", f"claim: {slot} {today} [{_run_id()}]",
        ])
        if _run(["git", "push", "origin", "main"]).returncode == 0:
            return True, "claimed"

        # Lost the race — drop our commit and loop to re-read the winner's state.
        _run(["git", "reset", "--hard", "HEAD~1"])

    return False, "could not claim after retries"


def complete_slot(today: str, slot: str, media_id: str, attempts: int = 5) -> bool:
    """Convert our claim into a completed entry and push it.

    Safe to retry: it re-reads the remote each attempt and only ever replaces
    THIS slot's value, so a concurrent writer touching a different slot is
    merged rather than clobbered.
    """
    for _ in range(attempts):
        remote = fetch_remote_log()
        remote.setdefault(today, {})[slot] = media_id
        with open(LOG_FILE, "w", encoding="utf-8") as f:
            json.dump(remote, f, indent=2, ensure_ascii=False)

        _run(["git", "add", LOG_FILE])
        if _run(["git", "diff", "--staged", "--quiet"]).returncode == 0:
            return True  # already recorded

        _run([
            "git", "-c", f"user.email={GIT_EMAIL}", "-c", f"user.name={GIT_NAME}",
            "commit", "-m", f"log: {slot} {today} = {media_id}",
        ])
        if _run(["git", "push", "origin", "main"]).returncode == 0:
            return True
        _run(["git", "reset", "--hard", "HEAD~1"])

    return False


def release_claim(today: str, slot: str) -> None:
    """Give the slot back after a failed publish, so a retry can pick it up
    immediately instead of waiting out the staleness window."""
    for _ in range(3):
        remote = fetch_remote_log()
        entry = remote.get(today, {}).get(slot)
        if not isinstance(entry, dict) or entry.get("media_id"):
            return  # not ours to release, or already completed
        del remote[today][slot]
        if not remote[today]:
            del remote[today]
        with open(LOG_FILE, "w", encoding="utf-8") as f:
            json.dump(remote, f, indent=2, ensure_ascii=False)
        _run(["git", "add", LOG_FILE])
        if _run(["git", "diff", "--staged", "--quiet"]).returncode == 0:
            return
        _run([
            "git", "-c", f"user.email={GIT_EMAIL}", "-c", f"user.name={GIT_NAME}",
            "commit", "-m", f"release: {slot} {today} (publish failed)",
        ])
        if _run(["git", "push", "origin", "main"]).returncode == 0:
            return
        _run(["git", "reset", "--hard", "HEAD~1"])
