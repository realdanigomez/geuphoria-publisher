"""LinkedIn publisher for Dani's profile (2026-11-03). The same finished pieces that post on Instagram / YouTube:
  python publish_linkedin.py carousel <slides_dir> <caption.txt> [--title T] [--dry-run]    slides -> one PDF -> a document post
  python publish_linkedin.py clip <mp4 path or url> <caption.txt> [--title T] [--dry-run]   a Short -> a video post
  python publish_linkedin.py link <url> <text file> [--dry-run]                              a link post (a longform's YouTube URL)
Env: LINKEDIN_ACCESS_TOKEN, LINKEDIN_PERSON_ID (local: .env via config_loader; Actions: secrets). Scopes: w_member_social.
The commentary uses LinkedIn's "little text" format, so reserved characters are escaped. The token lasts ~60 days (re-run
_system/tools/linkedin/linkedin_auth.py). Prints the new post URN on success; exit 1 on any API error."""
import io, json, os, sys, tempfile, time, urllib.request

import requests

API = "https://api.linkedin.com"
VERSIONS = [v for v in (os.environ.get("LINKEDIN_VERSION"), "202604", "202601", "202511", "202510") if v]


def creds():
    tok, pid = os.environ.get("LINKEDIN_ACCESS_TOKEN"), os.environ.get("LINKEDIN_PERSON_ID")
    if not (tok and pid):
        sys.path.insert(0, r"C:\Users\Dani Gomez\Documents\0. Content Team")
        from config_loader import get_secret
        tok, pid = get_secret("LINKEDIN_ACCESS_TOKEN"), get_secret("LINKEDIN_PERSON_ID")
    return tok, "urn:li:person:" + pid


TOKEN, AUTHOR = creds()
_ver = {"v": None}


def escape(text):
    """little text format: these characters are reserved and must be backslash-escaped"""
    out = []
    for ch in text:
        out.append("\\" + ch if ch in "\\|{}@[]()<>#*_~" else ch)
    return "".join(out)


def call(method, path, **kw):
    """one LinkedIn call; walks the version list on 426 (NONEXISTENT_VERSION) once, then sticks to the one that works"""
    for v in ([_ver["v"]] if _ver["v"] else VERSIONS):
        h = {"Authorization": "Bearer " + TOKEN, "LinkedIn-Version": v, "X-Restli-Protocol-Version": "2.0.0"}
        r = requests.request(method, API + path, headers={**h, **kw.get("headers", {})}, **{k: x for k, x in kw.items() if k != "headers"}, timeout=300)
        if r.status_code == 426:
            continue
        _ver["v"] = v
        return r
    raise SystemExit("no LinkedIn-Version accepted: " + ", ".join(VERSIONS))


def post_body(commentary, media=None, article=None):
    body = {"author": AUTHOR, "commentary": escape(commentary.strip()), "visibility": "PUBLIC",
            "distribution": {"feedDistribution": "MAIN_FEED", "targetEntities": [], "thirdPartyDistributionChannels": []},
            "lifecycleState": "PUBLISHED", "isReshareDisabledByAuthor": False}
    if media:
        body["content"] = {"media": media}
    if article:
        body["content"] = {"article": article}
    return body


def create_post(body, dry):
    if dry:
        print("[dry-run] would POST /rest/posts:", json.dumps(body)[:400]); return "dry-run"
    r = call("POST", "/rest/posts", json=body)
    if r.status_code not in (200, 201):
        raise SystemExit(f"post failed: HTTP {r.status_code} {r.text[:500]}")
    urn = r.headers.get("x-restli-id") or r.headers.get("X-RestLi-Id")
    print("POSTED", urn, "https://www.linkedin.com/feed/update/" + str(urn))
    return urn


def pdf_from_slides(folder):
    from PIL import Image
    import glob
    files = sorted(glob.glob(os.path.join(folder, "slide-*.png")))
    assert files, "no slides in " + folder
    imgs = [Image.open(f).convert("RGB") for f in files]
    out = os.path.join(tempfile.gettempdir(), "li-" + os.path.basename(os.path.normpath(folder)) + ".pdf")
    imgs[0].save(out, "PDF", save_all=True, append_images=imgs[1:], resolution=100.0)
    return out


def upload_document(pdf):
    r = call("POST", "/rest/documents?action=initializeUpload", json={"initializeUploadRequest": {"owner": AUTHOR}})
    if r.status_code != 200:
        raise SystemExit(f"document init failed: HTTP {r.status_code} {r.text[:400]}")
    v = r.json()["value"]
    u = requests.put(v["uploadUrl"], data=open(pdf, "rb").read(), headers={"Authorization": "Bearer " + TOKEN, "Content-Type": "application/octet-stream"}, timeout=300)
    if u.status_code not in (200, 201):
        raise SystemExit(f"document upload failed: HTTP {u.status_code} {u.text[:300]}")
    return v["document"]


def upload_video(path_or_url):
    if path_or_url.startswith("http"):
        path = os.path.join(tempfile.gettempdir(), "li-clip.mp4")
        with requests.get(path_or_url, stream=True, timeout=300) as g, open(path, "wb") as f:
            g.raise_for_status()
            for chunk in g.iter_content(1 << 20):
                f.write(chunk)
    else:
        path = path_or_url
    size = os.path.getsize(path)
    r = call("POST", "/rest/videos?action=initializeUpload", json={"initializeUploadRequest": {"owner": AUTHOR, "fileSizeBytes": size, "uploadCaptions": False, "uploadThumbnail": False}})
    if r.status_code != 200:
        raise SystemExit(f"video init failed: HTTP {r.status_code} {r.text[:400]}")
    v = r.json()["value"]
    etags = []
    with open(path, "rb") as f:
        for ins in v["uploadInstructions"]:
            f.seek(ins["firstByte"])
            chunk = f.read(ins["lastByte"] - ins["firstByte"] + 1)
            u = requests.put(ins["uploadUrl"], data=chunk, headers={"Content-Type": "application/octet-stream"}, timeout=600)
            if u.status_code not in (200, 201):
                raise SystemExit(f"video part failed: HTTP {u.status_code} {u.text[:300]}")
            etags.append(u.headers.get("etag", "").strip('"'))
    f = call("POST", "/rest/videos?action=finalizeUpload", json={"finalizeUploadRequest": {"video": v["video"], "uploadToken": v.get("uploadToken", ""), "uploadedPartIds": etags}})
    if f.status_code not in (200, 201):
        raise SystemExit(f"video finalize failed: HTTP {f.status_code} {f.text[:300]}")
    for _ in range(60):                       # wait until LinkedIn has processed it
        s = call("GET", "/rest/videos/" + requests.utils.quote(v["video"], safe=""))
        if s.status_code == 200 and s.json().get("status") == "AVAILABLE":
            break
        time.sleep(5)
    return v["video"]


if __name__ == "__main__":
    kind = sys.argv[1]
    dry = "--dry-run" in sys.argv
    args = [a for a in sys.argv[2:] if not a.startswith("--")]
    title = sys.argv[sys.argv.index("--title") + 1] if "--title" in sys.argv else None
    if title and title in args:
        args.remove(title)
    if kind == "carousel":
        folder, cap = args[0], args[1]
        text = open(cap, encoding="utf-8").read()
        title = title or text.strip().splitlines()[0][:200]
        pdf = pdf_from_slides(folder)
        print("pdf:", pdf, os.path.getsize(pdf) // 1024, "KB")
        urn = "urn:li:document:dry" if dry else upload_document(pdf)
        create_post(post_body(text, media={"title": title, "id": urn}), dry)
    elif kind == "clip":
        mp4, cap = args[0], args[1]
        text = open(cap, encoding="utf-8").read()
        title = title or text.strip().splitlines()[0][:200]
        urn = "urn:li:video:dry" if dry else upload_video(mp4)
        create_post(post_body(text, media={"title": title, "id": urn}), dry)
    elif kind == "link":
        url, cap = args[0], args[1]
        create_post(post_body(open(cap, encoding="utf-8").read(), article={"source": url, "title": title or url}), dry)
    else:
        sys.exit(__doc__)
