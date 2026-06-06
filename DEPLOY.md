# Deploy guide — Meeting Bot

Two free, always-on options. Pick **one**.

---

## Token storage — read first

The bot reads its token from the `TELEGRAM_BOT_TOKEN` environment variable.
**Rules:**

1. Never hardcode the token in `meeting_bot.py`.
2. Never commit `.env` files or anything containing the token (`.gitignore` already covers this).
3. If you ever paste the token in chat / a screenshot / a public repo by mistake → open BotFather → `/revoke` → generate a new one.

**Where to put the token, per environment:**

| Where | How |
|---|---|
| Local dev (Windows) | `setx TELEGRAM_BOT_TOKEN "12345:abc..."` (new shells only) or `$env:TELEGRAM_BOT_TOKEN="..."` for one session |
| Local dev (Linux/macOS) | `echo 'export TELEGRAM_BOT_TOKEN=...' >> ~/.bashrc` |
| Oracle Cloud VM | systemd `EnvironmentFile=/etc/meetingbot.env`, file `chmod 600`, owned by the bot user |
| Fly.io | `fly secrets set TELEGRAM_BOT_TOKEN=...` (encrypted at rest, injected at runtime) |

---

## Option A — Oracle Cloud Free Tier (recommended)

**Why:** truly free forever, never sleeps, persistent disk → your `meetings.json` survives.

### 1. Create a VM
1. Sign up at <https://cloud.oracle.com> (needs a card for verification; not charged).
2. Console → Compute → Instances → **Create Instance**.
3. Image: **Ubuntu 22.04 (Always Free eligible)**.
4. Shape: **VM.Standard.A1.Flex** (ARM, 1 OCPU, 6 GB) — Always Free.
5. Add your SSH public key. Create.

### 2. SSH in and install Python
```bash
ssh ubuntu@<your-vm-ip>
sudo apt update && sudo apt install -y python3-venv git
```

### 3. Get the code
```bash
git clone <your-repo-url> ~/meetingbot
cd ~/meetingbot
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### 4. Store the token
```bash
sudo tee /etc/meetingbot.env >/dev/null <<'EOF'
TELEGRAM_BOT_TOKEN=PASTE_YOUR_TOKEN_HERE
EOF
sudo chmod 600 /etc/meetingbot.env
sudo chown ubuntu:ubuntu /etc/meetingbot.env
```

### 5. Run as a systemd service (auto-restart, auto-start on boot)
```bash
sudo tee /etc/systemd/system/meetingbot.service >/dev/null <<'EOF'
[Unit]
Description=Telegram Meeting Bot
After=network-online.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/meetingbot
EnvironmentFile=/etc/meetingbot.env
ExecStart=/home/ubuntu/meetingbot/.venv/bin/python meeting_bot.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now meetingbot
sudo systemctl status meetingbot
```

Logs: `journalctl -u meetingbot -f`

---

## Option B — Fly.io

**Why:** simpler than VM management; `git push`-style deploys. Free allowance covers one tiny machine.

**Caveat:** the filesystem is ephemeral. You **must** mount a volume so `meetings.json` survives restarts.

### 1. Install flyctl & log in
```powershell
iwr https://fly.io/install.ps1 -useb | iex
fly auth signup   # or: fly auth login
```

### 2. Add a Dockerfile next to `meeting_bot.py`
```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY meeting_bot.py .
ENV MEETINGS_FILE=/data/meetings.json
CMD ["python", "meeting_bot.py"]
```

### 3. Launch (don't deploy yet)
```bash
fly launch --no-deploy
# Pick a region close to you. Say NO to Postgres/Redis.
```

### 4. Create a 1 GB volume for persistent storage
```bash
fly volumes create bot_data --size 1 --region <same region you picked>
```

Edit `fly.toml` to mount it:
```toml
[mounts]
source = "bot_data"
destination = "/data"
```

### 5. Set the token (encrypted secret)
```bash
fly secrets set TELEGRAM_BOT_TOKEN=PASTE_YOUR_TOKEN_HERE
```

### 6. Deploy
```bash
fly deploy
fly logs
```

To update later: `git commit` → `fly deploy`.

---

## After either deploy

Verify in Telegram: send `/start` to your bot. Then `/set_meeting`, restart the host (`sudo systemctl restart meetingbot` or `fly machine restart`), then `/show_meeting` — the data should persist.