"""One-time interactive Telegram login (creates the session file the worker
reuses). Run once on the machine that will run the worker:
    python -m scripts.telegram_login
"""
import sys, os, asyncio
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from telethon import TelegramClient
from app.config import TELEGRAM_API_ID, TELEGRAM_API_HASH, TELEGRAM_SESSION

async def main():
    client = TelegramClient(TELEGRAM_SESSION, int(TELEGRAM_API_ID), TELEGRAM_API_HASH)
    await client.start()   # prompts for phone number + code interactively
    me = await client.get_me()
    print("Logged in as", me.username or me.phone)
    await client.disconnect()

if __name__ == "__main__":
    asyncio.run(main())
