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
import uuid
import logging
from pathlib import Path
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from telegram import (
    Update,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.helpers import escape_markdown
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
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

# Telegram caps answerCallbackQuery popup text at 200 characters.
CALLBACK_ALERT_LIMIT = 200

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
# httpx logs every Telegram API call at INFO, which leaks the bot token in URLs.
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# ── Conversation states ────────────────────────────────────────────────────────
DATE, TIME, TIMEZONE, TOPIC = range(4)

# ── Persistent storage (per chat) ─────────────────────────────────────────────
# Structure: { chat_id: [ {"datetime": iso, "source_tz": label, "topic": str}, ... ] }
# In a 1:1 DM the chat_id equals the user's id, so legacy per-user entries keep
# working untouched. Meetings added in groups now live under the group's chat id
# and are visible to every participant.
def load_meetings() -> dict[int, list[dict]]:
    if not DATA_FILE.exists():
        return {}
    try:
        raw = json.loads(DATA_FILE.read_text(encoding="utf-8"))
        result: dict[int, list[dict]] = {}
        for cid, data in raw.items():
            cid_int = int(cid)
            if isinstance(data, list):
                result[cid_int] = data
            elif isinstance(data, dict):
                # Migrate legacy single-meeting schema into a one-item list.
                result[cid_int] = [data]
        # Ensure every meeting has a stable id (used to target it from inline
        # buttons). Legacy entries saved before ids existed get one here.
        for items in result.values():
            for m in items:
                m.setdefault("id", uuid.uuid4().hex[:8])
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


# ── Plain-text rendering (for popup alerts, which don't support Markdown) ───────

def _short_label_plain(m: dict) -> str:
    dt = _meeting_dt(m)
    when = dt.strftime("%Y-%m-%d %H:%M") + f" {m.get('source_tz', '')}" if dt else "?"
    return f"{when.strip()} — {m.get('topic', '?')}"


def format_meeting_plain(data: dict) -> str:
    """Plain-text version of format_meeting for callback popups."""
    if "datetime" in data:
        dt = datetime.fromisoformat(data["datetime"])
        lines = [f"Topic: {data['topic']}", "Time:"]
        for label, zone in DISPLAY_TIMEZONES.items():
            local = dt.astimezone(ZoneInfo(zone))
            lines.append(f"  {label}: {local.strftime('%Y-%m-%d %H:%M')}")
        return "\n".join(lines)
    return (
        f"Date: {data.get('date', '?')}\n"
        f"Time: {data.get('time', '?')}\n"
        f"Topic: {data.get('topic', '?')}"
    )


def _clip(text: str, limit: int = CALLBACK_ALERT_LIMIT) -> str:
    """Trim text to the popup character cap, adding an ellipsis if cut."""
    return text if len(text) <= limit else text[: limit - 1] + "…"


# ── Reminders ─────────────────────────────────────────────────────────────────
# We only ever track each chat's closest upcoming meeting. When it passes (or
# when someone adds/removes meetings), we re-pick the closest and reschedule.

def _chat_job_names(chat_id: int) -> tuple[str, str, str]:
    return (
        f"monday_reminder_{chat_id}",
        f"six_h_reminder_{chat_id}",
        f"rotate_{chat_id}",
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
    _reschedule_chat_reminders(context.job_queue, context.job.chat_id)


def _cancel_chat_reminders(job_queue, chat_id: int) -> None:
    if job_queue is None:
        return
    for name in _chat_job_names(chat_id):
        for j in job_queue.get_jobs_by_name(name):
            j.schedule_removal()


def _reschedule_chat_reminders(job_queue, chat_id: int) -> None:
    """Cancel any pending reminders and re-arm them for the chat's soonest meeting."""
    if job_queue is None:
        return
    _cancel_chat_reminders(job_queue, chat_id)

    upcoming, _ = _split_upcoming_past(meetings.get(chat_id, []))
    if not upcoming:
        return
    m = upcoming[0]
    meeting_dt = _meeting_dt(m)
    if meeting_dt is None:
        return

    now = datetime.now(meeting_dt.tzinfo)
    monday_name, six_h_name, rotate_name = _chat_job_names(chat_id)

    monday_dt = _monday_of_meeting_week(meeting_dt)
    if now < monday_dt < meeting_dt:
        job_queue.run_once(
            _send_monday_reminder,
            when=monday_dt,
            chat_id=chat_id,
            data=m,
            name=monday_name,
        )
        logger.info("Monday reminder for chat=%s at %s", chat_id, monday_dt.isoformat())

    six_h_dt = meeting_dt - timedelta(hours=HOURS_BEFORE_REMINDER)
    if six_h_dt > now:
        job_queue.run_once(
            _send_six_hour_reminder,
            when=six_h_dt,
            chat_id=chat_id,
            data=m,
            name=six_h_name,
        )
        logger.info("6h reminder for chat=%s at %s", chat_id, six_h_dt.isoformat())

    # Once this meeting starts, the next closest takes over — re-pick automatically.
    rotate_at = meeting_dt + timedelta(minutes=1)
    if rotate_at > now:
        job_queue.run_once(
            _rotate_to_next_meeting,
            when=rotate_at,
            chat_id=chat_id,
            name=rotate_name,
        )


# ── Handlers ───────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Welcome message."""
    await update.message.reply_text(
        "👋 Hi! I'm your *Meeting Bot*.\n\n"
        "Commands:\n"
        "  /menu – buttons that answer privately (only you see the popup)\n"
        "  /set\\_meeting – add a meeting\n"
        "  /show\\_next\\_meeting – view the closest upcoming meeting\n"
        "  /show\\_all\\_meetings – view all upcoming meetings (closest first)\n"
        "  /archive – view past meetings\n"
        "  /clear\\_meeting – pick a meeting to remove\n"
        "  /help – show this message",
        parse_mode="Markdown",
        reply_markup=_menu_markup(),
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
    chat_id = update.effective_chat.id

    new_meeting = {
        "id":        uuid.uuid4().hex[:8],
        "datetime":  context.user_data["datetime"],
        "source_tz": context.user_data["source_tz"],
        "topic":     context.user_data["topic"],
    }
    meetings.setdefault(chat_id, []).append(new_meeting)
    save_meetings()
    _reschedule_chat_reminders(context.application.job_queue, chat_id)

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


# ── /show_next_meeting & /show_all_meetings ───────────────────────────────────

async def show_next_meeting(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    upcoming, _ = _split_upcoming_past(meetings.get(chat_id, []))
    if not upcoming:
        await update.message.reply_text(
            "You have no upcoming meetings. Use /set\\_meeting to add one.",
            parse_mode="Markdown",
        )
        return
    await update.message.reply_text(
        "📋 *Your next meeting:*\n\n" + format_meeting(upcoming[0]),
        parse_mode="Markdown",
    )


async def show_all_meetings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    upcoming, _ = _split_upcoming_past(meetings.get(chat_id, []))
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
    chat_id = update.effective_chat.id
    _, past = _split_upcoming_past(meetings.get(chat_id, []))
    if not past:
        await update.message.reply_text("📦 Archive is empty.")
        return
    blocks = [f"*#{i}*\n{format_meeting(m)}" for i, m in enumerate(past, 1)]
    await update.message.reply_text(
        "📦 *Archived meetings* (most recent first):\n\n" + "\n\n".join(blocks),
        parse_mode="Markdown",
    )


# ── /menu — inline buttons that answer privately in a popup ─────────────────────
# Tapping a button triggers a callback we answer with show_alert=True, so the
# reply appears as a popup ONLY to the person who tapped — the rest of the group
# sees nothing. Great for groups where you don't want the bot spamming messages.

def _menu_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📋 Next meeting", callback_data="show_next")],
            [InlineKeyboardButton("🗓 All meetings", callback_data="show_all")],
            [InlineKeyboardButton("📦 Archive", callback_data="show_archive")],
        ]
    )


async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Post the button menu. The menu message itself is the only group-visible bit."""
    await update.message.reply_text(
        "Tap a button — the answer pops up only for you 👇",
        reply_markup=_menu_markup(),
    )


def _next_meeting_text(chat_id: int) -> str:
    upcoming, _ = _split_upcoming_past(meetings.get(chat_id, []))
    if not upcoming:
        return "No upcoming meetings. Use /set_meeting to add one."
    return "Your next meeting:\n\n" + format_meeting_plain(upcoming[0])


def _all_meetings_text(chat_id: int) -> str:
    upcoming, _ = _split_upcoming_past(meetings.get(chat_id, []))
    if not upcoming:
        return "No upcoming meetings. Use /set_meeting to add one."
    lines = [f"{i}. {_short_label_plain(m)}" for i, m in enumerate(upcoming, 1)]
    return "Upcoming meetings:\n" + "\n".join(lines)


def _archive_text(chat_id: int) -> str:
    _, past = _split_upcoming_past(meetings.get(chat_id, []))
    if not past:
        return "Archive is empty."
    lines = [f"{i}. {_short_label_plain(m)}" for i, m in enumerate(past, 1)]
    return "Archived meetings:\n" + "\n".join(lines)


async def on_menu_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Answer a menu button tap with a private popup."""
    query = update.callback_query
    chat_id = query.message.chat.id

    if query.data == "show_next":
        text = _next_meeting_text(chat_id)
    elif query.data == "show_all":
        text = _all_meetings_text(chat_id)
    elif query.data == "show_archive":
        text = _archive_text(chat_id)
    else:
        text = "Unknown action."

    # show_alert=True => popup only the tapping user sees. Telegram caps it at 200.
    await query.answer(text=_clip(text), show_alert=True)


# ── /clear_meeting — inline buttons (works in groups, no spam) ──────────────────
# The old flow used a ReplyKeyboardMarkup of numbers. Those taps are *plain
# messages*, which Telegram's group privacy mode silently drops — so in a group
# the pick never reached the bot and clearing "did nothing". Inline-button taps
# (callback queries) are ALWAYS delivered, even with privacy mode on, and we
# answer each with a private popup so we don't spam the group.

def _clear_keyboard(upcoming: list[dict]) -> InlineKeyboardMarkup:
    """One button per upcoming meeting, plus ALL / Cancel."""
    # Button labels are capped so long topics don't overflow the popup.
    rows = [
        [InlineKeyboardButton(f"🗑 {_short_label_plain(m)}"[:64],
                              callback_data=f"clr:{m['id']}")]
        for m in upcoming
    ]
    if len(upcoming) > 1:
        rows.append([InlineKeyboardButton("🗑 Clear ALL upcoming",
                                          callback_data="clr:all")])
    rows.append([InlineKeyboardButton("✖ Cancel", callback_data="clr:cancel")])
    return InlineKeyboardMarkup(rows)


async def clear_meeting_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    upcoming, _ = _split_upcoming_past(meetings.get(chat_id, []))
    if not upcoming:
        await update.message.reply_text(
            "Nothing upcoming to clear. Past meetings live in /archive."
        )
        return
    await update.message.reply_text(
        "Tap a meeting to remove it 👇",
        reply_markup=_clear_keyboard(upcoming),
    )


async def on_clear_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle a tap on a /clear_meeting inline button."""
    query = update.callback_query
    chat_id = query.message.chat.id
    action = query.data.split(":", 1)[1]

    if action == "cancel":
        await query.answer("Cancelled.")
        await query.edit_message_text("✖ Cancelled — nothing removed.")
        return

    if action == "all":
        upcoming, _ = _split_upcoming_past(meetings.get(chat_id, []))
        removed = len(upcoming)
        items = meetings.get(chat_id, [])
        # Keep past entries; drop the upcoming ones. Compare by identity so
        # value-identical past/upcoming meetings aren't conflated.
        meetings[chat_id] = [m for m in items if all(m is not u for u in upcoming)]
        save_meetings()
        _reschedule_chat_reminders(context.application.job_queue, chat_id)
        await query.answer(_clip(f"Removed {removed} meeting(s)."), show_alert=True)
        await query.edit_message_text(f"🗑 Removed {removed} upcoming meeting(s).")
        return

    # Otherwise `action` is a meeting id.
    items = meetings.get(chat_id, [])
    target = next((m for m in items if m.get("id") == action), None)
    if target is not None:
        items.remove(target)
        save_meetings()
        _reschedule_chat_reminders(context.application.job_queue, chat_id)
        await query.answer(_clip(f"Removed: {target.get('topic', '?')}"),
                           show_alert=True)
    else:
        await query.answer("That meeting was already removed.", show_alert=True)

    # Refresh the picker so the removed entry disappears for everyone.
    upcoming, _ = _split_upcoming_past(meetings.get(chat_id, []))
    if upcoming:
        await query.edit_message_reply_markup(reply_markup=_clear_keyboard(upcoming))
    else:
        await query.edit_message_text("🗑 No more upcoming meetings.")


# ── App setup ──────────────────────────────────────────────────────────────────

async def _schedule_all_on_startup(app: Application) -> None:
    """After the JobQueue is up, arm reminders for each chat's closest meeting."""
    if app.job_queue is None:
        logger.warning(
            "JobQueue not available — reminders disabled. "
            "Install python-telegram-bot[job-queue]."
        )
        return
    for chat_id in meetings:
        _reschedule_chat_reminders(app.job_queue, chat_id)
    logger.info("Reminders armed for %d chat(s)", len(meetings))


def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN env variable is not set.")

    global meetings
    meetings = load_meetings()
    total = sum(len(v) for v in meetings.values())
    logger.info("Loaded %d meeting(s) across %d chat(s) from %s",
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

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(set_conv)
    app.add_handler(CommandHandler("clear_meeting", clear_meeting_start))
    app.add_handler(CommandHandler("menu", menu))
    # Scope the callback handlers by prefix so menu taps and clear taps don't
    # swallow each other.
    app.add_handler(CallbackQueryHandler(on_menu_button, pattern="^show_"))
    app.add_handler(CallbackQueryHandler(on_clear_button, pattern="^clr:"))
    app.add_handler(CommandHandler("show_next_meeting", show_next_meeting))
    app.add_handler(CommandHandler("show_all_meetings", show_all_meetings))
    app.add_handler(CommandHandler("archive", show_archive))

    logger.info("Bot is running…")
    app.run_polling()


if __name__ == "__main__":
    main()