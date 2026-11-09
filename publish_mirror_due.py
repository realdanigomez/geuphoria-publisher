"""Post Dani's mirror-account variants to his 4 extra Instagram accounts as each one comes due (his ask 2026-11-07,
sample approved 2026-11-09). Runs every 15 minutes from GitHub Actions (publish-mirror.yml).

mirror_schedule.json = a list of entries, each written by _system/tools/mirror-kit (never by hand):
  {"key": "2026-11-13|real.danigomez_1|ep9-c6", "account": "real.danigomez_1", "ig_user_id": "1784...",
   "at_ast": "2026-11-13 17:38", "type": "reel", "video_url": "https://github.com/.../releases/download/...mp4",
   "caption_path": "mirror/captions/<key-slug>.txt"}
  carousels have "type": "carousel" and "folder": "mirror/carousels/<account>/<slug>" (slide-NN.png in the repo).

Rules: an entry posts once (mirror_log.json); never before its time; never more than 1 post per account per run;
an entry more than 6 h late is logged "stale" (a post hours off its random time is not posted in a burst);
a failure retries next run, up to 3 tries. Same System User token as the main account (IG_ACCESS_TOKEN).
  python publish_mirror_due.py [--dry-run | --check]
--check only reads each account's username (proves the token reaches all 4), posts nothing.
"""
import datetime as dt, json, os, sys, time
from zoneinfo import ZoneInfo

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
API = "https://graph.facebook.com/v21.0"
RAW = "https://raw.githubusercontent.com/realdanigomez/geuphoria-publisher/main"
TZ = ZoneInfo("America/Curacao")
STALE = dt.timedelta(hours=6)
TOKEN = os.environ.get("IG_ACCESS_TOKEN", "")
dry, check = "--dry-run" in sys.argv, "--check" in sys.argv


def jload(name, default):
    p = os.path.join(HERE, name)
    return json.load(open(p, encoding="utf-8")) if os.path.exists(p) else default


def api(method, path, **data):
    r = getattr(requests, method)(f"{API}/{path}", **({"params": data} if method == "get" else {"data": data}),
                                  timeout=60)
    j = r.json()
    if "error" in j:
        raise RuntimeError(f"{path}: {j['error'].get('message')}")
    return j


def wait_ready(cid):
    for _ in range(36):
        time.sleep(10)
        code = api("get", cid, fields="status_code", access_token=TOKEN).get("status_code")
        if code == "FINISHED":
            return
        if code == "ERROR":
            raise RuntimeError(f"Instagram could not process container {cid}")
    raise RuntimeError(f"container {cid} not ready after 6 minutes")


def post_reel(uid, url, caption):
    cid = api("post", f"{uid}/media", media_type="REELS", video_url=url, caption=caption, access_token=TOKEN)["id"]
    wait_ready(cid)
    return api("post", f"{uid}/media_publish", creation_id=cid, access_token=TOKEN)["id"]


def post_carousel(uid, folder, caption):
    slides = sorted(f for f in os.listdir(os.path.join(HERE, folder)) if f.startswith("slide-") and f.endswith(".png"))
    if not 2 <= len(slides) <= 10:
        raise RuntimeError(f"{folder}: {len(slides)} slides (Instagram takes 2 to 10)")
    kids = [api("post", f"{uid}/media", image_url=f"{RAW}/{folder}/{s}", is_carousel_item="true",
                access_token=TOKEN)["id"] for s in slides]
    cid = api("post", f"{uid}/media", media_type="CAROUSEL", children=",".join(kids), caption=caption,
              access_token=TOKEN)["id"]
    wait_ready(cid)
    return api("post", f"{uid}/media_publish", creation_id=cid, access_token=TOKEN)["id"]


def main():
    accounts = jload("mirror_accounts.json", {})
    if check:
        for h, uid in accounts.items():
            print(h, "->", api("get", uid, fields="username", access_token=TOKEN).get("username"))
        return 0
    sched, log = jload("mirror_schedule.json", []), jload("mirror_log.json", {})
    now = dt.datetime.now(TZ)
    done_accounts, posted, failed = set(), 0, 0
    for e in sorted(sched, key=lambda e: e["at_ast"]):
        key = e["key"]
        t = dt.datetime.strptime(e["at_ast"], "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
        if t > now or log.get(key, {}).get("status") in ("posted", "stale", "gave_up"):
            continue
        if e["account"] in done_accounts:
            continue                                   # one post per account per run
        if accounts.get(e["account"]) != e["ig_user_id"]:
            raise SystemExit(f"{key}: account id does not match mirror_accounts.json, refusing to post")
        if now - t > STALE:
            log[key] = {"status": "stale", "at": now.isoformat()}
            print("stale, not posted:", key)
            continue
        caption = open(os.path.join(HERE, e["caption_path"]), encoding="utf-8").read().strip()
        print("posting", e["type"], key, flush=True)
        done_accounts.add(e["account"])
        if dry:
            posted += 1
            continue
        try:
            mid = (post_reel(e["ig_user_id"], e["video_url"], caption) if e["type"] == "reel"
                   else post_carousel(e["ig_user_id"], e["folder"], caption))
            log[key] = {"status": "posted", "media_id": mid, "at": now.isoformat()}
            posted += 1
        except Exception as ex:  # noqa: BLE001 - logged and retried
            failed += 1
            n = log.get(key, {}).get("tries", 0) + 1
            log[key] = {"status": "failed" if n < 3 else "gave_up", "tries": n, "error": str(ex)[:300],
                        "at": now.isoformat()}
            print("FAILED", key, ex, flush=True)
    if not dry:
        json.dump(log, open(os.path.join(HERE, "mirror_log.json"), "w", encoding="utf-8"), indent=1)
    print(f"done: {posted} posted, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
