# update-bot

Daily Arch/AUR update notifications via Telegram, with a one-tap "Update now" button that runs `paru -Syu --noconfirm` and reports the result back to the same chat.

Designed for an always-on Arch box. Reuses an existing Telegram bot (e.g. one already used for arr-stack notifications).

## How it works

Two scripts, both run via [`uv`](https://docs.astral.sh/uv/) with inline PEP 723 dependencies — no separate venv or requirements file.

| Script | Role | Trigger |
|---|---|---|
| `update_checker.py` | Checks for repo and AUR updates, sends a Telegram message if any are pending | systemd timer, daily at 09:00 |
| `update_bot.py` | Long-polls Telegram for inline button presses, runs the update | systemd user service, always running |

When updates are available, the checker sends a message listing them with an inline **Update now** button. Pressing the button triggers a full system upgrade and posts a structured result back to the chat:

- **Starting update...** — initial acknowledgement
- **Progress edits** — the status message updates every 3 minutes with elapsed time during long runs
- **Completion summary** — success or failure with a parsed reason for common errors (sudo blocked, disk full, pacman lock, AUR build failure, network issues)
- **Post-update verification** — re-runs `checkupdates` and `paru -Qua` to report remaining packages
- **Duplicate protection** — a second button press while an update is running is rejected with an alert

On failure, the last ~3500 characters of `paru` output are included. Successful updates omit the full log.

## Prerequisites

On the Arch host:

- [`uv`](https://docs.astral.sh/uv/getting-started/installation/) (e.g. at `~/.local/bin/uv`)
- [`pacman-contrib`](https://archlinux.org/packages/extra/x86_64/pacman-contrib/) — provides `checkupdates`
- [`paru`](https://github.com/morganamilo/paru) — AUR helper
- A Telegram bot token and chat ID

Passwordless `pacman` via sudo (required so `paru` doesn't block on a password prompt during remote updates):

```bash
echo "jamie ALL=(ALL) NOPASSWD: /usr/bin/pacman" | sudo tee /etc/sudoers.d/paru-update
sudo chmod 440 /etc/sudoers.d/paru-update
```

## Configuration

Copy the example env file and fill in your values:

```bash
install -m600 -D config/update-bot.env.example ~/.config/update-bot.env
```

| Variable | Description |
|---|---|
| `TG_BOT_TOKEN` | Bot token from [@BotFather](https://t.me/BotFather) |
| `TG_CHAT_ID` | Chat ID to send notifications to (and accept button presses from) |

Both scripts read these from the environment. The systemd units load them via `EnvironmentFile`.

### Telegram bot conflict

**Before deploying `update_bot.py`, confirm nothing else on the same bot token long-polls `getUpdates`.** Telegram only allows one long-poll consumer per token — a second one gets HTTP 409 errors.

The checker only calls `sendMessage`, so it does not conflict. Other services that push messages (webhooks, one-off API calls) are fine; only a second long-polling `getUpdates` loop is a problem.

## Installation

```bash
# Install scripts
install -m755 scripts/update_checker.py scripts/update_bot.py ~/scripts/

# Install user systemd units
install -m644 systemd/*.service systemd/*.timer ~/.config/systemd/user/

# Enable and start
systemctl --user daemon-reload
systemctl --user enable --now update-bot.service
systemctl --user enable --now update-checker.timer
```

The systemd units use full paths for `uv` and the scripts (systemd does not inherit shell `PATH`). Adjust paths in the unit files if your layout differs from `/home/jamie/`.

### Timer behaviour

`update-checker.timer` fires at **09:00** daily with `Persistent=true`, so a missed run (e.g. machine was off) is caught on next boot.

## Manual testing

```bash
# Source config for ad-hoc runs
set -a && source ~/.config/update-bot.env && set +a

# Checker — should send a message if updates are pending, otherwise exit silently
uv run ~/scripts/update_checker.py

# Bot — should already be running via systemd; check status
systemctl --user status update-bot.service
journalctl --user -u update-bot.service -f
```

### Acceptance checklist

- [ ] Run `update_checker.py` with pending updates → Telegram message with correct package list and a working button
- [ ] Press the button → "Starting update...", progress edits during long runs, then a completion summary with remaining-update verification; `paru -Qu` empty afterwards
- [ ] Press the button again while an update is running → alert: "Update already in progress"
- [ ] Restart `update-bot.service` mid-poll → no duplicate update run on restart
- [ ] Run `update_checker.py` with nothing to update → no message sent, clean exit

## Project layout

```
scripts/
  update_checker.py   # daily oneshot checker
  update_bot.py       # long-poll callback listener
systemd/
  update-checker.service
  update-checker.timer
  update-bot.service
config/
  update-bot.env.example
```

## Out of scope

- `--skipreview` handling for AUR PKGBUILD review prompts
- Webhook mode (long polling is sufficient for an always-on box)
- Retry/backoff tuning beyond `RestartSec=10` on the bot service
- Full log streaming or document upload for large build output
