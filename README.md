# Meeting Bot

A simple Telegram bot that saves your next meeting's **date**, **time**, and **topic**, then lets you view or clear it.

Meetings are stored in memory per user — they are forgotten when the bot restarts.

---

## Requirements

- Python 3.10+ (the project ships with a `.venv` using Python 3.14)
- [`python-telegram-bot`](https://python-telegram-bot.org/) v22.x (already installed in `.venv`)
- A Telegram bot token from [@BotFather](https://t.me/BotFather)

---

## Setup

### 1. Get a bot token

1. Open Telegram and message [@BotFather](https://t.me/BotFather).
2. Send `/newbot` and follow the prompts (pick a name and a username ending in `bot`).
3. Copy the token BotFather gives you. It looks like `123456789:ABCdefGHIjklMNOpqrSTUvwxYZ`.

### 2. Install dependencies (if not already installed)

From the project root:

```powershell
.\.venv\Scripts\python.exe -m pip install python-telegram-bot
```

### 3. Provide the token

The bot reads the token from the `TELEGRAM_BOT_TOKEN` environment variable.

**PowerShell (current session only):**

```powershell
$env:TELEGRAM_BOT_TOKEN = "123456789:ABCdefGHIjklMNOpqrSTUvwxYZ"
```

**PowerShell (persistent, for your user account):**

```powershell
[Environment]::SetEnvironmentVariable("TELEGRAM_BOT_TOKEN", "123456789:ABC...", "User")
```

(You'll need to open a new terminal for the persistent variable to take effect.)

---

## Running the bot

From the project root (`C:\Users\Daria\PycharmProjects\dickens`):

```powershell
.\.venv\Scripts\python.exe .\dickens\meeting_bot.py
```

You should see:

```
INFO - __main__ - Bot is running…
```

Leave the terminal open — the bot stops when you close it or press `Ctrl+C`.

---

## Using the bot in Telegram

Open a chat with your bot and try these commands:

| Command          | What it does                                                  |
| ---------------- | ------------------------------------------------------------- |
| `/start`         | Show the welcome message and command list                     |
| `/help`          | Same as `/start`                                              |
| `/set_meeting`   | Start a guided flow to save a meeting (date → time → topic)   |
| `/show_meeting`  | Show your saved meeting                                       |
| `/clear_meeting` | Delete your saved meeting                                     |
| `/cancel`        | Abort the `/set_meeting` flow without saving                  |

### Example: saving a meeting

1. Send `/set_meeting`.
2. The bot asks for a **date**. Accepted formats:
   - `2025-06-15`
   - `15.06.2025`
   - `15/06/2025`
   - `06/15/2025`
3. The bot asks for a **time**. Accepted formats:
   - `14:30` (24-hour)
   - `2:30 PM`
   - `2:30PM`
   - `2 PM`
4. The bot asks for a **topic** — any non-empty text.
5. The bot confirms with a summary.

Send `/show_meeting` any time to see what's saved, or `/clear_meeting` to remove it.

---

## Troubleshooting

- **`InvalidToken` on startup** — `TELEGRAM_BOT_TOKEN` is empty or wrong. Re-check step 3 of *Setup*.
- **`ModuleNotFoundError: No module named 'telegram'`** — you're using the wrong Python. Use `.venv\Scripts\python.exe`, not the system Python.
- **"I couldn't parse that date/time"** — your input didn't match the accepted formats above. Try again with one of the listed examples.
- **Bot doesn't reply** — make sure the script is still running in your terminal and that you're chatting with the right bot (the one whose token you used).

---

## Notes

- Meetings are stored only in memory (`meetings: dict[int, dict]` in `meeting_bot.py`). Restarting the bot wipes all saved meetings.
- Each Telegram user has their own meeting slot — users don't see each other's data.
