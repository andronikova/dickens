"""
Telegram Meeting Bot
Saves the next meeting date, time, and topic.

Requirements:
    pip install python-telegram-bot

Usage:
    1. Create a bot via @BotFather on Telegram and get your token.
    2. Set your token in the BOT_TOKEN variable below (or use an env variable).
    $env:TELEGRAM_BOT_TOKEN=
    3. Run: python meeting_bot.py
"""

import os
import logging
from datetime import datetime
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

# ── Configuration ──────────────────────────────────────────────────────────────
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ── Conversation states ────────────────────────────────────────────────────────
DATE, TIME, TOPIC = range(3)

# ── In-memory storage (per user) ──────────────────────────────────────────────
# Structure: { user_id: {"date": ..., "time": ..., "topic": ...} }
meetings: dict[int, dict] = {}


# ── Helpers ────────────────────────────────────────────────────────────────────
def format_meeting(data: dict) -> str:
    return (
        f"📅 *Date:*  {data['date']}\n"
        f"⏰ *Time:*  {data['time']}\n"
        f"📝 *Topic:* {data['topic']}"
    )


# ── Handlers ───────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Welcome message."""
    await update.message.reply_text(
        "👋 Hi! I'm your *Meeting Bot*.\n\n"
        "Commands:\n"
        "  /set\\_meeting – save your next meeting\n"
        "  /show\\_meeting – view the saved meeting\n"
        "  /clear\\_meeting – delete the saved meeting\n"
        "  /help – show this message",
        parse_mode="Markdown",
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await start(update, context)


# ── /set_meeting conversation ──────────────────────────────────────────────────

async def set_meeting_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point: ask for the date."""
    await update.message.reply_text(
        "Let's save your next meeting! 🗓\n\n"
        "Please enter the *date* (e.g. 2025-06-15 or 15.06.2025):",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove(),
    )
    return DATE


async def receive_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Store date and ask for time."""
    raw = update.message.text.strip()

    # Try a few common formats
    parsed = None
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%m/%d/%Y"):
        try:
            parsed = datetime.strptime(raw, fmt)
            break
        except ValueError:
            pass

    if parsed is None:
        await update.message.reply_text(
            "⚠️ I couldn't parse that date. Please use a format like *2025-06-15* or *15.06.2025*:",
            parse_mode="Markdown",
        )
        return DATE

    context.user_data["date"] = parsed.strftime("%d %B %Y")  # e.g. "15 June 2025"
    await update.message.reply_text(
        f"Got it: *{context.user_data['date']}* ✅\n\n"
        "Now enter the *time* (e.g. 14:30 or 2:30 PM):",
        parse_mode="Markdown",
    )
    return TIME


async def receive_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Store time and ask for topic."""
    raw = update.message.text.strip()

    parsed = None
    for fmt in ("%H:%M", "%I:%M %p", "%I:%M%p", "%I %p"):
        try:
            parsed = datetime.strptime(raw.upper(), fmt.upper())
            break
        except ValueError:
            pass

    if parsed is None:
        await update.message.reply_text(
            "⚠️ I couldn't parse that time. Please use *HH:MM* (24h) or *2:30 PM*:",
            parse_mode="Markdown",
        )
        return TIME

    context.user_data["time"] = parsed.strftime("%H:%M")
    await update.message.reply_text(
        f"Got it: *{context.user_data['time']}* ✅\n\n"
        "Finally, what is the *topic* of the meeting?",
        parse_mode="Markdown",
    )
    return TOPIC


async def receive_topic(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Store topic and confirm."""
    topic = update.message.text.strip()
    if not topic:
        await update.message.reply_text("Please enter a topic (it can't be empty):")
        return TOPIC

    context.user_data["topic"] = topic
    user_id = update.effective_user.id

    meetings[user_id] = {
        "date": context.user_data["date"],
        "time": context.user_data["time"],
        "topic": context.user_data["topic"],
    }

    await update.message.reply_text(
        "✅ Meeting saved!\n\n" + format_meeting(meetings[user_id]),
        parse_mode="Markdown",
    )
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancel the conversation."""
    await update.message.reply_text(
        "❌ Cancelled. No meeting was saved.",
        reply_markup=ReplyKeyboardRemove(),
    )
    return ConversationHandler.END


# ── /show_meeting ──────────────────────────────────────────────────────────────

async def show_meeting(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if user_id not in meetings:
        await update.message.reply_text(
            "You have no meeting saved yet. Use /set\\_meeting to add one.",
            parse_mode="Markdown",
        )
        return
    await update.message.reply_text(
        "📋 *Your next meeting:*\n\n" + format_meeting(meetings[user_id]),
        parse_mode="Markdown",
    )


# ── /clear_meeting ─────────────────────────────────────────────────────────────

async def clear_meeting(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if user_id in meetings:
        del meetings[user_id]
        await update.message.reply_text("🗑 Meeting cleared.")
    else:
        await update.message.reply_text("Nothing to clear – you have no saved meeting.")


# ── App setup ──────────────────────────────────────────────────────────────────

def main() -> None:
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("set_meeting", set_meeting_start)],
        states={
            DATE:  [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_date)],
            TIME:  [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_time)],
            TOPIC: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_topic)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(conv_handler)
    app.add_handler(CommandHandler("show_meeting", show_meeting))
    app.add_handler(CommandHandler("clear_meeting", clear_meeting))

    logger.info("Bot is running…")
    app.run_polling()


if __name__ == "__main__":
    main()