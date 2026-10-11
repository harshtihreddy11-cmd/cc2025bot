# cc2025bot

## Run with GitHub Actions

The workflow runs the bot for up to 5 hours 50 minutes, then starts it again
on its next scheduled run. To enable it:

1. Add these repository secrets under **Settings > Secrets and variables > Actions**:
	- `PORTAL_CONFIG_JSON`: the complete JSON object normally stored in `config.json`.
	- `BOT_CONFIG_JSON`: the complete JSON object normally stored in `bot_config.json`, including a non-empty `bot_token`.
	- `TELEGRAM_API_ID` and `TELEGRAM_API_HASH`: application credentials from [my.telegram.org](https://my.telegram.org), required to run Telegram's Local Bot API Server.
2. In **Actions**, enable workflows for the repository and run **cc2025 Telegram Bot** manually once, or wait for its six-hour schedule.

The runner uses Ubuntu 24.04 explicitly, so it is not affected by the announced
`ubuntu-latest` migration to Ubuntu 26. A workflow cannot start if GitHub
blocks Actions because of a billing or spending-limit issue; the repository
owner must resolve that under **Billing & plans** before rerunning it.

The workflow starts a Local Bot API Server and configures the bot to use it,
allowing uploads up to 1900 MiB per file (subject to `telegram_max_mb` in
`BOT_CONFIG_JSON`). The server requires `TELEGRAM_API_ID` and
`TELEGRAM_API_HASH` secrets. Before switching a bot token from Telegram's
hosted Bot API to the Local Bot API Server for the first time, log it out of
the hosted API, then wait at least 10 minutes before starting the workflow:

```sh
curl "https://api.telegram.org/bot<YOUR_BOT_TOKEN>/logOut"
```

The response should contain `"result": true`. The token cannot use both API
servers at the same time.

Large media uploads use extended HTTP timeouts so a slow transfer is not
aborted by the Telegram client's short default media-upload timeout.
MP4 splits are written with fast-start metadata. Before sending MP4s as
videos, the bot probes their duration and dimensions and creates a JPEG
thumbnail from a frame near the middle of each part.

The optional 90-minute split is applied before the upload-size split. If
FFmpeg cannot produce multiple time-based parts for a file longer than the
requested interval, the job reports a failure instead of uploading the
unsplit file. Split MP4 and M4A files are numbered `_part_001`, `_part_002`,
and so on. Audio-only downloads require the archive to contain an audio
stream; video-only streams are reported as unavailable for audio conversion.
