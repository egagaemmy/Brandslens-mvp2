"""The WhatsApp Business API replacement for MVP: a Telegram bot tip line.

Why this instead of WhatsApp: WhatsApp Business API requires Meta business
verification (a multi-week approval process) before you can receive a single
message. A Telegram bot via @BotFather is live in under five minutes, free,
and does exactly the same job — someone forwards a suspicious message or
screenshot description, it lands in your incident review queue instantly.
Migrate to WhatsApp later once that approval clears; nothing else changes,
since both just feed the same ingest_candidates() pipeline.

Usage: give your team/partners this bot's @username. They DM or forward
suspicious content to it. Anyone can message it, but only text you route to a
specific workspace (via a per-workspace bot command, see below) gets ingested
— this is deliberately simple for MVP, tighten with per-user allow-lists once
you have real usage patterns to design against.
"""
from __future__ import annotations
import logging
from ..config import TIPLINE_BOT_TOKEN
from ..db import SessionLocal
from ..models import Workspace, TipLineMessage
from .pipeline import ingest_candidates
from sqlalchemy import select

log = logging.getLogger("tipline")


async def start() -> None:
    if not TIPLINE_BOT_TOKEN:
        log.info("Tip-line bot not configured — skipping (set TIPLINE_BOT_TOKEN from @BotFather)")
        return
    from telegram import Update                                   # pip install python-telegram-bot
    from telegram.ext import Application, MessageHandler, CommandHandler, ContextTypes, filters

    async def cmd_workspace(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/workspace ACME — sets which brand this chat's forwards apply to."""
        if not context.args:
            await update.message.reply_text("Usage: /workspace <workspace name or ref>")
            return
        context.chat_data["workspace_query"] = " ".join(context.args)
        await update.message.reply_text(f"Tip-line set to report against: {context.chat_data['workspace_query']}")

    async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        text = update.message.text or update.message.caption or ""
        if not text.strip():
            await update.message.reply_text("Got it, but I need text to act on — forward the message text itself.")
            return
        query = context.chat_data.get("workspace_query")
        db = SessionLocal()
        try:
            ws = None
            if query:
                ws = db.scalar(select(Workspace).where(Workspace.name.ilike(f"%{query}%")))
            if not ws:
                ws = db.scalar(select(Workspace).where(Workspace.active == True))  # noqa: E712
            if not ws:
                await update.message.reply_text("No workspace configured yet — nothing to file this against.")
                return
            forwarded_from = ""
            if update.message.forward_origin:
                forwarded_from = getattr(update.message.forward_origin, "sender_user_name", "") or \
                                 getattr(update.message.forward_origin, "chat", "")
            db.add(TipLineMessage(workspace_id=ws.id, from_telegram_id=str(update.effective_user.id),
                                  text=text, forwarded_from=str(forwarded_from)))
            db.commit()
            summary = ingest_candidates(db, ws, [{
                "text": text, "url": "", "author": "tipline:" + str(update.effective_user.id),
                "platform": "WhatsApp" if "whatsapp" in text.lower() else "Tip Line",
                "posted_at": None, "reach": 0,
            }], source="tipline")
            if summary["new"]:
                await update.message.reply_text(f"Logged against {ws.name}. Thank you — this has been added to the incident queue.")
            else:
                await update.message.reply_text("Received — didn't match any tracked keyword, but it's saved for manual review.")
        finally:
            db.close()

    app = Application.builder().token(TIPLINE_BOT_TOKEN).build()
    app.add_handler(CommandHandler("workspace", cmd_workspace))
    app.add_handler(MessageHandler(filters.TEXT | filters.CAPTION, on_message))
    log.info("Tip-line bot started")
    await app.run_polling()
