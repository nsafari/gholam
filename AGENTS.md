# gholam

Telegram bot that summarizes channel posts using Gemini. Single-file Python app.

## Run

```bash
./run.sh
```

Creates `.venv`, installs deps, runs `bot.py`. First run prompts for Telethon login.

## Structure

- `bot.py` — entire app (415 lines)
- `.env.example` — required env vars template
- No tests, no lint config, no CI

## Key details

- **Two Telegram libraries**: `python-telegram-bot` (bot framework) + `Telethon` (fetch channel post text). Both need credentials.
- **Access control is deny-by-default**: bot ignores all messages unless `ALLOWED_USER_IDS`, `ALLOWED_CHAT_IDS`, or `ALLOWED_CHANNEL_USERNAMES` are set.
- **Telethon sessions** stored outside repo at `~/.local/share/telegram-gemini-summary-bot/`.
- **Comment-only**: bot only replies to automatic channel-post forwards in linked discussion groups, never to direct messages or channel posts.
- **Gemini prompt** hardcoded in `summarize_with_gemini()` (line 247). Output must end with `--داش غلام`.
