"""
Railway notification setup — run this ONCE after `railway login`.

Usage:
    1. Open a terminal in this folder
    2. Run: railway login
    3. Run: python setup_railway_notifications.py
    4. It sets VAPID keys + NOTIFY_API_TOKEN in Railway, then runs sync --schema via railway run

Generated 2026-05-05. Re-run if VAPID keys are ever rotated.
"""
import subprocess
import sys

# ── Values generated 2026-05-05 ───────────────────────────────────────────────
VAPID_PUBLIC_KEY = "BO5sakfoMPXkWoIgH9ME8UIIGmVUGdWvXrfl7AfVNS_rV1db1x90pyucRCs0LB2Z1clSMxPOIIk16mgOIbXdk3Y"

VAPID_PRIVATE_KEY = (
    "LS0tLS1CRUdJTiBFQyBQUklWQVRFIEtFWS0tLS0tCk1IY0NBUUVFSU1JQnZTZ1ZiaitHM2JPUWNGU0xu"
    "Wi82Mzc0b1hsTCt2R3VQSzE2SVhwbXJvQW9HQ0NxR1NNNDkKQXdFSG9VUURRZ0FFN214cVIrZ3c5ZVJh"
    "Z2lBZjB3VHhRZ2dhWlZRWjFhOWV0K1hzQjlVMUwrdFhWMXZYSDNTbgpLNXhFS3pRc0hablZ5Vkl6RTg0"
    "Z2lUWHFhQTRodGQyVGRnPT0KLS0tLS1FTkQgRUMgUFJJVkFURSBLRVktLS0tLQo"
)

NOTIFY_API_TOKEN = "StY8JhlMamB3r8JgApXIB3kwMamg_C4wtUSzl6RDe3Dut4HTS4d9ZIgWCJAUO-8y"


def run(cmd, check=True):
    print(f"  $ {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.stdout.strip():
        print(f"    {result.stdout.strip()}")
    if result.returncode != 0:
        print(f"  ERROR (rc={result.returncode}): {result.stderr.strip()}")
        if check:
            sys.exit(1)
    return result


def main():
    print("=" * 60)
    print("  Railway Notification Setup — Geuphoria Dashboard")
    print("=" * 60)

    # Check railway CLI is logged in
    r = run(["railway", "whoami"], check=False)
    if r.returncode != 0:
        print("\nNot logged in. Run: railway login\nThen re-run this script.")
        sys.exit(1)

    print(f"\n[1/4] Setting VAPID_PUBLIC_KEY ...")
    run(["railway", "variables", "set", f"VAPID_PUBLIC_KEY={VAPID_PUBLIC_KEY}"])

    print(f"\n[2/4] Setting VAPID_PRIVATE_KEY ...")
    run(["railway", "variables", "set", f"VAPID_PRIVATE_KEY={VAPID_PRIVATE_KEY}"])

    print(f"\n[3/4] Setting NOTIFY_API_TOKEN ...")
    run(["railway", "variables", "set", f"NOTIFY_API_TOKEN={NOTIFY_API_TOKEN}"])

    print(f"\n[4/4] Applying schema (CREATE TABLE IF NOT EXISTS — safe to re-run) ...")
    # Run sync.py --schema via railway run so it uses the Railway DATABASE_URL
    run(["railway", "run", "python", "sync.py", "--schema"])

    print("\n" + "=" * 60)
    print("  DONE. Railway env vars set + notifications schema applied.")
    print()
    print("  GitHub Actions secrets already set:")
    print("    DASHBOARD_NOTIFY_URL  = https://media.geuphoria.com")
    print("    DASHBOARD_NOTIFY_TOKEN = (matches NOTIFY_API_TOKEN above)")
    print()
    print("  Final step — subscribe your phone:")
    print("    1. Open https://media.geuphoria.com/notifications on your phone")
    print("    2. Log in, then tap 'Enable Notifications'")
    print("    3. iOS: first tap Share → Add to Home Screen, then open from there")
    print("    4. Tap 'Send test push' to confirm end-to-end works")
    print("=" * 60)


if __name__ == "__main__":
    main()
