# Audition Radar

Daily casting scanner → classify (MATCH / NEEDS REVIEW / REJECTED) → email digest → GitHub Pages dashboard.
Stack: Python + GitHub Actions (cron) + Apify (Instagram) + Tesseract OCR (English+Malayalam) + Gmail SMTP + static dashboard.

## Why this architecture (research, Oct 2026)
- Instaloader / direct `web_profile_info` calls from cloud IPs: HTTP 401/400 is now common even for public profiles; a June 2026 report says the public GraphQL path broke on 2026‑06‑29 and Instagram is moving profile data into inline page payloads. GitHub Actions uses datacenter IPs, which are throttled first. **Not used** (your earlier failure matches this).
- Needs-login routes (instagrapi, cookies): excluded — you asked not to use personal credentials.
- **Primary: Apify `apify/instagram-scraper`** (managed proxies, no login). Free plan = $5/month credit, no card (verified on several July 2026 pricing pages; re-check apify.com/pricing). Your volume (3 accounts, 3‑day window) should be a small fraction of $5, but check the first run's cost in the Apify console.
- **Fallback: search-engine discovery** (DuckDuckGo HTML) for Instagram/Facebook/web posts. Gives only title/snippet, no poster or date, so results are mostly NEEDS REVIEW. It may be blocked from datacenter IPs; the app does not bypass CAPTCHAs.
- Limits of the PoC: I could not run live Instagram/Apify calls from my sandbox (network allow-list), so **live scraping is unverified until you run step 6 below.** Classification, dedupe, storage and dashboard were tested offline on sample data.

## Privacy
DOB lives only in the `CHILD_DOB` GitHub secret; age is computed in memory and never written. `config.yaml` (age range/gender) is visible if the repo is public — GitHub Pages on free plans needs a public repo. If that bothers you, keep criteria generic. Tracking statuses are stored in your browser only (localStorage), per device.

## Setup (Mac)
```bash
brew install git gh && gh auth login
cd ~/Downloads/audition-radar          # unzip location
git init -b main && git add . && git commit -m "init"
gh repo create audition-radar --public --source=. --push
```
1. **Apify**: sign up free → Settings → Integrations → copy API token.
2. **Gmail app password**: Google Account → Security → 2‑Step Verification on → App passwords → create.
3. **Secrets** (repo → Settings → Secrets and variables → Actions), or via CLI:
```bash
gh secret set APIFY_TOKEN; gh secret set CHILD_DOB   # YYYY-MM-DD
gh secret set SMTP_USER; gh secret set SMTP_PASS; gh secret set EMAIL_TO
```
4. **Pages**: Settings → Pages → Deploy from branch → `main` / `/docs`. URL: `https://USERNAME.github.io/audition-radar/`.
5. **Actions**: Settings → Actions → General → Workflow permissions → Read and write.
6. **Run once manually**: `gh workflow run scan -f lookback_days=7` then `gh run watch`.

## Verify
- **Offline sample**: `pip install -r requirements.txt && CHILD_DOB=2024-01-01 python radar.py --sample` then `open docs/index.html` via `cd docs && python3 -m http.server`. (Delete `data/db.json` afterwards.)
- **Live scraping**: Actions log shows `instagram ok N` and handles with data; dashboard overview shows per-source status. `health: ok` with 0 new = successful empty scan; `failed`/`degraded` = collection problem (listed in red, and emailed once a day).
- **Email**: needs a MATCH or NEEDS REVIEW not yet notified. To test, set the secrets and run the sample with SMTP vars exported; clear `data/db.json` to re-send.
- **Schedule**: cron is `30 12 * * *` UTC = 6 PM IST. GitHub may delay scheduled runs (sometimes 15–60 min) and disables schedules after 60 days of repo inactivity; the bot's daily commits normally count as activity. Check Actions tab → scan runs.

## Config
`config.yaml`: sources (add/remove handles, search queries, `web_pages`), keywords (English + Malayalam), lookback, retention (45 days), include Needs Review in email. Manual run accepts `lookback_days`.

## Troubleshooting
- `APIFY_TOKEN not set` / 402 / 403 → secret missing or monthly credit exhausted (free plan blocks until next cycle).
- `search engine blocked` → expected sometimes; Instagram path is primary.
- Gmail `535` → use an app password, not your login password.
- Everything NEEDS REVIEW → OCR missing/age unreadable; check Actions step 'apt-get' succeeded.
- Push race errors → the workflow uses `concurrency` and rebase; re-run.

## Limitations
No scraper is guaranteed. Apify's actor can break when Instagram changes (swap `apify_actor` in config). Rule-based classification is conservative and will miss unusual phrasing (check Needs Review/Rejected tabs weekly). Malayalam OCR on stylised posters is imperfect. Facebook is only reachable via search snippets. Poster URLs on Instagram's CDN expire, so posters are downloaded to `docs/posters/`.
