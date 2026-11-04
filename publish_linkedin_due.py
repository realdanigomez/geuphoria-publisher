"""Mirror every piece on publish_schedule.json to Dani's LinkedIn as it comes due (2026-11-03: "start posting on LinkedIn from today
going forward ... everything that's on the posting calendar"). Runs every 15 minutes from GitHub Actions:
  - a carousel entry (has "folder") -> its slides as ONE PDF document post with its caption.txt
  - a clip entry (has "cdn_url") -> a video post with its caption.txt (the Short's title is the post title)
  - linkedin_links.json -> a link post to each longform's YouTube video 5 minutes after it goes public
Entries are due when their AST slot time has passed; each is posted once (linkedin_log.json); anything older than 48 h is logged as
stale instead of posted, anything before START is never posted. At most 4 posts per run (oldest first). Exit 1 if any post failed.
  python publish_linkedin_due.py [--dry-run]
"""
import datetime as dt, json, os, sys, tempfile
from zoneinfo import ZoneInfo

import publish_linkedin as LI

HERE = os.path.dirname(os.path.abspath(__file__))
TZ = ZoneInfo("America/Curacao")
START = dt.datetime(2026, 11, 4, 0, 0, tzinfo=TZ)
MAX_RUN, STALE = 4, dt.timedelta(hours=48)
dry = "--dry-run" in sys.argv


def jload(name, default):
    p = os.path.join(HERE, name)
    return json.load(open(p, encoding="utf-8")) if os.path.exists(p) else default


sched, links, log = jload("publish_schedule.json", {}), jload("linkedin_links.json", []), jload("linkedin_log.json", {})
now = dt.datetime.now(TZ)
due = []
for day, slots in sched.items():
    if not (isinstance(slots, dict) and day[:2] == "20"):
        continue
    for slot, e in slots.items():
        if not (isinstance(e, dict) and e.get("slot_time_ast")):
            continue
        t = dt.datetime.strptime(day + " " + e["slot_time_ast"].replace(" AST", "").strip(), "%Y-%m-%d %I:%M %p").replace(tzinfo=TZ)
        due.append((t, f"{day}|{slot}|{e['name']}", "carousel" if e.get("folder") else "clip", e))
for l in links:
    due.append((dt.datetime.fromisoformat(l["at"]).replace(tzinfo=TZ), l["key"], "link", l))
due.sort(key=lambda d: d[0])
posted = failed = 0
for t, key, kind, e in due:
    if t > now or t < START or log.get(key, {}).get("status") in ("posted", "stale", "gave_up"):
        continue
    if now - t > STALE:
        log[key] = {"status": "stale", "at": now.isoformat()}
        print("stale, not posted:", key); continue
    if posted >= MAX_RUN:
        print("run cap reached; the rest next run"); break
    try:
        print("posting", kind, key, flush=True)
        if kind == "carousel":
            text = open(os.path.join(HERE, e["caption_path"]), encoding="utf-8").read()
            title = text.strip().splitlines()[0][:200]
            pdf = LI.pdf_from_slides(os.path.join(HERE, "carousels", e["folder"]))
            urn = "dry" if dry else LI.upload_document(pdf)
            res = LI.create_post(LI.post_body(text, media={"title": title, "id": urn}), dry)
        elif kind == "clip":
            text = open(os.path.join(HERE, e["caption_path"]), encoding="utf-8").read()
            title = open(os.path.join(HERE, e["yt_title_path"]), encoding="utf-8").read().strip() if e.get("yt_title_path") else text.splitlines()[0]
            urn = "dry" if dry else LI.upload_video(e["cdn_url"])
            res = LI.create_post(LI.post_body(text, media={"title": title[:200], "id": urn}), dry)
        else:
            res = LI.create_post(LI.post_body(e["text"], article={"source": e["url"], "title": e["title"][:200]}), dry)
        if not dry:
            log[key] = {"status": "posted", "urn": res, "at": now.isoformat()}
        posted += 1
    except SystemExit as ex:
        failed += 1
        n = log.get(key, {}).get("tries", 0) + 1       # a failed entry is retried next run, up to 3 tries
        log[key] = {"status": "failed" if n < 3 else "gave_up", "tries": n, "error": str(ex)[:300], "at": now.isoformat()}
        print("FAILED", key, ex, flush=True)
if not dry:
    json.dump(log, open(os.path.join(HERE, "linkedin_log.json"), "w", encoding="utf-8"), indent=1, ensure_ascii=False)
print(f"done: {posted} posted, {failed} failed, {sum(1 for t, k, *_ in due if START <= t <= now and log.get(k, {}).get("status") not in ("posted", "stale", "gave_up"))} still due")
sys.exit(1 if failed else 0)
