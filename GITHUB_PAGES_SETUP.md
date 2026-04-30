# GitHub Pages Setup — Live Dashboard URL

Goal: have your dashboard accessible at a public URL like
`kdubsk1.github.io/bot` that updates automatically.

This works because GitHub auto-publishes the `gh-pages` branch (or any
branch you choose) as a static website. You can put dashboard.html
there and it becomes a live URL.

---

## ONE-TIME SETUP (5 minutes)

### Step 1: Enable GitHub Pages

1. Go to https://github.com/kdubsk1/bot/settings/pages
2. Under "Build and deployment":
   - Source: **Deploy from a branch**
   - Branch: **main** / **/(root)**
   - (We'll change this to `/docs` folder in step 2)
3. Click **Save**

### Step 2: Move dashboard into a /docs folder

GitHub Pages can serve from `/docs` so the rest of the repo stays clean.

In your Trading bot folder, create a `docs/` directory and tell
generate_dashboard.py to write there too. Already done — see `OUTPUT_DOCS_PATH`
inside `generate_dashboard.py`.

### Step 3: Re-run generate_dashboard.py and push

```
python generate_dashboard.py
git add docs/dashboard.html
git commit -m "Add live dashboard"
git push
```

Or use your existing `auto_sync` system — it will commit `docs/dashboard.html`
on its 6-hour schedule.

### Step 4: Visit your live URL

Within 1-2 minutes after the push:
**https://kdubsk1.github.io/bot/dashboard.html**

GitHub will email you when it's published.

---

## HOW IT STAYS LIVE

The bot runs `auto_sync.py` every 6 hours, which commits the latest data
files (including `data/sim_account.json`, `outcomes.csv`, etc.) to GitHub.

After the auto-sync push, you (or a server-side hook) needs to regenerate
`docs/dashboard.html` from the fresh data. Since `auto_sync` runs on Railway
where Python is available, we can wire it to also regenerate the dashboard
on each sync.

**TODO (next session):** modify `auto_sync.py` to call `generate_dashboard.main()`
right before pushing so the dashboard.html gets pushed too. That way, every
6 hours your public URL updates automatically.

For now, you have two options:

**Option A (manual, simple):** Run `python generate_dashboard.py`
on your PC whenever you want to refresh the live URL, then push to GitHub.

**Option B (local always-fresh):** Run `auto_refresh_dashboard.py` (or
double-click `AUTO_REFRESH_DASHBOARD.bat`) — it regenerates every 5 min.
Then `git push` whenever. The local file is always fresh; the public
URL updates only when you push.

---

## TROUBLESHOOTING

**"404 Not Found"** — GitHub Pages may take 1-2 min after push.
Check https://github.com/kdubsk1/bot/actions for build status.

**"Site is private"** — Pages only works on public repos for free
accounts. Verify your repo is public, OR use a paid plan.

**Data not updating** — Make sure auto_sync is committing data files
AND that you're regenerating dashboard.html before each push.
Check your most recent commit: it should include `docs/dashboard.html`.

---

## WHAT THIS GETS YOU

- 🌐 Public URL: `https://kdubsk1.github.io/bot/dashboard.html`
- 📊 Same dashboard as local, but accessible from any device
- 💵 Free (GitHub Pages doesn't cost anything)
- 🔄 Auto-refreshes every 60 sec in the browser
- 👀 Share with friends / show off your bot

When you have a real subscription product, you'll move to a real domain
+ paid hosting. For now this is perfect.
