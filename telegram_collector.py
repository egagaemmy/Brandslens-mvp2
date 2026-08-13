"""Telegram channel monitoring (Telethon / MTProto) — zero approval process.
Register credentials once at my.telegram.org (instant), log in interactively
one time (scripts/telegram_login.py), then this reads public channels in real
time for free. This is where fraud rings promising "guaranteed returns" or
fake allotment desks actually operate — genuinely high-value coverage."""
from __future__ import annotations
import logging
from datetime import timezone
from ..config import TELEGRAM_API_ID, TELEGRAM_API_HASH, TELEGRAM_SESSION

log = logging.getLogger("collector.telegram")
_buffer: dict[str, list[dict]] = {}

def drain(workspace_id: str) -> list[dict]:
    return _buffer.pop(workspace_id, [])

async def start(channel_map: dict[str, list[str]]) -> None:
    """channel_map: workspace_id -> [public channel usernames to watch]."""
    if not (TELEGRAM_API_ID and TELEGRAM_API_HASH):
        log.info("Telegram not configured — channel monitoring disabled (set TELEGRAM_API_ID/HASH)")
        return
    from telethon import TelegramClient, events  # pip install telethon

    client = TelegramClient(TELEGRAM_SESSION, int(TELEGRAM_API_ID), TELEGRAM_API_HASH)
    await client.start()

    watch: dict[str, list[str]] = {}
    for ws_id, chans in channel_map.items():
        for ch in chans:
            watch.setdefault(ch.lstrip("@").lower(), []).append(ws_id)

    @client.on(events.NewMessage())
    async def handler(event):  # noqa: ANN001
        try:
            chat = await event.get_chat()
            uname = (getattr(chat, "username", "") or "").lower()
            if uname not in watch or not event.raw_text:
                return
            cand = {"text": event.raw_text[:1500], "url": f"https://t.me/{uname}/{event.id}",
                   "author": "@" + uname, "platform": "Telegram",
                   "posted_at": event.date.replace(tzinfo=timezone.utc) if event.date else None,
                   "reach": getattr(chat, "participants_count", 0) or 0}
            for ws_id in watch[uname]:
                _buffer.setdefault(ws_id, []).append(cand)
        except Exception:  # noqa: BLE001
            log.exception("Telegram handler error")

    log.info("Telegram collector listening on %d channels", len(watch))
    await client.run_until_disconnected()
