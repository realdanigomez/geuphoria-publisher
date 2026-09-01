# External cron setup — the Railway replacement (2026-09-01)

Why this exists: on 2026-09-01, GitHub's own scheduler (both the safety net's
`*/5 * * * *` cron AND the noon carousel's own cron) went quiet for about 17
minutes with nothing else happening to wake the safety net's other trigger
paths, and the day's carousel was missed until caught manually. `cron-safety-net.yml`
now also listens for a `repository_dispatch` event (`external-heartbeat`) —
this doc is the ~5 minute setup for the free external service that sends it,
completely independent of GitHub's own infrastructure. This is a straight
replacement for what Railway used to provide.

Two steps. Both need to happen in your own browser — account creation and
personal-access-token creation both require you to be signed in and to click
through GitHub's own confirmation screens, so I can't do either of these for you.

## Step 1 — Create a scoped GitHub token (2 min)

This token can only do ONE thing: trigger a `repository_dispatch` on this one
repo. It can't touch any other repo, can't change settings, can't see your
other repos at all.

1. Go to **https://github.com/settings/personal-access-tokens/new**
2. **Token name:** `geuphoria-external-cron`
3. **Expiration:** pick "No expiration" (or the longest option offered — if it
   expires, the external cron silently stops working until you notice and
   renew it, so longer is better here)
4. **Repository access:** "Only select repositories" → choose
   `realdanigomez/geuphoria-publisher`
5. **Permissions** → Repository permissions → set:
   - **Contents:** Read and write
   - **Actions:** Read and write
   (leave everything else as "No access")
6. Click **Generate token**
7. **Copy the token now** (starts with `github_pat_...`) — GitHub only shows
   it once. Paste it somewhere safe for a moment; you'll need it in Step 2.

## Step 2 — Set up the free cron at cron-job.org (3 min)

1. Go to **https://cron-job.org** and sign up (free, no credit card)
2. Once logged in, click **CREATE CRONJOB**
3. Fill in:
   - **Title:** `geuphoria safety net heartbeat`
   - **URL:** `https://api.github.com/repos/realdanigomez/geuphoria-publisher/dispatches`
   - **Request method:** `POST`
4. Under **Advanced** (or "Common expressions" for schedule):
   - **Schedule:** every 5 minutes (cron-job.org's UI usually has this as a
     preset; if setting it manually the cron expression is `*/5 * * * *`)
5. Still under **Advanced**, find **Request headers** and add exactly these
   three (as separate header rows, not one line):
   - `Authorization` → `Bearer <paste the github_pat_... token from Step 1>`
   - `Accept` → `application/vnd.github+json`
   - `Content-Type` → `application/json`
6. Find **Request body** (may also be under Advanced) and set it to exactly:
   ```json
   {"event_type": "external-heartbeat"}
   ```
7. Save the cron job.

## Verify it worked

cron-job.org has a "Run now" / "Test run" button on the job — use it once.
Then check that it actually reached GitHub:

```bash
gh run list --workflow="cron-safety-net.yml" --limit 3
```

You should see a new run with trigger type `repository_dispatch` (a few
seconds after you click test run). If cron-job.org's test shows a `204`
response, that's success — GitHub's dispatches endpoint returns no body on
success.

## If it ever needs replacing

Any service that can POST a JSON body with custom headers on a schedule works
the same way — cron-job.org was picked for being free, purpose-built, and not
requiring a credit card, not because it's the only option. UptimeRobot (free
tier, repurposed as a pinger) and Google Apps Script time-driven triggers
(if you're already using a Google account for this project's YouTube access)
are both reasonable alternatives with the same POST target and body.

## If you ever want to remove it

Delete the cron job at cron-job.org, and optionally revoke the token at
**https://github.com/settings/personal-access-tokens** — this doesn't break
anything else; the safety net still has its other 5 trigger sources.
