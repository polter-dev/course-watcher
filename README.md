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
Environment=DISCORD_WEBHOOK=https://discord.com/api/webhooks/...
ExecStart=/path/to/course_notif/venv/bin/python3 watcher.py
Restart=on-failure
RestartSec=30

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now course-watcher
sudo journalctl -u course-watcher -f  # tail logs
```
