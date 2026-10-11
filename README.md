# cc2025bot

## Run with GitHub Actions

The workflow runs the bot for up to 5 hours 50 minutes, then starts it again
on its next scheduled run. To enable it:

1. Add these repository secrets under **Settings > Secrets and variables > Actions**:
	- `PORTAL_CONFIG_JSON`: the complete JSON object normally stored in `config.json`.
	- `BOT_CONFIG_JSON`: the complete JSON object normally stored in `bot_config.json`, including a non-empty `bot_token`.
2. In **Actions**, enable workflows for the repository and run **cc2025 Telegram Bot** manually once, or wait for its six-hour schedule.

The runner uses Ubuntu 24.04 explicitly, so it is not affected by the announced
`ubuntu-latest` migration to Ubuntu 26. A workflow cannot start if GitHub
blocks Actions because of a billing or spending-limit issue; the repository
owner must resolve that under **Billing & plans** before rerunning it.