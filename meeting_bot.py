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
import json
import logging
from pathlib import Path
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.helpers import escape_markdown
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

# ── Configuration ──────────────────────────────────────────────────────────────
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DATA_FILE = Path(os.getenv("MEETINGS_FILE", "meetings.json"))

# Display timezones: label → IANA zone name. "Europe" = Berlin (CET/CEST).
DISPLAY_TIMEZONES: dict[str, str] = {
    "Europe":    "Europe/Berlin",
    "Moscow":    "Europe/Moscow",
    "Hong Kong": "Asia/Hong_Kong",
}

# Hour of the day (in the meeting's source timezone) at which the Monday
# "you have a meeting this week" reminder fires.
MONDAY_REMINDER_HOUR = 9
# How many hours before the meeting the "starts soon" reminder fires.
HOURS_BEFORE_REMINDER = 6

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
# httpx logs every Telegram API call at INFO, which leaks the bot token in URLs.
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# ── Conversation states ────────────────────────────────────────────────────────
DATE, TIME, TIMEZONE, TOPIC, CLEAR_PICK = range(5)

# ── Persistent storage (per user) ─────────────────────────────────────────────
# Structure: { user_id: [ {"datetime": iso, "source_tz": label, "topic": str}, ... ] }
def load_meetings() -> dict[int, list[dict]]:
    if not DATA_FILE.exists():
        return {}
    try:
        raw = json.loads(DATA_FILE.read_text(encoding="utf-8"))
        result: dict[int, list[dict]] = {}
        for uid, data in raw.items():
            uid_int = int(uid)
            if isinstance(data, list):
                result[uid_int] = data
            elif isinstance(data, dict):
                # Migrate legacy single-meeting schema into a one-item list.
                result[uid_int] = [data]
        return result
    except (json.JSONDecodeError, ValueError) as e:
        logging.getLogger(__name__).warning("Could not read %s: %s", DATA_FILE, e)
        return {}


def save_meetings() -> None:
    tmp = DATA_FILE.with_suffix(DATA_FILE.suffix + ".tmp")
    tmp.write_text(json.dumps(meetings, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(DATA_FILE)  # atomic on POSIX & Windows


meetings: dict[int, list[dict]] = {}


# ── Helpers ────────────────────────────────────────────────────────────────────
def _format_times_block(iso_dt: str) -> str:
    """Render the saved ISO datetime in each display timezone."""
    dt = datetime.fromisoformat(iso_dt)
    lines = []
    for label, zone in DISPLAY_TIMEZONES.items():
        local = dt.astimezone(ZoneInfo(zone))
        lines.append(f"    • *{label}:* {local.strftime('%Y-%m-%d %H:%M')}")
    return "\n".join(lines)


def format_meeting(data: dict) -> str:
    # New schema stores an ISO datetime; fall back to the legacy date/time fields.
    if "datetime" in data:
        return (
            f"📝 *Topic:* {escape_markdown(data['topic'], version=1)}\n"
            f"⏰ *Time:*\n{_format_times_block(data['datetime'])}"
        )
    return (
        f"📅 *Date:*  {data.get('date', '?')}\n"
        f"⏰ *Time:*  {data.get('time', '?')}\n"
        f"📝 *Topic:* {escape_markdown(data.get('topic', '?'), version=1)}"
    )


def _meeting_dt(m: dict) -> datetime | None:
    iso = m.get("datetime")
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso)
    except ValueError:
        return None


def _split_upcoming_past(items: list[dict]) -> tuple[list[dict], list[dict]]:
    """Return (upcoming sorted closest-first, past sorted most-recent-first).

    Entries without a parseable datetime (legacy schema) are treated as past."""
    now = datetime.now(ZoneInfo("UTC"))
    upcoming, past = [], []
    for m in items:
        dt = _meeting_dt(m)
        if dt is None or dt < now:
            past.append(m)
        else:
            upcoming.append(m)
    sentinel = datetime.min.replace(tzinfo=ZoneInfo("UTC"))
    upcoming.sort(key=lambda m: _meeting_dt(m) or sentinel)
    past.sort(key=lambda m: _meeting_dt(m) or sentinel, reverse=True)
    return upcoming, past


def _short_label(m: dict) -> str:
    """One-liner used in pickers/lists: 'YYYY-MM-DD HH:MM TZ — topic'."""
    dt = _meeting_dt(m)
    when = dt.strftime("%Y-%m-%d %H:%M") + f" {m.get('source_tz', '')}" if dt else "?"
    return f"{when.strip()} — {escape_markdown(m.get('topic', '?'), version=1)}"


# ── Reminders ─────────────────────────────────────────────────────────────────
# We only ever track the user's closest upcoming meeting. When it passes (or
# when the user adds/removes meetings), we re-pick the closest and reschedule.

def _user_job_names(user_id: int) -> tuple[str, str, str]:
    return (
        f"monday_reminder_{user_id}",
        f"six_h_reminder_{user_id}",
        f"rotate_{user_id}",
    )


def _monday_of_meeting_week(meeting_dt: datetime) -> datetime:
    """Monday at MONDAY_REMINDER_HOUR in meeting_dt's tz, for the meeting's ISO week."""
    monday = meeting_dt - timedelta(days=meeting_dt.weekday())
    return monday.replace(
        hour=MONDAY_REMINDER_HOUR, minute=0, second=0, microsecond=0
    )


async def _send_monday_reminder(context: ContextTypes.DEFAULT_TYPE) -> None:
    m = context.job.data
    await context.bot.send_message(
        chat_id=context.job.chat_id,
        text="📅 *Reminder:* you have a meeting this week!\n\n" + format_meeting(m),
        parse_mode="Markdown",
    )


async def _send_six_hour_reminder(context: ContextTypes.DEFAULT_TYPE) -> None:
    m = context.job.data
    await context.bot.send_message(
        chat_id=context.job.chat_id,
        text=f"⏰ *Reminder:* your meeting starts in {HOURS_BEFORE_REMINDER} hours.\n\n"
             + format_meeting(m),
        parse_mode="Markdown",
    )


async def _rotate_to_next_meeting(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Fires shortly after the current closest meeting starts — pick the new closest."""
    _reschedule_user_reminders(context.job_queue, context.job.chat_id)


def _cancel_user_reminders(job_queue, user_id: int) -> None:
    if job_queue is None:
        return
    for name in _user_job_names(user_id):
        for j in job_queue.get_jobs_by_name(name):
            j.schedule_removal()


def _reschedule_user_reminders(job_queue, user_id: int) -> None:
    """Cancel any pending reminders and re-arm them for the user's soonest meeting."""
    if job_queue is None:
        return
    _cancel_user_reminders(job_queue, user_id)

    upcoming, _ = _split_upcoming_past(meetings.get(user_id, []))
    if not upcoming:
        return
    m = upcoming[0]
    meeting_dt = _meeting_dt(m)
    if meeting_dt is None:
        return

    now = datetime.now(meeting_dt.tzinfo)
    monday_name, six_h_name, rotate_name = _user_job_names(user_id)

    monday_dt = _monday_of_meeting_week(meeting_dt)
    if now < monday_dt < meeting_dt:
        job_queue.run_once(
            _send_monday_reminder,
            when=monday_dt,
            chat_id=user_id,
            data=m,
            name=monday_name,
        )
        logger.info("Monday reminder for user=%s at %s", user_id, monday_dt.isoformat())

    six_h_dt = meeting_dt - timedelta(hours=HOURS_BEFORE_REMINDER)
    if six_h_dt > now:
        job_queue.run_once(
            _send_six_hour_reminder,
            when=six_h_dt,
            chat_id=user_id,
            data=m,
            name=six_h_name,
        )
        logger.info("6h reminder for user=%s at %s", user_id, six_h_dt.isoformat())

    # Once this meeting starts, the next closest takes over — re-pick automatically.
    rotate_at = meeting_dt + timedelta(minutes=1)
    if rotate_at > now:
        job_queue.run_once(
            _rotate_to_next_meeting,
            when=rotate_at,
            chat_id=user_id,
            name=rotate_name,
        )


# ── Handlers ───────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Welcome message."""
    await update.message.reply_text(
        "👋 Hi! I'm your *Meeting Bot*.\n\n"
        "Commands:\n"
        "  /set\\_meeting – add a meeting\n"
        "  /show\\_next\\_meeting – view the closest upcoming meeting\n"
        "  /show\\_all\\_meetings – view all upcoming meetings (closest first)\n"
        "  /archive – view past meetings\n"
        "  /clear\\_meeting – pick a meeting to remove\n"
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

    context.user_data["date_obj"] = parsed.date()
    context.user_data["date"] = parsed.strftime("%d %B %Y")  # e.g. "15 June 2025"
    await update.message.reply_text(
        f"Got it: *{context.user_data['date']}* ✅\n\n"
        "Now enter the *time* (e.g. 14:30 or 2:30 PM):",
        parse_mode="Markdown",
    )
    return TIME


async def receive_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Store time and ask which timezone it refers to."""
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

    context.user_data["time_obj"] = parsed.time()
    context.user_data["time"] = parsed.strftime("%H:%M")

    keyboard = ReplyKeyboardMarkup(
        [list(DISPLAY_TIMEZONES.keys())],
        one_time_keyboard=True,
        resize_keyboard=True,
    )
    await update.message.reply_text(
        f"Got it: *{context.user_data['time']}* ✅\n\n"
        "Which *timezone* is that in?",
        parse_mode="Markdown",
        reply_markup=keyboard,
    )
    return TIMEZONE


async def receive_timezone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Combine date+time with the chosen timezone, then ask for topic."""
    choice = update.message.text.strip()
    if choice not in DISPLAY_TIMEZONES:
        keyboard = ReplyKeyboardMarkup(
            [list(DISPLAY_TIMEZONES.keys())],
            one_time_keyboard=True,
            resize_keyboard=True,
        )
        await update.message.reply_text(
            "⚠️ Please pick one of the offered timezones:",
            reply_markup=keyboard,
        )
        return TIMEZONE

    zone = ZoneInfo(DISPLAY_TIMEZONES[choice])
    aware = datetime.combine(
        context.user_data["date_obj"],
        context.user_data["time_obj"],
        tzinfo=zone,
    )
    context.user_data["datetime"] = aware.isoformat()
    context.user_data["source_tz"] = choice

    await update.message.reply_text(
        f"Saved as *{choice}* time ✅\n\n"
        "Finally, what is the *topic* of the meeting?",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove(),
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

    new_meeting = {
        "datetime":  context.user_data["datetime"],
        "source_tz": context.user_data["source_tz"],
        "topic":     context.user_data["topic"],
    }
    meetings.setdefault(user_id, []).append(new_meeting)
    save_meetings()
    _reschedule_user_reminders(context.application.job_queue, user_id)

    await update.message.reply_text(
        "✅ Meeting saved!\n\n" + format_meeting(new_meeting)
        + f"\n\n_For your closest upcoming meeting I'll send a reminder on "
          f"Monday at {MONDAY_REMINDER_HOUR:02d}:00 and again "
          f"{HOURS_BEFORE_REMINDER}h before it starts._",
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
    upcoming, _ = _split_upcoming_past(meetings.get(user_id, []))
    if not upcoming:
        await update.message.reply_text(
            "You have no upcoming meetings. Use /set\\_meeting to add one.",
            parse_mode="Markdown",
        )
        return
    blocks = [f"*#{i}*\n{format_meeting(m)}" for i, m in enumerate(upcoming, 1)]
    await update.message.reply_text(
        "📋 *Upcoming meetings* (closest first):\n\n" + "\n\n".join(blocks),
        parse_mode="Markdown",
    )


# ── /archive ───────────────────────────────────────────────────────────────────

async def show_archive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    _, past = _split_upcoming_past(meetings.get(user_id, []))
    if not past:
        await update.message.reply_text("📦 Archive is empty.")
        return
    blocks = [f"*#{i}*\n{format_meeting(m)}" for i, m in enumerate(past, 1)]
    await update.message.reply_text(
        "📦 *Archived meetings* (most recent first):\n\n" + "\n\n".join(blocks),
        parse_mode="Markdown",
    )


# ── /clear_meeting conversation ────────────────────────────────────────────────

async def clear_meeting_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    upcoming, _ = _split_upcoming_past(meetings.get(user_id, []))
    if not upcoming:
        await update.message.reply_text(
            "Nothing upcoming to clear. Past meetings live in /archive."
        )
        return ConversationHandler.END

    context.user_data["clear_list"] = upcoming
    lines = [f"  *{i}.* {_short_label(m)}" for i, m in enumerate(upcoming, 1)]
    numbers = [str(i) for i in range(1, len(upcoming) + 1)]
    # Chunk number buttons so the keyboard stays usable.
    rows = [numbers[i:i + 5] for i in range(0, len(numbers), 5)] + [["all", "/cancel"]]
    keyboard = ReplyKeyboardMarkup(rows, one_time_keyboard=True, resize_keyboard=True)
    await update.message.reply_text(
        "Which meeting would you like to remove?\n\n"
        + "\n".join(lines)
        + "\n\nReply with a number, *all*, or /cancel.",
        parse_mode="Markdown",
        reply_markup=keyboard,
    )
    return CLEAR_PICK


async def receive_clear_pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw = update.message.text.strip().lower()
    user_id = update.effective_user.id
    upcoming = context.user_data.get("clear_list", [])

    if raw == "all":
        items = meetings.get(user_id, [])
        # Keep past entries; drop the upcoming ones we showed. Compare by identity
        # so value-identical past/upcoming meetings aren't conflated.
        meetings[user_id] = [m for m in items if all(m is not u for u in upcoming)]
        save_meetings()
        _reschedule_user_reminders(context.application.job_queue, user_id)
        await update.message.reply_text(
            f"🗑 Removed {len(upcoming)} upcoming meeting(s).",
            reply_markup=ReplyKeyboardRemove(),
        )
        return ConversationHandler.END

    try:
        idx = int(raw) - 1
    except ValueError:
        await update.message.reply_text(
            "Please reply with a number from the list, *all*, or /cancel.",
            parse_mode="Markdown",
        )
        return CLEAR_PICK

    if not (0 <= idx < len(upcoming)):
        await update.message.reply_text(
            "That number isn't in the list. Try again or /cancel."
        )
        return CLEAR_PICK

    target = upcoming[idx]
    try:
        meetings.get(user_id, []).remove(target)
    except ValueError:
        await update.message.reply_text(
            "Couldn't find that meeting anymore — it may have been removed already.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return ConversationHandler.END

    save_meetings()
    _reschedule_user_reminders(context.application.job_queue, user_id)
    await update.message.reply_text(
        f"🗑 Removed: *{escape_markdown(target.get('topic', '?'), version=1)}*",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove(),
    )
    return ConversationHandler.END


# ── App setup ──────────────────────────────────────────────────────────────────

async def _schedule_all_on_startup(app: Application) -> None:
    """After the JobQueue is up, arm reminders for each user's closest meeting."""
    if app.job_queue is None:
        logger.warning(
            "JobQueue not available — reminders disabled. "
            "Install python-telegram-bot[job-queue]."
        )
        return
    for user_id in meetings:
        _reschedule_user_reminders(app.job_queue, user_id)
    logger.info("Reminders armed for %d user(s)", len(meetings))


def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN env variable is not set.")

    global meetings
    meetings = load_meetings()
    total = sum(len(v) for v in meetings.values())
    logger.info("Loaded %d meeting(s) across %d user(s) from %s",
                total, len(meetings), DATA_FILE)

    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(_schedule_all_on_startup)
        .build()
    )

    set_conv = ConversationHandler(
        entry_points=[CommandHandler("set_meeting", set_meeting_start)],
        states={
            DATE:     [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_date)],
            TIME:     [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_time)],
            TIMEZONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_timezone)],
            TOPIC:    [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_topic)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    clear_conv = ConversationHandler(
        entry_points=[CommandHandler("clear_meeting", clear_meeting_start)],
        states={
            CLEAR_PICK: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_clear_pick)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(set_conv)
    app.add_handler(clear_conv)
    app.add_handler(CommandHandler("show_meeting", show_meeting))
    app.add_handler(CommandHandler("archive", show_archive))

    logger.info("Bot is running…")
    app.run_polling()


if __name__ == "__main__":
    main()