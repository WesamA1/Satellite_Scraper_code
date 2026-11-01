"""Phase 1 Task 8: optional finish/crash notification via Discord webhook or ntfy.

No-ops silently if neither is configured in .env -- notification is a nice-to-have,
never something that should block or crash the pipeline itself.
"""

import os

import requests
from dotenv import load_dotenv

load_dotenv()


def notify(message: str, post_fn=requests.post) -> bool:
    """Send message to whichever of DISCORD_WEBHOOK_URL / NTFY_URL is configured.

    Returns True if a notification was actually sent. Never raises -- a failed
    notification is logged to stdout, not propagated.
    """
    discord_url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    ntfy_url = os.environ.get("NTFY_URL", "").strip()

    if not discord_url and not ntfy_url:
        return False

    try:
        if discord_url:
            post_fn(discord_url, json={"content": message}, timeout=10)
        if ntfy_url:
            post_fn(ntfy_url, data=message.encode("utf-8"), timeout=10)
        return True
    except Exception as e:
        print(f"[notify] failed to send notification: {e}")
        return False
