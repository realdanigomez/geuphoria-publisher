"""Offline tests for publish_clip.publish_yt_short's hardening (2026-09-25): a fake YouTube client, no network, no git.

  python tests/test_yt_short_hardening.py

Scenarios:
  A. a normal upload that processes              -> recorded, one upload, nothing retired
  B. 409 'already exists' after the upload        -> the video is found by title, waited on, recorded
  C. the first upload sticks in processing        -> retired (private + renamed), re-uploaded ONCE, the 2nd recorded
  D. both uploads stick                           -> RuntimeError, nothing recorded, both retired
"""
import os, sys, tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
import httplib2
from googleapiclient.errors import HttpError
import publish_clip as PC

TITLE = "The DM line that actually gets replies"


class Exec:
    def __init__(self, fn):
        self.fn = fn

    def execute(self):
        return self.fn()


class FakeYT:
    def __init__(self, plan):
        self.plan = plan                  # one entry per upload: "ok" | "stuck" | "409"
        self.uploads, self.updates, self.n = [], [], 0
        self.state = {}                   # video id -> "processed" | "stuck"
        self.titles = {}

    # videos()
    def videos(self):
        return self

    def insert(self, part, body, media_body):
        mode = self.plan[self.n]
        self.n += 1
        vid = f"V{self.n}"
        yt = self

        class Req:
            def next_chunk(self, num_retries=0):
                yt.uploads.append(vid)
                yt.state[vid] = "stuck" if mode == "stuck" else "processed"
                yt.titles[vid] = body["snippet"]["title"]
                if mode == "409":
                    raise HttpError(httplib2.Response({"status": 409}),
                                    b'{"error": {"message": "Requested entity already exists"}}')
                return None, {"id": vid}
        return Req()

    def list(self, part=None, id=None, **kw):
        if "playlistId" in kw:                              # playlistItems().list
            items = [{"snippet": {"title": self.titles[v], "publishedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                                  "resourceId": {"videoId": v}}} for v in reversed(self.uploads)]
            return Exec(lambda: {"items": items})
        if "mine" in kw:                                    # channels().list
            return Exec(lambda: {"items": [{"contentDetails": {"relatedPlaylists": {"uploads": "UU1"}}}]})
        vid = id

        def get():
            st = self.state.get(vid)
            if st == "processed":
                return {"items": [{"status": {"uploadStatus": "processed"}, "processingDetails": {"processingStatus": "succeeded"},
                                   "contentDetails": {"duration": "PT53S"}, "snippet": {"description": "d", "categoryId": "27"}}]}
            return {"items": [{"status": {"uploadStatus": "uploaded"}, "processingDetails": {"processingStatus": "processing"},
                               "contentDetails": {"duration": "P0D"}, "snippet": {"description": "d", "categoryId": "27"}}]}
        return Exec(get)

    def update(self, part, body):
        self.updates.append((body["id"], part, body.get("status", {}).get("privacyStatus"), body.get("snippet", {}).get("title")))
        return Exec(lambda: {})

    def channels(self):
        return self

    def playlistItems(self):
        return self


def run(plan):
    fake = FakeYT(plan)
    recorded = []
    tmp = Path(tempfile.mkdtemp())
    PC.build_youtube = lambda: fake
    PC.download_yt_video = lambda url, dest: Path(dest).write_bytes(b"\x00" * 1024)
    PC.slot_lock.claim_slot = lambda today, key: (True, "test")
    PC.slot_lock.complete_slot = lambda today, key, vid: recorded.append((key, vid)) or True
    PC.time.sleep = lambda s: None
    PC.PROCESS_WAIT_S = 0.05
    PC.ROOT = tmp
    slot = {"name": "curiosity-dm-opener", "yt_cdn_url": "https://example.invalid/x.mp4",
            "yt_caption_path": "cap.txt", "yt_title_path": "title.txt"}
    (tmp / "cap.txt").write_text("line one\n\nThanks for watching!", encoding="utf-8")
    (tmp / "title.txt").write_text(TITLE, encoding="utf-8")
    PC.read_caption_file = lambda rel: (tmp / rel).read_text(encoding="utf-8").strip()
    try:
        out = PC.publish_yt_short("2026-09-25", "longform_clip", slot)
        err = None
    except Exception as e:
        out, err = None, e
    return fake, recorded, out, err


fails = 0


def check(name, cond, detail):
    global fails
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  -> {detail}"))
    fails += not cond


f, rec, out, err = run(["ok"])
check("A normal upload", out == "V1" and rec == [("yt_longform_clip", "V1")] and not f.updates and len(f.uploads) == 1,
      (out, rec, f.updates, err))
f, rec, out, err = run(["409"])
check("B 409 -> found by title", out == "V1" and rec == [("yt_longform_clip", "V1")] and not f.updates, (out, rec, f.updates, err))
f, rec, out, err = run(["stuck", "ok"])
check("C stuck -> retired + one re-upload", out == "V2" and rec == [("yt_longform_clip", "V2")]
      and ("V1", "status", "private", None) in f.updates and ("V1", "snippet", None, PC.RETIRED_TITLE) in f.updates
      and len(f.uploads) == 2, (out, rec, f.updates, err))
f, rec, out, err = run(["stuck", "stuck"])
check("D stuck twice -> error, nothing recorded", out is None and isinstance(err, RuntimeError) and rec == []
      and len(f.uploads) == 2 and sum(1 for u in f.updates if u[2] == "private") == 2, (out, rec, f.updates, err))
sys.exit(1 if fails else 0)
