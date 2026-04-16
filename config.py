# config.py - NQ CALLS Bot credentials
# ======================================
# On Railway: set TELEGRAM_TOKEN and CHAT_ID as environment variables.
# Locally: the hardcoded fallbacks below still work.

import os

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "8637758608:AAGIWdgrNhCWUlY-mmADUiAITwoJ3IyBrfQ")
CHAT_ID        = int(os.environ.get("CHAT_ID", "-1003804686713"))
