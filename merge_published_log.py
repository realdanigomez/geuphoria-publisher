"""Safely merge a locally-modified published_log.json with origin/main's current
copy — used by every publish workflow's "Save log + push" retry loop, in place
of `git pull --rebase`.

WHY THIS EXISTS (2026-09-01): two publish workflows landing close together (e.g.
publish-reel.yml + publish-yt-reel.yml, both firing at 8AM AST) commit to this
SAME file around the same moment. `git pull --rebase` on a real text conflict
in the JSON left the retry loop stuck in a broken mid-rebase state that
`|| true` silently swallowed — the loop exited on "Nothing to commit" without
ever actually pushing, and a real dedup log entry was lost even though the
underlying post had already gone out. Every entry in this file is purely
additive (new date -> new key -> new value; nothing is ever edited or removed
by a publish script), so a deep-union merge in Python is always safe and can
never lose data the way a line-based git merge can.

Usage: python merge_published_log.py [path]  (default: published_log.json)
Reads the given file (the working tree's copy, already modified by the
publish script's own mark_published() call), reads origin/main's current copy
via `git show`, deep-merges them, and overwrites the local file with the
result — ready for `git add` + `git commit` + `git push`.
"""
import json
import os
import subprocess
import sys


def deep_merge(base: dict, overlay: dict) -> dict:
    result = dict(base)
    for k, v in overlay.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 else "published_log.json"

    local = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            content = f.read().strip()
            if content:
                local = json.loads(content)

    remote = {}
    r = subprocess.run(["git", "show", f"origin/main:{path}"], capture_output=True, text=True)
    if r.returncode == 0 and r.stdout.strip():
        remote = json.loads(r.stdout)

    merged = deep_merge(remote, local)

    with open(path, "w", encoding="utf-8") as f:
        json.dump(merged, f, indent=2, ensure_ascii=False)

    local_dates = len(local)
    remote_dates = len(remote)
    merged_dates = len(merged)
    print(f"[merge_published_log] local={local_dates} remote={remote_dates} merged={merged_dates} dates")
    return 0


if __name__ == "__main__":
    sys.exit(main())
