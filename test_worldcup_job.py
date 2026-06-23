"""
Test worldcup_morning job manually.
Run on server: docker exec moni-bot python test_worldcup_job.py
"""
import asyncio
import os
import sys
from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, os.path.dirname(__file__))

from worldcup import fetch_worldcup_scores


async def main():
    print("=== Fetching World Cup scores ===")
    text = await fetch_worldcup_scores()
    print(repr(text))
    print()
    print("--- Rendered ---")
    print(text)
    print()

    if text and text.startswith("⚽ *"):
        print("✅ morning job WOULD send this message")
    else:
        print("⚠️  morning job would SKIP (no match data)")

    # Also test sending via bot if BOT_TOKEN + REMINDER_USERS set
    bot_token = os.getenv("BOT_TOKEN")
    reminder_users = os.getenv("REMINDER_USERS", "")
    allowed_users = os.getenv("ALLOWED_USERS", "")
    targets_str = reminder_users or allowed_users

    if bot_token and targets_str and text and text.startswith("⚽ *"):
        targets = [int(u.strip()) for u in targets_str.split(",") if u.strip()]
        import httpx
        print(f"\n--- Sending to {targets} via Bot API ---")
        async with httpx.AsyncClient() as client:
            for uid in targets:
                resp = await client.post(
                    f"https://api.telegram.org/bot{bot_token}/sendMessage",
                    json={"chat_id": uid, "text": text, "parse_mode": "Markdown"},
                )
                print(f"  user {uid}: {resp.status_code} {resp.json().get('description', 'ok')}")


asyncio.run(main())
