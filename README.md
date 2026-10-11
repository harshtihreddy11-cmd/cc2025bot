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

Uploads through Telegram's hosted Bot API are split to stay below 45,000,000
bytes, with additional size checks and automatic re-splitting if Telegram
still rejects a part as too large. Its upload limit is much lower than
Telegram's roughly 2 GB limit for a self-hosted Local Bot API Server, which
this bot is not configured to use. `telegram_max_mb` in `BOT_CONFIG_JSON` can
set a lower limit, but a higher value does not increase the hosted Bot API
upload cap.

The optional 90-minute split is applied before the upload-size split. If
FFmpeg cannot produce multiple time-based parts for a file longer than the
requested interval, the job reports a failure instead of uploading the
unsplit file. Split MP4 and M4A files are numbered `_part_001`, `_part_002`,
and so on. Audio-only downloads require the archive to contain an audio
stream; video-only streams are reported as unavailable for audio conversion.
