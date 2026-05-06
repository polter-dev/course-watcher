# Valencia College Course Watcher

Monitors Valencia College Banner Self-Service for open seats and sends Discord notifications when availability changes.

## Install

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp config.example.yaml config.yaml
```

## Configuration

Edit `config.yaml` with your term and courses. See `config.example.yaml` for all options.

### Finding term codes

Hit the terms endpoint directly:

```bash
curl -s 'https://banner.aws.valenciacollege.edu/StudentRegistrationSsb/ssb/classSearch/getTerms?searchTerm=&offset=1&max=30' | python3 -m json.tool
```

Format is `YYYYTT`: `10` = Fall, `20` = Spring, `30` = Summer. Example: `202630` = Summer 2026.

### Finding CRNs

Go to [Banner Self-Service](https://banner.aws.valenciacollege.edu/StudentRegistrationSsb/ssb/term/termSelection?mode=search), select your term, search for your course, and note the CRN (5-digit number) from the results.

Or use `--once --dry-run` with a subject/courseNumber config — the startup log prints all matching CRNs.

### Discord webhook

Create a webhook in your Discord channel (Server Settings → Integrations → Webhooks) and set the env var:

```bash
export DISCORD_WEBHOOK="https://discord.com/api/webhooks/..."
```

Never put the webhook URL in config.yaml.

## Usage

```bash
# Test a single cycle without sending notifications
DISCORD_WEBHOOK="https://discord.com/api/webhooks/..." python3 watcher.py --once --dry-run

# Run continuously
DISCORD_WEBHOOK="https://discord.com/api/webhooks/..." python3 watcher.py

# Custom config path
python3 watcher.py --config /path/to/config.yaml
```

### CLI flags

| Flag | Description |
|------|-------------|
| `--once` | Run a single poll cycle and exit |
| `--dry-run` | Log notifications instead of sending to Discord |
| `--config PATH` | Config file path (default: `config.yaml`) |

## How it works

1. On startup, validates the term code and watch targets against live Banner data. Fails loudly if anything is wrong.
2. Each poll cycle creates a fresh Banner session (3-step flow: init cookies → set term → search).
3. Tracks `seatsAvailable` and `waitAvailable` per CRN in `seats.json`.
4. Notifies on `0 → >0` transitions. Re-arms when seats go back to 0.
5. First run populates state without notifications (avoids startup storm).
6. Exponential backoff on errors (30s → 60s → ... capped at 15min).

### Online-only filtering

When `online_only: true` (the default), the watcher excludes all in-person and Mixed Mode sections. Only sections with `instructionalMethod == "X"` (Banner's code for fully online courses) are tracked. Mixed Mode (`M`) requires campus attendance and is **not** considered online. If no online sections currently exist for a watch target, the watcher logs a warning and continues running — it will notify you if an online section is added later.

### New-section detection

When `notify_new_sections: true` (the default), the watcher sends a Discord notification whenever a CRN appears in search results that wasn't in the previous state file. This catches mid-term section additions. The notification uses a blue embed titled "New section added." This feature respects `online_only` — if enabled, only new online sections trigger notifications. Like seat notifications, new-section notifications are suppressed on the very first run to avoid a startup storm.

## Always-on deployment (systemd)

```ini
# /etc/systemd/system/course-watcher.service
[Unit]
Description=Valencia Course Watcher
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=youruser
WorkingDirectory=/path/to/course_notif
EnvironmentFile=/path/to/course_notif/.env
ExecStart=/path/to/course_notif/venv/bin/python3 watcher.py
Restart=on-failure
RestartSec=30

[Install]
WantedBy=multi-user.target
```

Create the env file with restricted permissions:

```bash
echo 'DISCORD_WEBHOOK=https://discord.com/api/webhooks/...' > /path/to/course_notif/.env
chmod 600 /path/to/course_notif/.env
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now course-watcher
sudo journalctl -u course-watcher -f  # tail logs
```
