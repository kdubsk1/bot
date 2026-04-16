# NQ CALLS Bot — Railway Deployment Guide

## What Railway Does
Railway runs your bot 24/7 on a cloud Linux server. No more leaving your
PC on overnight. The watchdog keeps the bot alive, and Railway keeps the
server alive. Costs ~$5/month.

---

## Step 1: Push Code to GitHub

1. Go to https://github.com/new and create a **private** repository
   called `nq-calls-bot` (or whatever you want).

2. Open a terminal **in your Trading bot folder** and run:

   ```
   cd "C:\Users\wayne\Desktop\Trading bot"
   git init
   git add -A
   git commit -m "Initial commit"
   git branch -M main
   git remote add origin https://github.com/YOUR_USERNAME/nq-calls-bot.git
   git push -u origin main
   ```

3. **IMPORTANT**: Before pushing, make sure `.gitignore` exists with at
   least:
   ```
   __pycache__/
   *.pyc
   bot_log.txt
   ```
   The hardcoded token in config.py has a fallback — we will override it
   with environment variables on Railway so the token in the repo is only
   a backup. For extra security, remove the hardcoded values after you
   set up Railway env vars.

---

## Step 2: Create a Railway Project

1. Go to https://railway.app and sign up with your GitHub account.

2. Click **"New Project"** on the dashboard.

3. Choose **"Deploy from GitHub Repo"**.

4. Select your `nq-calls-bot` repository.

5. Railway will detect the `Procfile` and `requirements.txt` automatically.

---

## Step 3: Set Environment Variables

In the Railway dashboard for your service:

1. Click on your service (the purple box).
2. Go to the **"Variables"** tab.
3. Add these two variables:

   | Variable         | Value                                              |
   |------------------|----------------------------------------------------|
   | `TELEGRAM_TOKEN` | `8637758608:AAGIWdgrNhCWUlY-mmADUiAITwoJ3IyBrfQ`  |
   | `CHAT_ID`        | `-1003804686713`                                   |

4. Railway will automatically redeploy with the new variables.

---

## Step 4: Verify the Deploy

1. In Railway, click on your service and go to the **"Logs"** tab.
2. You should see the watchdog starting, then the bot starting.
3. Check Telegram — you should get the startup message from the bot.
4. Send `/menu` in Telegram to confirm the bot responds.

---

## Step 5: Clean Up Local config.py (Optional but Recommended)

After Railway is working, update your local `config.py` to remove the
hardcoded token:

```python
import os

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID        = int(os.environ.get("CHAT_ID", "0"))
```

This way if the repo ever leaks, the token isn't exposed.
You only need the hardcoded values if running locally without env vars.

---

## How It Works

```
Railway Server (Linux)
  |
  +-- watchdog.py          (Procfile says: worker: python watchdog.py)
       |
       +-- bot.py           (watchdog starts this as a subprocess)
            |
            +-- Telegram     (receives/sends messages)
            +-- yfinance     (NQ, GC price data)
            +-- ccxt         (BTC, SOL price data)
```

- Railway runs `watchdog.py` which starts `bot.py`
- If bot.py crashes, watchdog restarts it in 15 seconds
- If it crashes 5 times in 10 minutes, watchdog stops and alerts you
- Railway keeps watchdog.py alive — if even that dies, Railway restarts it

---

## Useful Railway Commands

| Action              | How                                                  |
|---------------------|------------------------------------------------------|
| View logs           | Railway dashboard -> your service -> Logs tab        |
| Restart             | Railway dashboard -> your service -> three dots -> Restart |
| Stop                | Railway dashboard -> your service -> Settings -> Remove |
| Update code         | `git push` — Railway auto-deploys on push            |
| Check usage/billing | Railway dashboard -> Usage tab                       |

---

## Troubleshooting

**Bot doesn't start:**
- Check the Logs tab for errors
- Make sure env vars are set (Variables tab)
- Make sure `requirements.txt` has all packages

**Bot starts but no Telegram messages:**
- Verify TELEGRAM_TOKEN and CHAT_ID in Variables
- Make sure the bot isn't already running locally (two bots with the
  same token will fight each other)

**"Module not found" errors:**
- Add the missing package to `requirements.txt` and push

**Bot works locally but not on Railway:**
- Railway is Linux, not Windows. The `ctypes.windll` calls in watchdog
  are already wrapped in try/except to handle this.
- File paths use forward slashes on Linux — the bot uses `os.path` so
  this is handled automatically.

---

## Cost

Railway free tier: 500 hours/month (about 20 days).
Hobby plan: $5/month for unlimited hours. This is what you want for 24/7.
